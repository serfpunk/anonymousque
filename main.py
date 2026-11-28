"""Anonymous Q&A bot: one polling process and PostgreSQL, aiogram 3."""
import asyncio
import html
import logging
import os
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, MessageOriginChannel

import db
import admin

logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('anonymousque')
r = Router()
r.message.filter(F.chat.type == 'private')
r.callback_query.filter(F.message.chat.type == 'private')

class Flow(StatesGroup):
    question = State()
    answer = State()
    channel = State()
    greeting = State()

def buttons(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])

def home_keyboard(has_channel):
    return buttons([('📥 Неотвеченные вопросы', 'inbox')], [('📢 Подключить канал', 'channel')],
                   *([[('❌ Отключить канал', 'unlink')]] if has_channel else []),
                   [('⚙️ Настройки вопросов', 'settings')], [('📊 Статистика', 'stats')], [('🆘 Поддержка', 'support')])

def question_keyboard(qid):
    return buttons([('✍️ Ответить', f'answer:{qid}')], [('⏭ Пропустить', f'skip:{qid}')], [('🚫 Заблокировать / разблокировать', f'ban:{qid}')])

def answer_keyboard(qid, delivery_failed=False):
    rows = []
    if delivery_failed:
        rows.append([('🔁 Повторить доставку ответа', f'deliver:{qid}')])
    rows.append([('📢 Опубликовать вопрос и ответ', f'publish:{qid}')])
    return buttons(*rows)

def payload(message):
    if message.text is not None:
        text = message.text.strip()
        return ('text', None, text) if text and len(text.encode('utf-16-le')) // 2 <= 3000 else None
    text = (message.caption or '').strip()
    if len(text.encode('utf-16-le')) // 2 > 800:
        return None
    if getattr(message, 'video', None):
        return 'video', message.video.file_id, text
    if message.photo:
        return 'photo', message.photo[-1].file_id, text
    if message.voice:
        return 'voice', message.voice.file_id, text
    return None

async def send_item(bot, chat_id, kind, file_id, body, heading, markup=None, footer=None):
    """Create a new message, never forward metadata identifying the sender."""
    text = heading + ('\n\n' + body if body else '') + ('\n\n' + footer if footer else '')
    if kind == 'text':
        return await bot.send_message(chat_id, text, reply_markup=markup)
    if kind == 'photo':
        return await bot.send_photo(chat_id, file_id, caption=text, reply_markup=markup)
    if kind == 'voice':
        return await bot.send_voice(chat_id, file_id, caption=text, reply_markup=markup)
    if kind == 'video':
        return await bot.send_video(chat_id, file_id, caption=text, reply_markup=markup)
    raise ValueError('Unsupported message type')

def question_link(username, public_code):
    return f'https://t.me/{quote(username, safe="")}?start={quote(public_code, safe="")}'

def question_footer(username, public_code):
    return '🔗 Задать анонимный вопрос:\n' + question_link(username, public_code)

def combined_post(question, answer, username, public_code):
    """Render text-only Q&A as one post; do not truncate user content."""
    question, answer = question or '', answer or ''
    label = 'Задать анонимный вопрос'
    plain = f'❓ Анонимный вопрос:\n{question}\n\n💬 Ответ:\n{answer}\n\n{label}'
    # Telegram's entity offsets and limits are measured in UTF-16 code units.
    if len(plain.encode('utf-16-le')) // 2 > 4096:
        return None
    link = question_link(username, public_code)
    return (
        f'❓ <b>Анонимный вопрос:</b>\n<blockquote>{html.escape(question)}</blockquote>\n\n'
        f'💬 <b>Ответ:</b>\n{html.escape(answer)}\n\n'
        f'<a href="{html.escape(link, quote=True)}">{label}</a>'
    )

async def cabinet(message, bot, pool):
    user = await db.register(pool, message.from_user.id)
    me = await bot.get_me()
    channel = user['channel_title'] or 'не подключён'
    await message.answer(f'👋 Кабинет креатора\n\nВаша ссылка для вопросов:\nhttps://t.me/{me.username}?start={user["public_code"]}\n\nКанал: {channel}\n\nРазместите ссылку в канале. Ответы можно отправлять лично и при желании публиковать.\n\nБот может отправлять сервисные уведомления и рекламу. Чтобы прекратить все сообщения, остановите бота в Telegram.', reply_markup=home_keyboard(bool(user['channel_id'])))

