import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import main
import db

class PayloadTests(unittest.TestCase):
    def msg(self, text=None, caption=None, photo=None, voice=None):
        return NS(text=text, caption=caption, photo=photo, voice=voice)
    def test_text_limits_and_formatting(self):
        self.assertEqual(main.payload(self.msg(text=' <hello> ')), ('text',None,'<hello>'))
        self.assertIsNone(main.payload(self.msg(text='x'*3001)))
        self.assertIsNone(main.payload(self.msg(text=' ')))
    def test_media(self):
        self.assertEqual(main.payload(self.msg(photo=[NS(file_id='small'),NS(file_id='best')], caption='hi')), ('photo','best','hi'))
        self.assertEqual(main.payload(self.msg(voice=NS(file_id='v'))), ('voice','v',''))
        self.assertIsNone(main.payload(self.msg(photo=[NS(file_id='p')], caption='x'*801)))

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sending_question_creates_new_message_no_identity(self):
        bot = NS(send_message=AsyncMock(),send_photo=AsyncMock(),send_voice=AsyncMock())
        await main.send_item(bot,123,'text',None,'hello','Question #1',main.question_keyboard(1))
        self.assertEqual(bot.send_message.await_args.args, (123,'Question #1\n\nhello'))
        self.assertFalse(hasattr(bot,'forward_message'))
    async def test_media_sent_as_new_message(self):
        bot = NS(send_message=AsyncMock(),send_photo=AsyncMock(),send_voice=AsyncMock())
        await main.send_item(bot,123,'photo','file','caption','Question',None)
        self.assertEqual(bot.send_photo.await_args.args, (123,'file'))
        self.assertEqual(bot.send_photo.await_args.kwargs['caption'], 'Question\n\ncaption')
    async def test_callback_requires_question_owner(self):
        cb = NS(data='answer:2', from_user=NS(id=123), answer=AsyncMock())
        pool = NS(fetchrow=AsyncMock(return_value=None))
        self.assertIsNone(await main.get_callback_question(cb,pool))
        self.assertEqual(pool.fetchrow.await_args.args[1:], (2,123))
        cb.answer.assert_awaited_once()
    async def test_malformed_callback_rejected(self):
        cb = NS(data='answer:nope',from_user=NS(id=123),answer=AsyncMock())
        self.assertIsNone(await main.get_callback_question(cb,NS()))
    async def test_successful_answer_delivery(self):
        q = dict(asker_id=200,answer_kind='text',answer_file_id=None,answer_body='reply')
        bot = NS(send_message=AsyncMock(),send_photo=AsyncMock(),send_voice=AsyncMock())
        pool = NS(execute=AsyncMock())
        with patch.object(db,'claim_answer',AsyncMock(return_value=q)):
            self.assertEqual(await main.deliver_answer(bot,pool,1,100),'sent')
        self.assertEqual(bot.send_message.await_args.args[0],200)
        self.assertIn("answer_delivery_status='sent'",pool.execute.await_args.args[0])
    async def test_blocked_sender_notified(self):
        m=NS(from_user=NS(id=7),text='hello',answer=AsyncMock())
        state=NS(get_data=AsyncMock(return_value={'creator_id':9}),clear=AsyncMock())
        with patch.object(db,'creator',AsyncMock(return_value={'user_id':9})), patch.object(db,'save_question',AsyncMock(return_value=('banned',None))):
            await main.ask(m,state,NS(),NS(),60)
        m.answer.assert_awaited_once()
        self.assertIn('недоступна',m.answer.await_args.args[0])
        state.clear.assert_awaited_once()

if __name__ == '__main__': unittest.main()
