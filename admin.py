"""Owner-only bot administration and durable broadcast queue."""
import asyncio
import logging
import secrets
from datetime import timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

log = logging.getLogger('broadcast')
router = Router()
router.message.filter(F.chat.type == 'private')
router.callback_query.filter(F.message.chat.type == 'private')

class AdminFlow(StatesGroup):
    text = State()
    confirm = State()

def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t,callback_data=d) for t,d in row] for row in rows])

def owner(uid, admin_ids):
    return uid in admin_ids

def menu():
    return kb([('📊 Общая статистика', 'adm:stats')], [('📣 Сервисное уведомление', 'adm:service')], [('💼 Рекламная рассылка', 'adm:ads')], [('📋 Последние рассылки', 'adm:jobs')])

class TrackUsers(BaseMiddleware):
    async def __call__(self, handler: Callable[[Any, dict], Awaitable[Any]], event: Any, data: dict):
        user = getattr(event, 'from_user', None)
        chat = event.chat if isinstance(event, Message) else getattr(getattr(event, 'message', None), 'chat', None)
        if user and chat and chat.type == 'private' and not user.is_bot:
            pool = data['pool']
            await pool.execute('''INSERT INTO bot_users(user_id,first_seen,last_seen,blocked)
                VALUES($1,now(),now(),false) ON CONFLICT(user_id)
                DO UPDATE SET last_seen=now(),blocked=false''', user.id)
        return await handler(event, data)

