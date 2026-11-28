import os
import unittest
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import asyncpg
import db
import main
import admin

class FeatureUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_payload_and_delivery(self):
        m=NS(text=None,caption='hi',photo=None,voice=None,video=NS(file_id='vid'))
        self.assertEqual(main.payload(m),('video','vid','hi'))
        bot=NS(send_video=AsyncMock())
        await main.send_item(bot,1,'video','vid','hi','question')
        self.assertEqual(bot.send_video.await_args.args,(1,'vid'))
    async def test_closed_link_does_not_enter_question_flow(self):
        m=NS(from_user=NS(id=4),answer=AsyncMock())
        state=NS(clear=AsyncMock(),set_state=AsyncMock())
        with patch.object(db,'by_code',AsyncMock(return_value={'user_id':5,'accepting_questions':False})):
            await main.start_link(m,NS(args='code'),state,NS())
        state.set_state.assert_not_awaited()
        self.assertIn('закрыл',m.answer.await_args.args[0])
    async def test_settings_rejects_injected_property(self):
        cb=NS(data='set:evil',from_user=NS(id=1),answer=AsyncMock())
        pool=NS(fetchrow=AsyncMock())
        await main.settings_change(cb,NS(),pool)
        pool.fetchrow.assert_not_awaited()
    async def test_invalid_pagination_rejected(self):
        cb=NS(data='inbox:new:-1',from_user=NS(id=1),answer=AsyncMock())
        pool=NS(fetchval=AsyncMock())
        await main.inbox_page(cb,NS(),pool)
        pool.fetchval.assert_not_awaited()
    async def test_fallback_channel_answer_has_deep_link(self):
        q=dict(id=3,kind='photo',file_id='f',body='Q',answer_kind='text',answer_file_id=None,
               answer_body='A',published_channel_id=None,published_question_message_id=None,published_answer_message_id=None)
        cb=NS(data='publish:3',from_user=NS(id=1),answer=AsyncMock(),message=NS(answer=AsyncMock()))
        bot=NS(get_me=AsyncMock(return_value=NS(username='bot_name')),send_photo=AsyncMock(return_value=NS(message_id=1)),send_message=AsyncMock(return_value=NS(message_id=2)))
        pool=NS(execute=AsyncMock())
        with patch.object(main,'get_callback_question',AsyncMock(return_value=q)),patch.object(db,'creator',AsyncMock(return_value={'channel_id':-100,'public_code':'ABC'})),patch.object(main,'verify_channel',AsyncMock()),patch.object(db,'claim_publish',AsyncMock(return_value=q)):
            await main.publish(cb,bot,pool)
        markup=bot.send_message.await_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[0][0].url,'https://t.me/bot_name?start=ABC')
    async def test_greeting_stored_and_removed(self):
        state=NS(clear=AsyncMock()); pool=NS(execute=AsyncMock())
        m=NS(text='/clear_greeting',from_user=NS(id=1),answer=AsyncMock())
        await main.set_greeting(m,state,pool)
        self.assertEqual(pool.execute.await_args.args[1:],(1,''))
    async def test_skipped_question_cannot_be_answered(self):
        cb=NS(data='answer:1',from_user=NS(id=1),answer=AsyncMock(),message=NS(answer=AsyncMock()))
        state=NS(set_state=AsyncMock())
        with patch.object(main,'get_callback_question',AsyncMock(return_value={'skipped':True})):
            await main.answer_start(cb,state,NS())
        state.set_state.assert_not_awaited()

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL for integration test')
class FeatureDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema='feature_'+uuid.uuid4().hex
        self.conn=await asyncpg.connect(os.environ['TEST_DATABASE_URL'])
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool=await asyncpg.create_pool(os.environ['TEST_DATABASE_URL'],server_settings={'search_path':self.schema})
        await self.pool.execute(db.SCHEMA)
    async def asyncTearDown(self):
        await self.pool.close(); await self.conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE'); await self.conn.close()
    async def test_schema_idempotence_and_data_preservation(self):
        await db.register(self.pool,10)
        await self.pool.execute("UPDATE creators SET greeting='hello',accepting_questions=false WHERE user_id=10")
        await self.pool.execute(db.SCHEMA)
        self.assertEqual((await db.creator(self.pool,10))['greeting'],'hello')
        self.assertFalse((await db.creator(self.pool,10))['accepting_questions'])
    async def test_disabled_and_reopened_settings_enforced(self):
        await db.register(self.pool,10)
        await self.pool.execute('UPDATE creators SET accepting_questions=false WHERE user_id=10')
        self.assertEqual((await db.save_question(self.pool,20,10,'text',None,'a',60))[0],'closed')
        await self.pool.execute('UPDATE creators SET accepting_questions=true, allow_text=false, allow_video=true WHERE user_id=10')
        self.assertEqual((await db.save_question(self.pool,20,10,'text',None,'a',60))[0],'type_disabled')
        self.assertEqual((await db.save_question(self.pool,20,10,'video','vid','',60))[0],'ok')
    async def test_skip_restore_and_ownership(self):
        await db.register(self.pool,10); await db.register(self.pool,11)
        _,q=await db.save_question(self.pool,20,10,'text',None,'question',60)
        cb=NS(data=f'skip:{q["id"]}',from_user=NS(id=11),answer=AsyncMock(),message=NS(answer=AsyncMock()))
        await main.skip_question(cb,self.pool)
        self.assertFalse((await db.question(self.pool,q['id'],10))['skipped'])
        cb.from_user.id=10
        await main.skip_question(cb,self.pool)
        self.assertTrue((await db.question(self.pool,q['id'],10))['skipped'])
        self.assertIsNone(await db.save_answer(self.pool,q['id'],10,'text',None,'answer'))
        cb.data=f'restore:{q["id"]}'; await main.skip_question(cb,self.pool)
        self.assertIsNotNone(await db.save_answer(self.pool,q['id'],10,'video','vid','answer'))
    async def test_migration_of_preexisting_tables(self):
        # Recreate pre-update schema in this disposable schema and preserve existing data.
        await self.pool.execute('DROP TABLE rate_limits,bans,questions,creators,broadcast_recipients,broadcasts,bot_users CASCADE')
        old=db.SCHEMA.replace("('text', 'photo', 'voice', 'video')", "('text', 'photo', 'voice')")
        begin=old.index('ALTER TABLE creators ADD COLUMN')
        end=old.index('CREATE INDEX IF NOT EXISTS questions_creator_id_idx',begin)
        old=old[:begin]+old[end:]
        await self.pool.execute(old)
        await self.pool.execute("INSERT INTO creators(user_id,public_code) VALUES(10,'code'); INSERT INTO questions(asker_id,creator_id,kind,body,delivery_status) VALUES(20,10,'text','original','sent')")
        await self.pool.execute(db.SCHEMA)
        self.assertEqual(await self.pool.fetchval('SELECT body FROM questions'),'original')
        row=await self.pool.fetchrow('SELECT * FROM creators WHERE user_id=10')
        self.assertTrue(row['accepting_questions']); self.assertFalse(row['allow_video'])
        await self.pool.execute("INSERT INTO questions(asker_id,creator_id,kind,file_id) VALUES(21,10,'video','vid')")
    async def test_stats_and_history_queries(self):
        await db.register(self.pool,10)
        await self.pool.execute("INSERT INTO bot_users(user_id,first_seen,last_seen) VALUES (10,now(),now())")
        cb=NS(from_user=NS(id=10),answer=AsyncMock(),message=NS(answer=AsyncMock()))
        await admin.stats(cb,self.pool,frozenset({10}))
        self.assertIn('Креаторов: 1',cb.message.answer.await_args.args[0])
        await main.stats(cb,self.pool)
        self.assertIn('Получено всего: 0',cb.message.answer.await_args.args[0])
        await self.pool.execute("INSERT INTO broadcasts(id,admin_id,category,body) VALUES('b',10,'ads','t'); INSERT INTO broadcast_recipients(broadcast_id,user_id,status) VALUES('b',10,'sent')")
        await admin.recent_jobs(cb,self.pool,frozenset({10}))
        self.assertIn('Отправлено: 1',cb.message.answer.await_args.args[0])

    async def test_paginated_queue_and_answered_render(self):
        await db.register(self.pool,10)
        for asker in (21,22,23):
            _,q=await db.save_question(self.pool,asker,10,'text',None,f'q{asker}',60)
            await self.pool.execute("UPDATE questions SET delivery_status='sent' WHERE id=$1",q['id'])
        cb=NS(data='inbox:new:0',from_user=NS(id=10),answer=AsyncMock(),message=NS(answer=AsyncMock()))
        bot=NS(send_message=AsyncMock(),get_me=AsyncMock(return_value=NS(username='my_bot')))
        await main.inbox_page(cb,bot,self.pool)
        self.assertIn('1/3',bot.send_message.await_args.args[1])
        self.assertIn('q23',bot.send_message.await_args.args[1])
        cb.data='inbox:new:1'; await main.inbox_page(cb,bot,self.pool)
        self.assertIn('2/3',bot.send_message.await_args.args[1])
        self.assertIn('q22',bot.send_message.await_args.args[1])
        qid=await self.pool.fetchval("SELECT id FROM questions WHERE body='q23'")
        await db.save_answer(self.pool,qid,10,'text',None,'answer23')
        cb.data='inbox:answered:0'; await main.inbox_page(cb,bot,self.pool)
        self.assertIn('answer23',bot.send_message.await_args.args[1])
    async def test_creator_cannot_read_another_queue(self):
        await db.register(self.pool,10); await db.register(self.pool,11)
        _,q=await db.save_question(self.pool,21,10,'text',None,'secret',60)
        await self.pool.execute("UPDATE questions SET delivery_status='sent' WHERE id=$1",q['id'])
        cb=NS(data='inbox:new:0',from_user=NS(id=11),answer=AsyncMock(),message=NS(answer=AsyncMock()))
        bot=NS(send_message=AsyncMock())
        await main.inbox_page(cb,bot,self.pool)
        bot.send_message.assert_not_awaited()
        self.assertIn('нет вопросов',cb.message.answer.await_args.args[0])
