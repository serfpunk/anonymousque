"""
Мультипользовательский Telegram-бот для анонимных вопросов (aiogram 3.x).

Точка входа для App Platform: main.py
Токен задаётся строго через переменную окружения BOT_TOKEN.
"""

import asyncio
import html
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
)

# --- Настройки (берутся из переменных окружения App Platform) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "anon_qa_multi.db")
CAPTION_LIMIT = 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

router = Router()


# --- Модели ---
@dataclass
class UserProfile:
    user_id: int
    public_code: str
    channel_id: int | None
    channel_title: str | None
    plan: str  # "free" | "pro"
    plan_expires_at: str | None


@dataclass
class QuestionRecord:
    id: int
    asker_id: int
    recipient_id: int
    media_type: str  # "text" | "photo" | "voice"
    file_id: str | None
    text: str


# --- База данных (SQLite) ---
def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                public_code TEXT UNIQUE NOT NULL,
                channel_id INTEGER,
                channel_title TEXT,
                plan TEXT NOT NULL DEFAULT 'free',
                plan_expires_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asker_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'text',
                file_id TEXT,
                text TEXT NOT NULL DEFAULT '',
                answered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bans (
                recipient_id INTEGER NOT NULL,
                banned_user_id INTEGER NOT NULL,
                PRIMARY KEY (recipient_id, banned_user_id)
            )
            """
        )


def _row_to_user(row: tuple | None) -> UserProfile | None:
    if not row:
        return None
    return UserProfile(
        user_id=int(row[0]),
        public_code=str(row[1]),
        channel_id=int(row[2]) if row[2] is not None else None,
        channel_title=str(row[3]) if row[3] is not None else None,
        plan=str(row[4] or "free"),
        plan_expires_at=row[5],
    )


def get_or_create_user(user_id: int) -> UserProfile:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_id, public_code, channel_id, channel_title, plan, plan_expires_at "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            return _row_to_user(row)  # type: ignore[return-value]

        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        conn.execute(
            "INSERT INTO users (user_id, public_code, created_at) VALUES (?, ?, ?)",
            (user_id, code, datetime.now(timezone.utc).isoformat()),
        )
        return UserProfile(
            user_id=user_id,
            public_code=code,
            channel_id=None,
            channel_title=None,
            plan="free",
            plan_expires_at=None,
        )


def get_user_by_code(public_code: str) -> UserProfile | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_id, public_code, channel_id, channel_title, plan, plan_expires_at "
            "FROM users WHERE public_code = ?",
            (public_code,),
        ).fetchone()
        return _row_to_user(row)


def set_user_channel(user_id: int, channel_id: int | None, channel_title: str | None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET channel_id = ?, channel_title = ? WHERE user_id = ?",
            (channel_id, channel_title, user_id),
        )


def is_pro(user: UserProfile) -> bool:
    """Точка расширения для монетизации (подписка Pro через Telegram Stars / ЮKassa и т.д.)."""
    if user.plan != "pro":
        return False
    if not user.plan_expires_at:
        return True
    try:
        return datetime.fromisoformat(user.plan_expires_at) > datetime.now(timezone.utc)
    except ValueError:
        return False


def get_user_stats(user_id: int) -> tuple[int, int]:
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE recipient_id = ?", (user_id,)
        ).fetchone()[0]
        answered = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE recipient_id = ? AND answered = 1", (user_id,)
        ).fetchone()[0]
        return int(total), int(answered)


def save_question(
    asker_id: int,
    recipient_id: int,
    media_type: str,
    text: str = "",
    file_id: str | None = None,
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO questions (asker_id, recipient_id, media_type, file_id, text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                asker_id,
                recipient_id,
                media_type,
                file_id,
                text,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return int(cur.lastrowid)


def get_question(question_id: int) -> QuestionRecord | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id, asker_id, recipient_id, media_type, file_id, text "
            "FROM questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if not row:
            return None
        return QuestionRecord(
            id=int(row[0]),
            asker_id=int(row[1]),
            recipient_id=int(row[2]),
            media_type=str(row[3] or "text"),
            file_id=row[4],
            text=str(row[5] or ""),
        )


def mark_question_answered(question_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE questions SET answered = 1 WHERE id = ?", (question_id,))


def is_banned(recipient_id: int, asker_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM bans WHERE recipient_id = ? AND banned_user_id = ?",
            (recipient_id, asker_id),
        ).fetchone()
        return row is not None


def set_banned(recipient_id: int, asker_id: int, ban: bool) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        if ban:
            conn.execute(
                "INSERT OR IGNORE INTO bans (recipient_id, banned_user_id) VALUES (?, ?)",
                (recipient_id, asker_id),
            )
        else:
            conn.execute(
                "DELETE FROM bans WHERE recipient_id = ? AND banned_user_id = ?",
                (recipient_id, asker_id),
            )


# --- FSM-состояния ---
class AskState(StatesGroup):
    waiting_question = State()


class ChannelState(StatesGroup):
    waiting_channel = State()


class ReplyState(StatesGroup):
    waiting_channel_reply = State()
    waiting_private_reply = State()


# --- Вспомогательные функции ---
def extract_payload(message: Message) -> tuple[str, str | None, str] | None:
    if message.photo:
        return ("photo", message.photo[-1].file_id, (message.caption or "").strip())
    if message.voice:
        return ("voice", message.voice.file_id, (message.caption or "").strip())
    if message.text:
        text = message.text.strip()
        if text:
            return ("text", None, text)
    return None


async def send_media_or_text(
    bot: Bot,
    chat_id: int | str,
    media_type: str,
    file_id: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    reply_to_message_id: int | None = None,
) -> Message:
    if media_type == "photo" and file_id:
        if len(text) <= CAPTION_LIMIT:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=file_id,
                caption=text or None,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        media_msg = await bot.send_photo(
            chat_id=chat_id, photo=file_id, reply_to_message_id=reply_to_message_id
        )
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            reply_to_message_id=media_msg.message_id,
        )

    if media_type == "voice" and file_id:
        if len(text) <= CAPTION_LIMIT:
            return await bot.send_voice(
                chat_id=chat_id,
                voice=file_id,
                caption=text or None,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )
        media_msg = await bot.send_voice(
            chat_id=chat_id, voice=file_id, reply_to_message_id=reply_to_message_id
        )
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            reply_to_message_id=media_msg.message_id,
        )

    return await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to_message_id,
    )


def format_question_block(record: QuestionRecord) -> str:
    label_map = {
        "text": "❓ <b>Анонимный вопрос:</b>",
        "photo": "❓ <b>Анонимный вопрос (фото):</b>",
        "voice": "❓ <b>Анонимный вопрос (голосовое):</b>",
    }
    header = label_map.get(record.media_type, "❓ <b>Анонимный вопрос:</b>")
    if record.text:
        return f"{header}\n<blockquote>{html.escape(record.text)}</blockquote>"
    return header


def cabinet_keyboard(user: UserProfile) -> InlineKeyboardMarkup:
    channel_btn = (
        InlineKeyboardButton(text="🔄 Сменить / отвязать канал", callback_data="menu:channel")
        if user.channel_id
        else InlineKeyboardButton(text="📢 Привязать канал", callback_data="menu:channel")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [channel_btn],
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
                InlineKeyboardButton(text="⭐ Тариф", callback_data="menu:plan"),
            ],
        ]
    )


def question_keyboard(
    question_id: int, has_channel: bool, banned: bool = False
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_channel:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📢 Ответить и в канал", callback_data=f"reply_ch:{question_id}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="💬 Ответить лично", callback_data=f"reply_pm:{question_id}"
            )
        ]
    )
    ban_btn = (
        InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"unban:{question_id}")
        if banned
        else InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"ban:{question_id}")
    )
    rows.append([ban_btn])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_cabinet(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    user = get_or_create_user(message.from_user.id)
    me = await bot.get_me()
    personal_link = f"https://t.me/{me.username}?start={user.public_code}"
    channel_status = (
        f"<b>{html.escape(user.channel_title or str(user.channel_id))}</b>"
        if user.channel_id
        else "не привязан"
    )
    plan_label = "⭐ Pro" if is_pro(user) else "Free"

    await message.answer(
        f"👋 <b>Ваш кабинет анонимных вопросов</b>\n\n"
        f"🔗 <b>Ссылка для подписчиков:</b>\n<code>{personal_link}</code>\n\n"
        f"📢 <b>Канал для публикаций:</b> {channel_status}\n"
        f"💎 <b>Тариф:</b> {plan_label}\n\n"
        f"Разместите ссылку в описании канала или в посте — вопросы (текст, фото, голосовые) "
        f"будут приходить сюда.",
        reply_markup=cabinet_keyboard(user),
    )


# --- Команды ---
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Действие отменено.")


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep_link(
    message: Message, command: CommandObject, state: FSMContext, bot: Bot
) -> None:
    if not message.from_user:
        return

    code = (command.args or "").strip()
    recipient = get_user_by_code(code)
    if not recipient:
        await message.answer("Ссылка устарела или недействительна. Откройте /start, чтобы создать свою.")
        return

    if is_banned(recipient.user_id, message.from_user.id):
        await message.answer("Вы не можете отправлять вопросы этому автору.")
        return

    await state.set_state(AskState.waiting_question)
    await state.update_data(recipient_id=recipient.user_id)

    target_hint = (
        f"для канала «<b>{html.escape(recipient.channel_title)}</b>»"
        if recipient.channel_title
        else "автору"
    )
    await message.answer(
        f"✍️ Напишите анонимный вопрос {target_hint}.\n"
        f"Можно отправить <b>текст, фото или голосовое сообщение</b>.\n\n"
        f"Для отмены отправьте /cancel."
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await show_cabinet(message, bot)


# --- Меню кабинета и привязка канала ---
@router.callback_query(F.data.startswith("menu:"))
async def handle_menu_callbacks(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return

    user = get_or_create_user(callback.from_user.id)
    action = (callback.data or "").split(":", 1)[1]

    if action == "channel":
        await state.set_state(ChannelState.waiting_channel)
        kb = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отвязать текущий канал", callback_data="ch:unlink")]
                ]
            )
            if user.channel_id
            else None
        )
        await callback.message.answer(
            "📢 <b>Привязка канала:</b>\n"
            "1. Добавьте этого бота администратором в ваш канал (право на публикацию сообщений).\n"
            "2. Перешлите сюда любой пост из канала <b>или</b> отправьте его @username.\n\n"
            "Для отмены отправьте /cancel.",
            reply_markup=kb,
        )
        await callback.answer()
    elif action == "stats":
        total, answered = get_user_stats(user.user_id)
        await callback.message.answer(
            f"📊 <b>Ваша статистика:</b>\n"
            f"• Получено вопросов: <b>{total}</b>\n"
            f"• Отвечено: <b>{answered}</b>"
        )
        await callback.answer()
    elif action == "plan":
        if is_pro(user):
            await callback.message.answer(
                "⭐ У вас активен тариф <b>Pro</b>: публикации в канале идут без подписи бота."
            )
        else:
            await callback.message.answer(
                "💎 Текущий тариф: <b>Free</b>.\n\n"
                "В бесплатной версии к постам в канале добавляется ссылка на бота, "
                "а в будущей версии <b>Pro</b> можно будет убрать брендинг бота и подключать "
                "дополнительные функции (оплата через Telegram Stars / внешнюю кассу)."
            )
        await callback.answer()


@router.callback_query(F.data == "ch:unlink")
async def handle_unlink_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return
    await state.clear()
    set_user_channel(callback.from_user.id, None, None)
    await callback.message.answer("✅ Канал отвязан. Вы по-прежнему можете принимать вопросы и отвечать лично.")
    await callback.answer()


@router.message(ChannelState.waiting_channel)
async def process_channel_link(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return

    chat_ref: int | str | None = None
    if isinstance(message.forward_origin, MessageOriginChannel):
        chat_ref = message.forward_origin.chat.id
    elif message.text:
        raw = message.text.strip()
        if raw.startswith("@") or raw.lstrip("-").isdigit():
            chat_ref = int(raw) if raw.lstrip("-").isdigit() else raw

    if chat_ref is None:
        await message.answer(
            "Перешлите пост из канала или отправьте его @username / ID (или /cancel для отмены)."
        )
        return

    try:
        chat = await bot.get_chat(chat_ref)
        if chat.type != "channel":
            await message.answer("Это не канал. Пожалуйста, укажите именно Telegram-канал.")
            return

        user_member = await bot.get_chat_member(chat.id, message.from_user.id)
        if user_member.status not in ("creator", "administrator"):
            await message.answer("Вы должны быть администратором или владельцем этого канала.")
            return

        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat.id, me.id)
        if bot_member.status != "administrator" or not getattr(bot_member, "can_post_messages", False):
            await message.answer(
                "Бот не имеет прав на публикацию в этом канале. "
                "Добавьте бота в администраторы канала и выдайте право «Публикация сообщений»."
            )
            return
    except Exception as exc:
        logger.warning("Ошибка проверки канала: %s", exc)
        await message.answer(
            "Не удалось проверить канал. Убедитесь, что бот добавлен туда администратором, и попробуйте снова."
        )
        return

    title = chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
    set_user_channel(message.from_user.id, chat.id, title)
    await state.clear()
    await message.answer(
        f"✅ Канал «<b>{html.escape(title)}</b>» привязан! "
        f"Теперь под новыми вопросами появится кнопка публикации ответа в канал."
    )


# --- Отправка анонимного вопроса ---
@router.message(AskState.waiting_question, F.text | F.photo | F.voice)
async def process_user_question(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return

    payload = extract_payload(message)
    if not payload:
        await message.answer("Отправьте текст, фото или голосовое сообщение (или /cancel).")
        return

    data = await state.get_data()
    recipient_id: int = data["recipient_id"]
    await state.clear()

    if is_banned(recipient_id, message.from_user.id):
        await message.answer("Вы не можете отправлять вопросы этому автору.")
        return

    recipient = get_or_create_user(recipient_id)
    media_type, file_id, question_text = payload
    question_id = save_question(
        asker_id=message.from_user.id,
        recipient_id=recipient_id,
        media_type=media_type,
        text=question_text,
        file_id=file_id,
    )

    recipient_text = f"📩 <b>Новый анонимный вопрос #{question_id}</b>"
    if question_text:
        recipient_text += f"\n\n{html.escape(question_text)}"

    try:
        await send_media_or_text(
            bot=bot,
            chat_id=recipient_id,
            media_type=media_type,
            file_id=file_id,
            text=recipient_text,
            reply_markup=question_keyboard(
                question_id=question_id,
                has_channel=recipient.channel_id is not None,
            ),
        )
    except Exception:
        await message.answer("Не удалось доставить вопрос автору (возможно, он остановил бота).")
        return

    await message.answer(
        "✅ Вопрос отправлен анонимно! Чтобы создать собственную ссылку для вопросов, нажмите /start."
    )


@router.message(AskState.waiting_question)
async def unsupported_question_type(message: Message) -> None:
    await message.answer(
        "Сейчас можно отправлять только текст, фото и голосовые сообщения (или /cancel)."
    )


# --- Кнопки под вопросом у получателя ---
@router.callback_query(F.data.startswith(("reply_ch:", "reply_pm:", "ban:", "unban:")))
async def handle_question_callbacks(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user or not callback.message:
        return

    action, raw_id = (callback.data or "").split(":", 1)
    question_id = int(raw_id)
    record = get_question(question_id)

    if not record or record.recipient_id != callback.from_user.id:
        await callback.answer("Вопрос не найден.", show_alert=True)
        return

    recipient = get_or_create_user(callback.from_user.id)

    if action == "reply_ch":
        if not recipient.channel_id:
            await callback.answer("Сначала привяжите канал в меню /start.", show_alert=True)
            return
        await state.set_state(ReplyState.waiting_channel_reply)
        await state.update_data(question_id=question_id)
        await callback.message.answer(
            f"✍️ Отправьте ответ на вопрос #{question_id} (текст, фото или голосовое) — "
            "он будет опубликован в вашем канале.\nДля отмены отправьте /cancel."
        )
        await callback.answer()
    elif action == "reply_pm":
        await state.set_state(ReplyState.waiting_private_reply)
        await state.update_data(question_id=question_id)
        await callback.message.answer(
            f"✍️ Отправьте личный ответ на вопрос #{question_id} (текст, фото или голосовое).\n"
            "Для отмены отправьте /cancel."
        )
        await callback.answer()
    elif action == "ban":
        set_banned(recipient.user_id, record.asker_id, True)
        await callback.message.edit_reply_markup(
            reply_markup=question_keyboard(
                question_id, has_channel=recipient.channel_id is not None, banned=True
            )
        )
        await callback.answer("Отправитель заблокирован для ваших вопросов.")
    elif action == "unban":
        set_banned(recipient.user_id, record.asker_id, False)
        await callback.message.edit_reply_markup(
            reply_markup=question_keyboard(
                question_id, has_channel=recipient.channel_id is not None, banned=False
            )
        )
        await callback.answer("Отправитель разблокирован.")


# --- Обработка ответов владельца канала ---
@router.message(ReplyState.waiting_channel_reply, F.text | F.photo | F.voice)
async def process_channel_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return

    payload = extract_payload(message)
    if not payload:
        await message.answer("Отправьте текст, фото или голосовое сообщение (или /cancel).")
        return

    reply_type, reply_file_id, reply_text = payload
    data = await state.get_data()
    question_id: int = data["question_id"]
    record = get_question(question_id)
    await state.clear()

    if not record or record.recipient_id != message.from_user.id:
        await message.answer("Не удалось найти исходный вопрос.")
        return

    recipient = get_or_create_user(message.from_user.id)
    if not recipient.channel_id:
        await message.answer("Канал не привязан. Привяжите его в меню /start.")
        return

    me = await bot.get_me()
    ask_link = f"https://t.me/{me.username}?start={recipient.public_code}"
    footer = (
        ""
        if is_pro(recipient)
        else f'\n\n<i><a href="{ask_link}">Задать анонимный вопрос</a></i>'
    )
    q_block = format_question_block(record)
    answer_block = (
        f"💬 <b>Ответ:</b>\n{html.escape(reply_text)}" if reply_text else "💬 <b>Ответ:</b>"
    )

    try:
        if record.media_type == "text" and reply_type == "text":
            await bot.send_message(
                chat_id=recipient.channel_id,
                text=f"{q_block}\n\n{answer_block}{footer}",
                disable_web_page_preview=True,
            )
        elif record.media_type == "text" and reply_type in ("photo", "voice"):
            await send_media_or_text(
                bot=bot,
                chat_id=recipient.channel_id,
                media_type=reply_type,
                file_id=reply_file_id,
                text=f"{q_block}\n\n{answer_block}{footer}",
            )
        elif record.media_type in ("photo", "voice") and reply_type == "text":
            await send_media_or_text(
                bot=bot,
                chat_id=recipient.channel_id,
                media_type=record.media_type,
                file_id=record.file_id,
                text=f"{q_block}\n\n{answer_block}{footer}",
            )
        else:
            q_msg = await send_media_or_text(
                bot=bot,
                chat_id=recipient.channel_id,
                media_type=record.media_type,
                file_id=record.file_id,
                text=q_block,
            )
            await send_media_or_text(
                bot=bot,
                chat_id=recipient.channel_id,
                media_type=reply_type,
                file_id=reply_file_id,
                text=f"{answer_block}{footer}",
                reply_to_message_id=q_msg.message_id,
            )
    except Exception as exc:
        logger.exception("Ошибка публикации в канал")
        await message.answer(
            f"Не удалось опубликовать в канал ({html.escape(str(exc))}). "
            "Проверьте, что бот всё ещё администратор канала."
        )
        return

    mark_question_answered(question_id)

    asker_caption = "📩 <b>Автор ответил на твой вопрос и опубликовал его в канале!</b>"
    if record.text:
        asker_caption += f"\n\n<i>Твой вопрос:</i> {html.escape(record.text)}"
    if reply_text:
        asker_caption += f"\n<i>Ответ:</i> {html.escape(reply_text)}"

    try:
        await send_media_or_text(
            bot=bot,
            chat_id=record.asker_id,
            media_type=reply_type,
            file_id=reply_file_id,
            text=asker_caption,
        )
    except Exception:
        pass

    await message.answer(f"✅ Вопрос #{question_id} и ваш ответ опубликованы в канале.")


@router.message(ReplyState.waiting_private_reply, F.text | F.photo | F.voice)
async def process_private_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return

    payload = extract_payload(message)
    if not payload:
        await message.answer("Отправьте текст, фото или голосовое сообщение (или /cancel).")
        return

    reply_type, reply_file_id, reply_text = payload
    data = await state.get_data()
    question_id: int = data["question_id"]
    record = get_question(question_id)
    await state.clear()

    if not record or record.recipient_id != message.from_user.id:
        await message.answer("Не удалось найти исходный вопрос.")
        return

    asker_caption = "💬 <b>Личный ответ на твой анонимный вопрос:</b>"
    if record.text:
        asker_caption += f"\n\n<i>Твой вопрос:</i> {html.escape(record.text)}"
    if reply_text:
        asker_caption += f"\n<i>Ответ:</i> {html.escape(reply_text)}"

    try:
        await send_media_or_text(
            bot=bot,
            chat_id=record.asker_id,
            media_type=reply_type,
            file_id=reply_file_id,
            text=asker_caption,
        )
        mark_question_answered(question_id)
        await message.answer(f"✅ Личный ответ на вопрос #{question_id} отправлен автору.")
    except Exception:
        await message.answer("Не удалось доставить ответ (возможно, пользователь заблокировал бота).")


@router.message(ReplyState.waiting_channel_reply)
@router.message(ReplyState.waiting_private_reply)
async def unsupported_reply_type(message: Message) -> None:
    await message.answer(
        "Поддерживаются только текст, фото и голосовые сообщения. Попробуйте снова или отправьте /cancel."
    )


@router.message()
async def fallback_handler(message: Message, bot: Bot) -> None:
    await message.answer(
        "Чтобы открыть свой кабинет и получить ссылку для анонимных вопросов, нажмите /start.\n"
        "Чтобы задать вопрос автору канала, перейдите по его персональной ссылке."
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задана переменная окружения BOT_TOKEN. Добавьте её в настройках App Platform."
        )

    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    logger.info("Мультипользовательский бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
