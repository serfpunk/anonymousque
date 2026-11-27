"""Optional integration test; set TEST_DATABASE_URL to a disposable Postgres database."""
import os
import unittest
import uuid
import asyncpg
import db

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL for integration test')
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_' + uuid.uuid4().hex
        self.admin = await asyncpg.connect(os.environ['TEST_DATABASE_URL'])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ['TEST_DATABASE_URL'], server_settings={'search_path':self.schema})
        await self.pool.execute(db.SCHEMA)
    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()
    async def test_lifecycle_rate_ban_ownership_and_claims(self):
        a=await db.register(self.pool,101)
        self.assertEqual(a['public_code'], (await db.register(self.pool,101))['public_code'])
        self.assertEqual((await db.by_code(self.pool,a['public_code']))['user_id'],101)
        await db.register(self.pool,102)
        status,q=await db.save_question(self.pool,201,101,'text',None,'hello',60)
        self.assertEqual(status,'ok')
        self.assertIsNone(await db.question(self.pool,q['id'],102))
        self.assertEqual((await db.save_question(self.pool,201,101,'text',None,'spam',60))[0],'rate_limited')
        self.assertIsNone(await db.save_answer(self.pool,q['id'],102,'text',None,'bad'))
        self.assertIsNotNone(await db.save_answer(self.pool,q['id'],101,'text',None,'ok'))
        self.assertIsNone(await db.save_answer(self.pool,q['id'],101,'text',None,'another'))
        self.assertIsNotNone(await db.claim_answer(self.pool,q['id'],101))
        self.assertIsNone(await db.claim_answer(self.pool,q['id'],101))
        self.assertTrue(await db.toggle_ban(self.pool,101,201))
        self.assertEqual((await db.save_question(self.pool,201,101,'text',None,'spam',60))[0],'banned')
        self.assertFalse(await db.toggle_ban(self.pool,101,201))
        self.assertIsNotNone(await db.claim_publish(self.pool,q['id'],101))
        self.assertIsNone(await db.claim_publish(self.pool,q['id'],101))
