import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import main


class FormattingTests(unittest.TestCase):
    def test_question_quote_and_link_are_escaped(self):
        post = main.combined_post('A < B & C', 'Ответ <b>текст</b>', 'our_bot', 'abc123')
        self.assertIn('<blockquote>A &lt; B &amp; C</blockquote>', post)
        self.assertIn('Ответ &lt;b&gt;текст&lt;/b&gt;', post)
        self.assertIn('https://t.me/our_bot?start=abc123', post)
        self.assertIn('Задать анонимный вопрос', post)

    def test_limit_checks_utf16_without_truncating(self):
        self.assertIsNone(main.combined_post('😀' * 2048, 'end', 'our_bot', 'code'))
        self.assertIsNone(main.combined_post('q' * 3000, 'a' * 3000, 'our_bot', 'code'))
        self.assertIsNotNone(main.combined_post('q' * 1000, 'a' * 1000, 'our_bot', 'code'))


class PublishTests(unittest.IsolatedAsyncioTestCase):
    def question(self, **changes):
        q = dict(id=23, kind='text', file_id=None, body='q <x>',
                 answer_kind='text', answer_file_id=None, answer_body='answer',
                 published_channel_id=None, published_question_message_id=None,
                 published_answer_message_id=None)
        q.update(changes)
        return q

    async def run_publish(self, question):
        callback = NS(data='publish:23', from_user=NS(id=42),
                      answer=AsyncMock(), message=NS(answer=AsyncMock()))
        bot = NS(get_me=AsyncMock(return_value=NS(username='our_bot')),
                 send_message=AsyncMock(return_value=NS(message_id=76)),
                 send_photo=AsyncMock(return_value=NS(message_id=75)),
                 send_voice=AsyncMock(return_value=NS(message_id=74)))
        pool = NS(execute=AsyncMock())
        creator = {'channel_id': -100123, 'public_code': 'code123'}
        with patch.object(main, 'get_callback_question', AsyncMock(return_value=question)), \
             patch.object(main.db, 'creator', AsyncMock(return_value=creator)), \
             patch.object(main, 'verify_channel', AsyncMock()), \
             patch.object(main.db, 'claim_publish', AsyncMock(return_value=question)):
            await main.publish(callback, bot, pool)
        return callback, bot, pool

    async def test_text_pair_is_one_post_and_both_ids_are_recorded(self):
        callback, bot, pool = await self.run_publish(self.question())
        self.assertEqual(bot.send_message.await_count, 1)
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args[0], -100123)
        self.assertIn('<blockquote>q &lt;x&gt;</blockquote>', args[1])
        self.assertIn('https://t.me/our_bot?start=code123', args[1])
        self.assertEqual(kwargs['parse_mode'], 'HTML')
        self.assertTrue(kwargs['link_preview_options'].is_disabled)
        sql, qid, uid, cid, msgid = pool.execute.await_args.args
        self.assertIn('published_question_message_id=$4,published_answer_message_id=$4', sql)
        self.assertEqual((qid, uid, cid, msgid), (23, 42, -100123, 76))

    async def test_media_preserves_old_two_message_behavior(self):
        _, bot, pool = await self.run_publish(self.question(kind='photo', file_id='photoid'))
        self.assertEqual(bot.send_photo.await_count, 1)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertEqual(pool.execute.await_count, 2)

    async def test_long_text_preserves_both_contents(self):
        _, bot, pool = await self.run_publish(self.question(body='q' * 3000, answer_body='a' * 3000))
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertIn('q' * 3000, bot.send_message.await_args_list[0].args[1])
        self.assertIn('a' * 3000, bot.send_message.await_args_list[1].args[1])
        self.assertEqual(pool.execute.await_count, 2)

    async def test_incomplete_old_post_finishes_without_reposting_question(self):
        _, bot, pool = await self.run_publish(self.question(published_channel_id=-100123, published_question_message_id=70))
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertIn('Ответ на вопрос', bot.send_message.await_args.args[1])
        self.assertEqual(pool.execute.await_count, 1)

import os
import uuid
import asyncpg
import db

@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL for integration test')
class PublishDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = 'test_' + uuid.uuid4().hex
        self.conn = await asyncpg.connect(os.environ['TEST_DATABASE_URL'])
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool = await asyncpg.create_pool(os.environ['TEST_DATABASE_URL'], server_settings={'search_path': self.schema})
        await self.pool.execute(db.SCHEMA)

    async def asyncTearDown(self):
        await self.pool.close()
        await self.conn.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.conn.close()

    async def test_one_post_persists_both_ids_and_prevents_second_publish(self):
        creator = await db.register(self.pool, 42)
        await db.set_channel(self.pool, 42, -100123, 'Example')
        _, question = await db.save_question(self.pool, 22, 42, 'text', None, 'Question <3', 60)
        qid = question['id']
        await db.save_answer(self.pool, qid, 42, 'text', None, 'Answer')
        bot = NS(get_me=AsyncMock(return_value=NS(username='our_bot')),
                 send_message=AsyncMock(return_value=NS(message_id=76)))
        callback = NS(data=f'publish:{qid}', from_user=NS(id=42),
                      answer=AsyncMock(), message=NS(answer=AsyncMock()))
        with patch.object(main, 'verify_channel', AsyncMock()):
            await main.publish(callback, bot, self.pool)
            await main.publish(callback, bot, self.pool)
        self.assertEqual(bot.send_message.await_count, 1)
        text = bot.send_message.await_args.args[1]
        self.assertIn('<blockquote>Question &lt;3</blockquote>', text)
        self.assertIn(creator['public_code'], text)
        row = await self.pool.fetchrow('SELECT published_channel_id, published_question_message_id, published_answer_message_id, publish_claimed_at FROM questions WHERE id=$1', qid)
        self.assertEqual(row['published_channel_id'], -100123)
        self.assertEqual(row['published_question_message_id'], 76)
        self.assertEqual(row['published_answer_message_id'], 76)
        self.assertIsNone(row['publish_claimed_at'])
