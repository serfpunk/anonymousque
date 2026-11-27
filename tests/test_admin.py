import os
import asyncio
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import asyncpg
import db
import admin

class AdminUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_waits_without_periodic_queries(self):
        wakeup = asyncio.Event()
        pool = NS(fetchval=AsyncMock(return_value=False))
        with patch.object(admin,'recover',AsyncMock()), patch.object(admin,'next_recipient',AsyncMock(return_value=None)) as next_item:
            task = asyncio.create_task(admin.broadcast_worker(NS(),pool,wakeup))
            try:
                await asyncio.sleep(0.1)
                self.assertEqual(next_item.await_count, 1)
                await asyncio.sleep(0.3)
                self.assertEqual(next_item.await_count, 1)
                wakeup.set()
                await asyncio.sleep(0.1)
                self.assertEqual(next_item.await_count, 2)
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_nonadmin_denied_even_for_copied_button(self):
        cb=NS(from_user=NS(id=999), answer=AsyncMock(), message=NS(answer=AsyncMock()))
        await admin.stats(cb, NS(), frozenset({1}))
        self.assertTrue(cb.answer.await_args.kwargs['show_alert'])
        cb.message.answer.assert_not_awaited()
    def test_neon_tls_parameters(self):
        import ssl
        url, opts = db.connection_parameters('postgresql://u:p@somehost.aws.neon.tech/db?sslmode=require&channel_binding=require')
        self.assertNotIn('channel_binding',url)
        self.assertEqual(opts['ssl'].verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(opts['ssl'].check_hostname)
        with self.assertRaises(ValueError):
            db.connection_parameters('postgresql://u:p@somehost.aws.neon.tech/db?sslmode=disable')

    def test_ads_label_and_stop_bot_footer(self):
        msg=admin.format_message('ads','Акция')
        self.assertIn('Реклама',msg)
        self.assertIn('остановите бота',msg)
        self.assertNotIn('/notifications',msg)
        self.assertNotIn('Реклама',admin.format_message('service','Изменение режима'))
    async def test_admin_state_and_confirmation_permissions(self):
        cb=NS(from_user=NS(id=999),data='adm:send:a',answer=AsyncMock(),message=NS(answer=AsyncMock()))
        state=NS(get_data=AsyncMock())
        await admin.confirm(cb,state,NS(),frozenset({1}),asyncio.Event())
        state.get_data.assert_not_awaited()

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL for integration test')
class BroadcastDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='test_'+uuid.uuid4().hex
        self.admin_conn=await asyncpg.connect(os.environ['TEST_DATABASE_URL'])
        await self.admin_conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool=await asyncpg.create_pool(os.environ['TEST_DATABASE_URL'],server_settings={'search_path':self.schema})
        await self.pool.execute(db.SCHEMA)
    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin_conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin_conn.close()
    async def test_all_users_except_blocked_and_recovery(self):
        await self.pool.execute("INSERT INTO bot_users(user_id,advertising_opt_in,service_opt_in,blocked) VALUES(1,false,false,false),(2,true,true,false),(3,true,true,true)")
        state=NS(get_data=AsyncMock(return_value={'job_id':'abc','category':'ads','body':'hello'}), get_state=AsyncMock(return_value=admin.AdminFlow.confirm.state), clear=AsyncMock())
        cb=NS(from_user=NS(id=1),data='adm:send:abc',answer=AsyncMock(),message=NS(answer=AsyncMock()))
        await admin.confirm(cb,state,self.pool,frozenset({1}),asyncio.Event())
        rows=await self.pool.fetch('SELECT user_id FROM broadcast_recipients ORDER BY user_id')
        self.assertEqual([r['user_id'] for r in rows],[1,2])
        await self.pool.execute('UPDATE bot_users SET blocked=true WHERE user_id=1')
        self.assertIsNone(await admin.next_recipient(self.pool))
        job,uid=await admin.next_recipient(self.pool)
        self.assertEqual(uid,2)
        await admin.recover(self.pool)
        self.assertEqual(await self.pool.fetchval("SELECT status FROM broadcast_recipients WHERE user_id=2"),'unknown')
        self.assertIsNone(await admin.next_recipient(self.pool))
        self.assertEqual(await self.pool.fetchval("SELECT status FROM broadcasts WHERE id='abc'"),'completed')
        self.assertEqual(await self.pool.fetchval("SELECT status FROM broadcast_recipients WHERE user_id=1"),'skipped')
    async def test_stop_does_not_claim_pending(self):
        await self.pool.execute("INSERT INTO bot_users(user_id) VALUES(4); INSERT INTO broadcasts(id,admin_id,category,body,status) VALUES('stop',1,'service','test','cancelled'); INSERT INTO broadcast_recipients(broadcast_id,user_id) VALUES('stop',4)")
        self.assertIsNone(await admin.next_recipient(self.pool))
        self.assertEqual(await self.pool.fetchval("SELECT status FROM broadcast_recipients WHERE user_id=4"),'pending')