@router.message(Command('cancel'))
async def cancel_any(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('Действие отменено. /start — открыть кабинет.')

@router.message(Command('myid'))
async def myid(message: Message):
    await message.answer(f'Ваш Telegram ID: {message.from_user.id}. Для настройки владельца добавьте его в ADMIN_IDS в настройках приложения.')

@router.message(Command('admin'))
async def admin_menu(message: Message, state: FSMContext, admin_ids):
    if not owner(message.from_user.id, admin_ids):
        return  # No admin functions or data are disclosed to other users.
    await state.clear()
    await message.answer('🔐 Админка. Выберите действие. /cancel — отменить подготовку рассылки.', reply_markup=menu())

@router.callback_query(F.data == 'adm:stats')
async def stats(callback: CallbackQuery, pool, admin_ids):
    if not owner(callback.from_user.id, admin_ids):
        await callback.answer('Нет доступа.', show_alert=True); return
    row = await pool.fetchrow('''SELECT COUNT(*) AS total,
        COUNT(*) FILTER (WHERE last_seen >= now()-interval '7 days') AS active7,
        COUNT(*) FILTER (WHERE last_seen >= now()-interval '30 days') AS active30,
        COUNT(*) FILTER (WHERE NOT blocked) AS eligible,
        COUNT(*) FILTER (WHERE blocked) AS blocked FROM bot_users''')
    creators = await pool.fetchval('SELECT COUNT(*) FROM creators')
    questions = await pool.fetchval("SELECT COUNT(*) FROM questions WHERE delivery_status='sent'")
    answers = await pool.fetchval("SELECT COUNT(*) FROM questions WHERE answer_delivery_status='sent'")
    await callback.answer()
    await callback.message.answer(f'📊 Статистика\nВсего известных пользователей: {row["total"]}\n'
        f'Активны за 7 / 30 дней: {row["active7"]} / {row["active30"]}\nКреаторов: {creators}\n'
        f'Доставлено вопросов: {questions}\nДоставлено ответов: {answers}\n\n'
        f'Потенциальные получатели рассылок: {row["eligible"]}\n'
        f'Известно о блокировке бота: {row["blocked"]}\n\n'
        'Активность считается с момента установки этой версии; старые пользователи включены в общее число, но дата активности может быть неизвестна.', reply_markup=menu())

@router.callback_query(F.data == 'adm:jobs')
async def recent_jobs(callback: CallbackQuery, pool, admin_ids):
    if not owner(callback.from_user.id, admin_ids):
        await callback.answer('Нет доступа.', show_alert=True); return
    jobs = await pool.fetch('SELECT id,category,status,created_at FROM broadcasts ORDER BY created_at DESC LIMIT 10')
    await callback.answer()
    if not jobs:
        await callback.message.answer('Рассылок пока нет.'); return
    for job in jobs:
        label = 'Реклама' if job['category'] == 'ads' else 'Сервисное уведомление'
        await callback.message.answer(f'{label} · {job["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} · {job["status"]}', reply_markup=job_keyboard(job['id']))

@router.callback_query(F.data.in_({'adm:service', 'adm:ads'}))
async def compose_start(callback: CallbackQuery, state: FSMContext, admin_ids):
    if not owner(callback.from_user.id, admin_ids):
        await callback.answer('Нет доступа.', show_alert=True); return
    category = callback.data.split(':')[1]
    await state.set_state(AdminFlow.text)
    await state.update_data(category=category)
    await callback.answer()
    await callback.message.answer(('Напишите текст рекламной рассылки. Она будет отправлена доступным пользователям бота.' if category=='ads' else
        'Напишите текст сервисного уведомления. Не используйте этот тип для рекламы.') +
        '\n\nТолько текст до 3000 символов. Затем покажу предпросмотр и количество получателей. /cancel — отмена.')

@router.message(AdminFlow.text)
async def compose_text(message: Message, state: FSMContext, pool, admin_ids):
    if not owner(message.from_user.id, admin_ids): return
    text = (message.text or '').strip()
    if not text or len(text.encode('utf-16-le')) // 2 > 3000:
        await message.answer('Нужен текст до 3000 символов. /cancel — отмена.'); return
    data = await state.get_data()
    category = data['category']
    count = await pool.fetchval('SELECT COUNT(*) FROM bot_users WHERE NOT blocked')
    job_id = secrets.token_urlsafe(12)
    await state.update_data(body=text, job_id=job_id)
    await state.set_state(AdminFlow.confirm)
    preview = format_message(category, text)
    await message.answer(f'Предпросмотр для {count} получателей (состав может измениться):\n\n{preview}',
        reply_markup=kb([('✅ Запустить рассылку',f'adm:send:{job_id}')], [('❌ Отменить',f'adm:discard:{job_id}')]))

def format_message(category, body):
    return ('📣 Реклама\n\n' if category == 'ads' else '🔔 Уведомление сервиса\n\n') + body + '\n\nЧтобы прекратить сообщения, остановите бота в Telegram.'

@router.callback_query(F.data.startswith(('adm:send:', 'adm:discard:')))
async def confirm(callback: CallbackQuery, state: FSMContext, pool, admin_ids, broadcast_wakeup):
    if not owner(callback.from_user.id, admin_ids):
        await callback.answer('Нет доступа.', show_alert=True); return
    action, job_id = callback.data.split(':')[1:]
    data = await state.get_data()
    if await state.get_state() != AdminFlow.confirm.state or data.get('job_id') != job_id:
        await callback.answer('Предпросмотр устарел. Создайте его заново через /admin.', show_alert=True); return
    if action == 'discard':
        await state.clear(); await callback.answer('Отменено')
        await callback.message.answer('Рассылка отменена.'); return
    category = data['category']
    # Create job + snapshot atomically. Repeated confirmation cannot create a second job.
    async with pool.acquire() as con, con.transaction():
        inserted = await con.fetchval('''INSERT INTO broadcasts(id,admin_id,category,body) VALUES($1,$2,$3,$4)
             ON CONFLICT(id) DO NOTHING RETURNING true''', job_id, callback.from_user.id, category, data['body'])
        if inserted:
            await con.execute('''INSERT INTO broadcast_recipients(broadcast_id,user_id)
                SELECT $1,user_id FROM bot_users WHERE NOT blocked''', job_id)
        count = await con.fetchval('SELECT COUNT(*) FROM broadcast_recipients WHERE broadcast_id=$1', job_id)
    broadcast_wakeup.set()
    await state.clear()
    await callback.answer('Добавлено в очередь')
    await callback.message.answer(f'Рассылка добавлена в очередь. Получателей: {count}.', reply_markup=job_keyboard(job_id))

def job_keyboard(job_id):
    return kb([('📊 Статус',f'adm:status:{job_id}'),('⏹ Остановить',f'adm:stop:{job_id}')])

@router.callback_query(F.data.startswith(('adm:status:', 'adm:stop:')))
async def manage_job(callback: CallbackQuery, pool, admin_ids):
    if not owner(callback.from_user.id, admin_ids):
        await callback.answer('Нет доступа.', show_alert=True); return
    action, job_id = callback.data.split(':')[1:]
    job = await pool.fetchrow('SELECT * FROM broadcasts WHERE id=$1', job_id)
    if not job:
        await callback.answer('Рассылка не найдена.', show_alert=True); return
    if action=='stop':
        changed = await pool.fetchval("UPDATE broadcasts SET status='cancelled',completed_at=now() WHERE id=$1 AND status IN ('queued','running') RETURNING true", job_id)
        await callback.answer('Остановлена' if changed else 'Уже завершена')
    else:
        await callback.answer()
    job = await pool.fetchrow('SELECT * FROM broadcasts WHERE id=$1',job_id)
    counts = await pool.fetch('SELECT status,COUNT(*) AS n FROM broadcast_recipients WHERE broadcast_id=$1 GROUP BY status',job_id)
    c = {row['status']:row['n'] for row in counts}
    await callback.message.answer(f'Статус: {job["status"]}\nОтправлено: {c.get("sent",0)}\nОшибки: {c.get("failed",0)}\n'
        f'Пропущено: {c.get("skipped",0)}\nВ очереди: {c.get("pending",0)}\n'
        f'В процессе: {c.get("sending",0)}\nРезультат неизвестен: {c.get("unknown",0)}\n'
        'При остановке сообщение, уже отправляемое Telegram, может всё же прийти.', reply_markup=job_keyboard(job_id))

async def recover(pool):
    # Never automatically retry an uncertain send after process death; it may already have succeeded.
    await pool.execute("UPDATE broadcast_recipients SET status='unknown' WHERE status='sending'")

async def next_recipient(pool):
    async with pool.acquire() as con, con.transaction():
        job = await con.fetchrow("SELECT * FROM broadcasts WHERE status IN ('queued','running') ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1")
        if not job: return None
        await con.execute("UPDATE broadcasts SET status='running' WHERE id=$1 AND status='queued'",job['id'])
        recipient = await con.fetchrow("SELECT * FROM broadcast_recipients WHERE broadcast_id=$1 AND status='pending' ORDER BY user_id LIMIT 1 FOR UPDATE SKIP LOCKED", job['id'])
        if not recipient:
            await con.execute("UPDATE broadcasts SET status='completed',completed_at=now() WHERE id=$1 AND status='running'",job['id'])
            return None
        u = await con.fetchrow('SELECT blocked FROM bot_users WHERE user_id=$1', recipient['user_id'])
        eligible = u and not u['blocked']
        if not eligible:
            await con.execute("UPDATE broadcast_recipients SET status='skipped' WHERE broadcast_id=$1 AND user_id=$2",job['id'],recipient['user_id'])
            return None
        await con.execute("UPDATE broadcast_recipients SET status='sending' WHERE broadcast_id=$1 AND user_id=$2",job['id'],recipient['user_id'])
        return job, recipient['user_id']

async def broadcast_worker(bot: Bot, pool, broadcast_wakeup: asyncio.Event):
    """Drain durable queue on startup or explicit wake-up; never poll an empty DB."""
    needs_recovery = True
    broadcast_wakeup.set()
    while True:
        await broadcast_wakeup.wait()
        broadcast_wakeup.clear()
        try:
            if needs_recovery:
                await recover(pool)
                needs_recovery = False
            while True:
                item = await next_recipient(pool)
                if not item:
                    # next_recipient may also have just skipped a user or completed a job.
                    remaining = await pool.fetchval("SELECT EXISTS(SELECT 1 FROM broadcasts WHERE status IN ('queued','running'))")
                    if remaining:
                        continue
                    break
                job, uid = item
                eligible = await pool.fetchval('''SELECT (b.status='running' AND NOT u.blocked)
                     FROM broadcasts b CROSS JOIN bot_users u WHERE b.id=$1 AND u.user_id=$2''',job['id'],uid)
                if not eligible:
                    await pool.execute("UPDATE broadcast_recipients SET status='skipped' WHERE broadcast_id=$1 AND user_id=$2",job['id'],uid)
                    continue
                try:
                    await bot.send_message(uid,format_message(job['category'],job['body']))
                except TelegramRetryAfter as e:
                    await pool.execute("UPDATE broadcast_recipients SET status='pending' WHERE broadcast_id=$1 AND user_id=$2",job['id'],uid)
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                except TelegramForbiddenError:
                    async with pool.acquire() as con, con.transaction():
                        await con.execute('UPDATE bot_users SET blocked=true WHERE user_id=$1',uid)
                        await con.execute("UPDATE broadcast_recipients SET status='failed' WHERE broadcast_id=$1 AND user_id=$2",job['id'],uid)
                except TelegramBadRequest:
                    await pool.execute("UPDATE broadcast_recipients SET status='failed' WHERE broadcast_id=$1 AND user_id=$2",job['id'],uid)
                except TelegramAPIError:
                    await pool.execute("UPDATE broadcast_recipients SET status='unknown' WHERE broadcast_id=$1 AND user_id=$2",job['id'],uid)
                else:
                    await pool.execute("UPDATE broadcast_recipients SET status='sent' WHERE broadcast_id=$1 AND user_id=$2",job['id'],uid)
                await asyncio.sleep(0.07)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning('Broadcast worker error: %s', type(e).__name__)
            needs_recovery = True
            broadcast_wakeup.set()
            await asyncio.sleep(10)