@r.message(Command('cancel'))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Действие отменено. /start — открыть кабинет.')

@r.message(CommandStart(deep_link=True))
async def start_link(message: Message, command: CommandObject, state: FSMContext, pool):
    await state.clear()
    target = await db.by_code(pool, (command.args or '').strip())
    if not target:
        await message.answer('Ссылка недействительна. Попросите креатора прислать её снова.')
        return
    if target['user_id'] == message.from_user.id:
        await message.answer('Это ваша собственная ссылка. /start — открыть кабинет.')
        return
    if not target['accepting_questions']:
        await message.answer('Креатор временно закрыл приём вопросов. Попробуйте позже.')
        return
    if await db.is_banned(pool, target['user_id'], message.from_user.id):
        await message.answer('Отправка вопросов этому креатору недоступна.')
        return
    await state.set_state(Flow.question)
    await state.update_data(creator_id=target['user_id'])
    name = target['channel_title'] or 'креатору'
    allowed = ', '.join(label for kind, label in (('text','текст'), ('photo','фото'), ('voice','голосовое'), ('video','видео')) if target['allow_' + kind]) or 'нет доступных форматов'
    greeting = f'Сообщение креатора: {target["greeting"]}\n\n' if target['greeting'] else ''
    await message.answer(f'{greeting}✍️ Напишите вопрос: {name}. Доступно: {allowed}.\n\n'
        'Креатор не увидит ваш профиль. Владелец сервиса и Telegram могут знать, кто отправил вопрос; '
        'ваш Telegram ID хранится для доставки ответа. Креатор может опубликовать вопрос и ответ в канале. '
        'Не включайте личные данные, если не хотите их раскрывать. Фото и голос также могут вас идентифицировать.\n\nБот может отправлять сервисные уведомления и рекламу. Чтобы прекратить все сообщения, остановите бота в Telegram. /cancel — отмена. /support — связь с поддержкой.')

@r.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot, pool):
    await state.clear()
    await cabinet(message, bot, pool)

@r.callback_query(F.data == 'channel')
async def channel_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(Flow.channel)
    await callback.message.answer('📢 Добавьте бота администратором канала с правом публикации. Затем перешлите пост из канала или отправьте его @username или числовой ID. Вы должны быть администратором этого канала. /cancel — отмена.')

async def verify_channel(bot, channel_ref, uid):
    chat = await bot.get_chat(channel_ref)
    if chat.type != 'channel':
        raise ValueError('Это не канал.')
    user = await bot.get_chat_member(chat.id, uid)
    if user.status not in ('administrator', 'creator'):
        raise ValueError('Вы должны быть администратором канала.')
    me = await bot.get_me()
    member = await bot.get_chat_member(chat.id, me.id)
    if member.status != 'administrator' or not getattr(member, 'can_post_messages', False):
        raise ValueError('У бота нет права публиковать сообщения в канале.')
    return chat

@r.message(Flow.channel)
async def channel_set(message: Message, state: FSMContext, bot: Bot, pool):
    ref = None
    if isinstance(message.forward_origin, MessageOriginChannel):
        ref = message.forward_origin.chat.id
    elif message.text:
        raw = message.text.strip()
        if raw.startswith('@'):
            ref = raw
        elif raw.lstrip('-').isdigit():
            ref = int(raw)
    if ref is None:
        await message.answer('Перешлите пост канала или отправьте @username / ID. /cancel — отмена.')
        return
    try:
        chat = await verify_channel(bot, ref, message.from_user.id)
    except ValueError as e:
        await message.answer(str(e))
        return
    except TelegramAPIError:
        await message.answer('Не удалось проверить канал. Проверьте права бота и повторите.')
        return
    await db.register(pool, message.from_user.id)
    await db.set_channel(pool, message.from_user.id, chat.id, chat.title or 'Канал')
    await state.clear()
    await message.answer('✅ Канал подключён. Публикация каждого ответа выполняется только по вашему выбору.')

