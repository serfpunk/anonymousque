import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramForbiddenError
import main,admin,db

class SupportTests(unittest.IsolatedAsyncioTestCase):
    async def test_support_message_to_admins_then_answer(self):
        state=NS(clear=AsyncMock()); bot=NS(send_message=AsyncMock())
        user=NS(text='Не приходит ответ',from_user=NS(id=8),answer=AsyncMock())
        await admin.support_message(user,state,bot,frozenset({1,2}))
        self.assertEqual(bot.send_message.await_count,2)
        call=bot.send_message.await_args_list[0]
        self.assertIn('Не приходит ответ',call.args[1]); self.assertIn('Telegram ID: 8',call.args[1])
        self.assertEqual(call.kwargs['reply_markup'].inline_keyboard[0][0].callback_data,'support:reply:8')
        state.clear.assert_awaited_once()
        self.assertIn('отправлено',user.answer.await_args.args[0])
        state=NS(clear=AsyncMock(),get_data=AsyncMock(return_value={'support_user_id':8}))
        msg=NS(text='Попробуйте ещё раз',from_user=NS(id=1),answer=AsyncMock())
        await admin.support_reply(msg,state,bot,frozenset({1}))
        self.assertEqual(bot.send_message.await_args.args,(8,'🆘 Ответ поддержки\n\nПопробуйте ещё раз'))
        state.clear.assert_awaited_once()
    async def test_support_failure_is_not_marked_sent(self):
        state=NS(clear=AsyncMock()); m=NS(text='помогите',from_user=NS(id=8),answer=AsyncMock())
        bot=NS(send_message=AsyncMock(side_effect=TelegramForbiddenError(method=NS(), message='blocked')))
        await admin.support_message(m,state,bot,frozenset({1}))
        state.clear.assert_not_awaited()
        self.assertIn('Не удалось',m.answer.await_args.args[0])
    async def test_reply_rejects_nonadmin(self):
        cb=NS(data='support:reply:8',from_user=NS(id=9),answer=AsyncMock(),message=NS(answer=AsyncMock()))
        state=NS(set_state=AsyncMock())
        await admin.support_reply_start(cb,state,frozenset({1}))
        state.set_state.assert_not_awaited(); self.assertTrue(cb.answer.await_args.kwargs['show_alert'])
    async def test_support_starts_with_privacy_notice(self):
        state=NS(clear=AsyncMock(),set_state=AsyncMock()); m=NS(answer=AsyncMock())
        await admin.begin_support(m,state)
        self.assertIn('Telegram ID',m.answer.await_args.args[0])
    async def test_slash_commands_are_not_sent_to_support(self):
        self.assertFalse(admin.not_command(NS(text='/start abc')))
        self.assertFalse(admin.not_command(NS(text='/cancel')))
        self.assertTrue(admin.not_command(NS(text='Помогите')))

class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_question_contains_personal_link_in_caption(self):
        bot=NS(send_photo=AsyncMock())
        footer=main.question_footer('our_bot','person-code')
        await main.send_item(bot,1,'photo','photo','Вопрос','📩 Анонимный вопрос #4',footer=footer)
        caption=bot.send_photo.await_args.kwargs['caption']
        self.assertIn('Вопрос',caption)
        self.assertIn('https://t.me/our_bot?start=person-code',caption)
        self.assertIn('Задать анонимный вопрос',caption)
    async def test_new_question_contains_link(self):
        q={'id':3,'kind':'text','file_id':None,'body':'hello'}
        creator={'user_id':9,'public_code':'code9'}
        m=NS(from_user=NS(id=7),text='hello',answer=AsyncMock())
        state=NS(get_data=AsyncMock(return_value={'creator_id':9}),clear=AsyncMock())
        bot=NS(get_me=AsyncMock(return_value=NS(username='the_bot')),send_message=AsyncMock())
        pool=NS(execute=AsyncMock())
        with patch.object(db,'creator',AsyncMock(return_value=creator)),patch.object(db,'save_question',AsyncMock(return_value=('ok',q))):
            await main.ask(m,state,bot,pool,60)
        self.assertIn('https://t.me/the_bot?start=code9',bot.send_message.await_args.args[1])
    async def test_fallback_question_and_answer_both_contain_links(self):
        q=dict(id=3,kind='photo',file_id='f',body='Q',answer_kind='text',answer_file_id=None,
               answer_body='A',published_channel_id=None,published_question_message_id=None,published_answer_message_id=None)
        cb=NS(data='publish:3',from_user=NS(id=1),answer=AsyncMock(),message=NS(answer=AsyncMock()))
        bot=NS(get_me=AsyncMock(return_value=NS(username='bot_name')),send_photo=AsyncMock(return_value=NS(message_id=1)),send_message=AsyncMock(return_value=NS(message_id=2)))
        pool=NS(execute=AsyncMock())
        with patch.object(main,'get_callback_question',AsyncMock(return_value=q)),patch.object(db,'creator',AsyncMock(return_value={'channel_id':-100,'public_code':'ABC'})),patch.object(main,'verify_channel',AsyncMock()),patch.object(db,'claim_publish',AsyncMock(return_value=q)):
            await main.publish(cb,bot,pool)
        self.assertIn('https://t.me/bot_name?start=ABC',bot.send_photo.await_args.kwargs['caption'])
        self.assertEqual(bot.send_photo.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url,'https://t.me/bot_name?start=ABC')
        self.assertEqual(bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0].url,'https://t.me/bot_name?start=ABC')
    async def test_emoji_caption_is_bounded_in_utf16(self):
        m=NS(text=None,caption='😀'*401,photo=[NS(file_id='p')],voice=None,video=None)
        self.assertIsNone(main.payload(m))