@r.callback_query(F.data == 'unlink')
async def unlink(callback: CallbackQuery, pool):
    await db.set_channel(pool, callback.from_user.id, None, None)
    await callback.answer('Канал отключён')
    await callback.message.answer('Канал отключён. Личные ответы продолжат работать.')

def inbox_menu():
    return buttons([('📥 Новые', 'inbox:new:0'), ('✅ Отвеченные', 'inbox:answered:0')],
                   [('⏭ Пропущенные', 'inbox:skipped:0')])

@r.callback_query(F.data == 'inbox')
async def inbox(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer('Выберите список вопросов:', reply_markup=inbox_menu())

@r.callback_query(F.data.startswith('inbox:'))
async def inbox_page(callback: CallbackQuery, bot: Bot, pool):
    try:
        _, category, raw = callback.data.split(':')
        offset = int(raw)
        if category not in ('new','answered','skipped') or offset < 0 or offset > 1000000: raise ValueError
    except (ValueError, AttributeError):
        await callback.answer('Некорректная кнопка.', show_alert=True); return
    filters = {'new': "answer_kind IS NULL AND NOT legacy_answered AND NOT skipped",
               'answered': '(answer_kind IS NOT NULL OR legacy_answered)',
               'skipped': 'skipped AND answer_kind IS NULL AND NOT legacy_answered'}
    where = filters[category]
    count = await pool.fetchval(f"SELECT COUNT(*) FROM questions WHERE creator_id=$1 AND delivery_status='sent' AND {where}", callback.from_user.id)
    if not count:
        await callback.answer(); await callback.message.answer('В этом списке пока нет вопросов.', reply_markup=inbox_menu()); return
    offset = min(offset, count - 1)
    q = await pool.fetchrow(f"SELECT * FROM questions WHERE creator_id=$1 AND delivery_status='sent' AND {where} ORDER BY id DESC LIMIT 1 OFFSET $2", callback.from_user.id, offset)
    if not q:
        await callback.answer('Список изменился, откройте его снова.', show_alert=True); return
    rows = []
    if category == 'new': rows += [[('✍️ Ответить', f'answer:{q["id"]}'), ('⏭ Пропустить', f'skip:{q["id"]}')]]
    if category == 'skipped': rows += [[('↩️ Вернуть в новые', f'restore:{q["id"]}')]]
    if category == 'answered' and q['answer_kind']:
        rows += [[('📢 Опубликовать', f'publish:{q["id"]}')]]
        if q['answer_delivery_status'] != 'sent': rows += [[('🔁 Повторить доставку', f'deliver:{q["id"]}')]]
    nav = []
    if offset > 0: nav.append(('⬅️ Назад', f'inbox:{category}:{offset-1}'))
    if offset + 1 < count: nav.append(('Вперёд ➡️', f'inbox:{category}:{offset+1}'))
    if nav: rows.append(nav)
    rows.append([('📋 Списки', 'inbox')])
    await callback.answer()
    suffix = ' · старый ответ не сохранился' if q['legacy_answered'] else ''
    heading = f'📩 Вопрос #{q["id"]} · {offset+1}/{count}{suffix}'
    me = await bot.get_me()
    creator = await db.creator(pool, callback.from_user.id)
    footer = question_footer(me.username, creator['public_code']) if me.username and creator else None
    if category == 'answered' and q['answer_kind']:
        await send_item(bot, callback.from_user.id, q['kind'], q['file_id'], q['body'], heading, footer=footer)
        await send_item(bot, callback.from_user.id, q['answer_kind'], q['answer_file_id'], q['answer_body'] or '', '💬 Ваш ответ', buttons(*rows))
    else:
        await send_item(bot, callback.from_user.id, q['kind'], q['file_id'], q['body'], heading, buttons(*rows), footer=footer)

@r.callback_query(F.data.startswith(('skip:', 'restore:')))
async def skip_question(callback: CallbackQuery, pool):
    q = await get_callback_question(callback, pool)
    if not q: return
    skip = callback.data.startswith('skip:')
    changed = await pool.fetchval("UPDATE questions SET skipped=$3 WHERE id=$1 AND creator_id=$2 AND answer_kind IS NULL AND NOT legacy_answered AND skipped<>$3 RETURNING true", q['id'], callback.from_user.id, skip)
    await callback.answer('Сохранено' if changed else 'Вопрос уже изменился', show_alert=not bool(changed))
    if changed: await callback.message.answer('⏭ Вопрос пропущен.' if skip else '↩️ Вопрос возвращён в новые.', reply_markup=inbox_menu())

def settings_keyboard(user):
    mark = lambda v: '✅' if v else '❌'
    return buttons([(f'{mark(user["accepting_questions"])} Приём вопросов', 'set:accepting_questions')],
        [(f'{mark(user["allow_text"])} Текст', 'set:allow_text'), (f'{mark(user["allow_photo"])} Фото', 'set:allow_photo')],
        [(f'{mark(user["allow_voice"])} Голосовые', 'set:allow_voice'), (f'{mark(user["allow_video"])} Видео', 'set:allow_video')],
        [('✏️ Приветствие', 'set:greeting')])

@r.callback_query(F.data == 'settings')
async def settings(callback: CallbackQuery, pool):
    user = await db.register(pool, callback.from_user.id)
    await callback.answer()
    await callback.message.answer('⚙️ Настройки вопросов\nНажмите на пункт, чтобы изменить его. Приветствие показывается до отправки вопроса.', reply_markup=settings_keyboard(user))

@r.callback_query(F.data.startswith('set:'))
async def settings_change(callback: CallbackQuery, state: FSMContext, pool):
    key = callback.data.split(':',1)[1]
    if key == 'greeting':
        await state.set_state(Flow.greeting); await callback.answer()
        await callback.message.answer('Отправьте приветствие до 300 символов. Чтобы удалить его, отправьте /clear_greeting. /cancel — отмена.')
        return
    if key not in ('accepting_questions','allow_text','allow_photo','allow_voice','allow_video'):
        await callback.answer('Некорректная настройка.', show_alert=True); return
    user = await pool.fetchrow(f'UPDATE creators SET {key}=NOT {key} WHERE user_id=$1 RETURNING *', callback.from_user.id)
    if not user:
        await callback.answer('Откройте /start.', show_alert=True); return
    await callback.answer('Сохранено')
    await callback.message.answer('⚙️ Настройки обновлены. Если выключить все форматы, вопросы отправить нельзя.', reply_markup=settings_keyboard(user))

@r.message(Flow.greeting)
async def set_greeting(message: Message, state: FSMContext, pool):
    text = (message.text or '').strip()
    if text == '/clear_greeting': text = ''
    elif not text or len(text) > 300:
        await message.answer('Нужен текст до 300 символов. Для удаления отправьте /clear_greeting.'); return
    await pool.execute('UPDATE creators SET greeting=$2 WHERE user_id=$1', message.from_user.id, text)
    await state.clear()
    await message.answer('✅ Приветствие сохранено. /start — кабинет.')

@r.callback_query(F.data == 'stats')
async def stats(callback: CallbackQuery, pool):
    row = await pool.fetchrow("""SELECT COUNT(*) AS total,
        COUNT(*) FILTER (WHERE created_at >= now()-interval '7 days') AS week,
        COUNT(*) FILTER (WHERE created_at >= now()-interval '14 days' AND created_at < now()-interval '7 days') AS previous_week,
        COUNT(*) FILTER (WHERE answered_at >= now()-interval '7 days') AS answered_week,
        COUNT(*) FILTER (WHERE published_at >= now()-interval '7 days') AS published_week,
        COUNT(*) FILTER (WHERE answer_kind IS NOT NULL OR legacy_answered) AS answered,
        COUNT(*) FILTER (WHERE answer_kind IS NULL AND NOT legacy_answered AND NOT skipped) AS new,
        COUNT(*) FILTER (WHERE skipped AND answer_kind IS NULL AND NOT legacy_answered) AS skipped,
        COUNT(*) FILTER (WHERE published_answer_message_id IS NOT NULL) AS published
        FROM questions WHERE creator_id=$1 AND delivery_status='sent'""", callback.from_user.id)
    await callback.answer()
    await callback.message.answer(f'📊 Статистика\nПолучено всего: {row["total"]}\nЗа последние 7 дней: {row["week"]}; за предыдущие 7 дней: {row["previous_week"]}\nНовых: {row["new"]}\nОтвеченных: {row["answered"]}\nПропущенных: {row["skipped"]}\nОпубликовано ответов: {row["published"]}\nЗа последние 7 дней: ответов {row["answered_week"]}, публикаций {row["published_week"]}\n\nДаты публикаций учитываются только после этого обновления. В ответах учитываются также отметки из старой версии; их содержание могло не сохраниться.')

@r.message(Flow.question)
async def ask(message: Message, state: FSMContext, bot: Bot, pool, rate_limit: int):
    data = payload(message)
    if not data:
        await message.answer('Отправьте текст до 3000 символов, фото, видео или голосовое с подписью до 800 символов. /cancel — отмена.')
        return
    flow = await state.get_data()
    creator_id = flow.get('creator_id')
    creator = await db.creator(pool, creator_id) if creator_id else None
    if not creator:
        await state.clear()
        await message.answer('Ссылка больше недоступна.')
        return
    status, q = await db.save_question(pool, message.from_user.id, creator_id, *data, rate_limit)
    if status == 'closed':
        await state.clear(); await message.answer('Креатор временно закрыл приём вопросов.'); return
    if status == 'type_disabled':
        await message.answer('Этот формат сейчас отключён креатором. Попробуйте другой.'); return
    if status == 'rate_limited':
        await message.answer(f'Слишком часто. Подождите {rate_limit} секунд и попробуйте снова.')
        return
    if status == 'banned':
        await state.clear()
        await message.answer('Отправка вопросов этому креатору недоступна.')
        return
    await state.clear()
    try:
        me = await bot.get_me()
        footer = question_footer(me.username, creator['public_code']) if me.username else None
        await send_item(bot, creator_id, q['kind'], q['file_id'], q['body'], f'📩 Анонимный вопрос #{q["id"]}', question_keyboard(q['id']), footer=footer)
    except TelegramAPIError:
        await pool.execute("UPDATE questions SET delivery_status='failed' WHERE id=$1", q['id'])
        await message.answer('Не удалось доставить вопрос креатору. Попробуйте позже.')
        return
    await pool.execute("UPDATE questions SET delivery_status='sent' WHERE id=$1", q['id'])
    await message.answer('✅ Вопрос отправлен. Если креатор ответит, бот пришлёт ответ сюда.')

async def get_callback_question(callback, pool):
    try:
        qid = int(callback.data.split(':', 1)[1])
    except (IndexError, ValueError):
        await callback.answer('Некорректная кнопка.', show_alert=True)
        return None
    q = await db.question(pool, qid, callback.from_user.id)
    if not q:
        await callback.answer('Вопрос не найден.', show_alert=True)
    return q

@r.callback_query(F.data.startswith(('answer:', 'reply_pm:', 'reply_ch:')))
async def answer_start(callback: CallbackQuery, state: FSMContext, pool):
    q = await get_callback_question(callback, pool)
    if not q: return
    if q.get('skipped'):
        await callback.answer('Сначала верните вопрос в новые через список пропущенных.', show_alert=True); return
    if q['legacy_answered']:
        await callback.answer('В старой версии на этот вопрос уже ответили; содержимое ответа не сохранилось.', show_alert=True)
        return
    if q['answer_kind']:
        await callback.answer('Ответ уже подготовлен.', show_alert=True)
        await callback.message.answer('Вы можете повторить доставку или опубликовать ответ.', reply_markup=answer_keyboard(q['id'], q['answer_delivery_status'] != 'sent'))
        return
    await state.set_state(Flow.answer)
    await state.update_data(question_id=q['id'])
    await callback.answer()
    await callback.message.answer(f'Напишите ответ на вопрос #{q["id"]}: текст, фото, видео или голосовое. Ответ будет отправлен лично; затем можно будет опубликовать его в канале. /cancel — отмена.')

@r.callback_query(F.data.startswith('ban:'))
async def ban(callback: CallbackQuery, pool):
    q = await get_callback_question(callback, pool)
    if not q: return
    is_now_banned = await db.toggle_ban(pool, callback.from_user.id, q['asker_id'])
    await callback.answer('Отправитель заблокирован.' if is_now_banned else 'Отправитель разблокирован.', show_alert=True)

async def deliver_answer(bot, pool, qid, uid):
    q = await db.claim_answer(pool, qid, uid)
    if not q:
        return 'unavailable'
    try:
        await send_item(bot, q['asker_id'], q['answer_kind'], q['answer_file_id'], q['answer_body'] or '', f'💬 Ответ на ваш вопрос #{qid}')
    except TelegramAPIError:
        await pool.execute("UPDATE questions SET answer_delivery_status='failed',answer_claimed_at=NULL WHERE id=$1 AND creator_id=$2", qid, uid)
        return 'failed'
    await pool.execute("UPDATE questions SET answer_delivery_status='sent',answer_claimed_at=NULL WHERE id=$1 AND creator_id=$2", qid, uid)
    return 'sent'

@r.message(Flow.answer)
async def answer(message: Message, state: FSMContext, bot: Bot, pool):
    data = payload(message)
    if not data:
        await message.answer('Отправьте текст до 3000 символов, фото, видео или голосовое с подписью до 800 символов. /cancel — отмена.')
        return
    qid = (await state.get_data()).get('question_id')
    await state.clear()
    if not qid:
        await message.answer('Действие устарело. Нажмите «Ответить» ещё раз.')
        return
    q = await db.save_answer(pool, qid, message.from_user.id, *data)
    if not q:
        await message.answer('Вопрос не найден или ответ уже подготовлен.')
        return
    status = await deliver_answer(bot, pool, qid, message.from_user.id)
    result = '✅ Ответ отправлен автору вопроса.' if status == 'sent' else '⚠️ Ответ сохранён, но доставка не подтверждена. Можно повторить.'
    await message.answer(result + '\n\nПри желании опубликуйте вопрос и ответ в подключённом канале.', reply_markup=answer_keyboard(qid, status != 'sent'))

@r.callback_query(F.data.startswith('deliver:'))
async def retry_delivery(callback: CallbackQuery, bot: Bot, pool):
    q = await get_callback_question(callback, pool)
    if not q: return
    await callback.answer()
    status = await deliver_answer(bot, pool, q['id'], callback.from_user.id)
    text = {'sent':'✅ Ответ доставлен.', 'failed':'⚠️ Не удалось доставить ответ: возможно, пользователь заблокировал бота.', 'unavailable':'Ответ уже доставлен или доставка ещё выполняется.'}[status]
    await callback.message.answer(text, reply_markup=answer_keyboard(q['id'], status == 'failed'))

@r.callback_query(F.data.startswith('publish:'))
async def publish(callback: CallbackQuery, bot: Bot, pool):
    q = await get_callback_question(callback, pool)
    if not q: return
    creator = await db.creator(pool, callback.from_user.id)
    if not creator or not creator['channel_id']:
        await callback.answer('Сначала подключите канал через /start.', show_alert=True)
        return
    if q['published_answer_message_id']:
        await callback.answer('Этот ответ уже опубликован.', show_alert=True)
        return
    if q['published_channel_id'] and q['published_channel_id'] != creator['channel_id']:
        await callback.answer('Публикация началась в другом канале. Подключите прежний канал, чтобы завершить её.', show_alert=True)
        return
    try:
        await verify_channel(bot, creator['channel_id'], callback.from_user.id)
    except (TelegramAPIError, ValueError):
        await callback.answer('Не удалось проверить права в канале. Проверьте права бота и свои права.', show_alert=True)
        return
    q = await db.claim_publish(pool, q['id'], callback.from_user.id)
    if not q:
        await callback.answer('Публикация уже завершена или выполняется.', show_alert=True)
        return
    await callback.answer()
    cid, qid = creator['channel_id'], q['id']
    if q['published_channel_id'] and q['published_channel_id'] != cid:
        await pool.execute('UPDATE questions SET publish_claimed_at=NULL WHERE id=$1 AND creator_id=$2', qid, callback.from_user.id)
        await callback.message.answer('Публикация началась в другом канале. Подключите прежний канал.')
        return
    try:
        post = None
        if not q['published_question_message_id'] and q['kind'] == 'text' and q['answer_kind'] == 'text':
            me = await bot.get_me()
            if me.username:
                post = combined_post(q['body'], q['answer_body'], me.username, creator['public_code'])
        if post is not None:
            message = await bot.send_message(
                cid, post, parse_mode='HTML',
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            await pool.execute('''UPDATE questions SET published_channel_id=$3,
                published_question_message_id=$4,published_answer_message_id=$4,
                published_at=now(),publish_claimed_at=NULL WHERE id=$1 AND creator_id=$2''',
                qid, callback.from_user.id, cid, message.message_id)
        else:
            # Preserve media, long messages and publications already started by older code.
            me = await bot.get_me()
            link_markup = (InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Задать анонимный вопрос', url=question_link(me.username, creator['public_code']))]]) if me.username else None)
            footer = question_footer(me.username, creator['public_code']) if me.username else None
            if not q['published_question_message_id']:
                qm = await send_item(bot, cid, q['kind'], q['file_id'], q['body'], f'❓ Анонимный вопрос #{qid}', link_markup, footer=footer)
                await pool.execute('UPDATE questions SET published_channel_id=$3,published_question_message_id=$4 WHERE id=$1 AND creator_id=$2', qid, callback.from_user.id, cid, qm.message_id)
            am = await send_item(bot, cid, q['answer_kind'], q['answer_file_id'], q['answer_body'] or '', f'💬 Ответ на вопрос #{qid}', link_markup)
            await pool.execute('UPDATE questions SET published_answer_message_id=$3,published_at=now(),publish_claimed_at=NULL WHERE id=$1 AND creator_id=$2', qid, callback.from_user.id, am.message_id)
    except TelegramAPIError:
        await pool.execute('UPDATE questions SET publish_claimed_at=NULL WHERE id=$1 AND creator_id=$2', qid, callback.from_user.id)
        await callback.message.answer('⚠️ Не удалось завершить публикацию. Проверьте права бота. Вопрос мог уже появиться в канале; повторная попытка продолжит публикацию.', reply_markup=answer_keyboard(qid))
        return
    await callback.message.answer('✅ Вопрос и ответ опубликованы в канале.')

@r.message()
async def fallback(message: Message):
    await message.answer('Откройте /start, чтобы получить ссылку креатора. Чтобы задать вопрос, перейдите по персональной ссылке креатора.')

async def main():
    token = os.getenv('BOT_TOKEN', '').strip()
    url = os.getenv('DATABASE_URL', '').strip()
    if not token or not url:
        raise SystemExit('BOT_TOKEN and DATABASE_URL are required')
    try:
        rate = int(os.getenv('RATE_LIMIT_SECONDS', '60'))
        if rate < 1: raise ValueError
    except ValueError:
        raise SystemExit('RATE_LIMIT_SECONDS must be a positive integer')
    try:
        raw_ids = os.getenv('ADMIN_IDS', '')
        admin_ids = frozenset(int(x.strip()) for x in raw_ids.split(',') if x.strip())
        if any(uid <= 0 for uid in admin_ids): raise ValueError
    except ValueError:
        raise SystemExit('ADMIN_IDS must contain numeric Telegram user IDs separated by commas')
    pool = await db.create_pool(url)
    bot = Bot(token)
    dp = Dispatcher()
    dp.message.outer_middleware(admin.TrackUsers())
    dp.callback_query.outer_middleware(admin.TrackUsers())
    dp.include_router(admin.router)
    dp.include_router(r)
    broadcast_wakeup = asyncio.Event()
    worker = asyncio.create_task(admin.broadcast_worker(bot, pool, broadcast_wakeup))
    try:
        # Polling and webhook cannot be active together. Do not discard pending updates.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, pool=pool, rate_limit=rate, admin_ids=admin_ids, broadcast_wakeup=broadcast_wakeup, handle_signals=True)
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await bot.session.close()
        await pool.close()

if __name__ == '__main__':
    asyncio.run(main())
