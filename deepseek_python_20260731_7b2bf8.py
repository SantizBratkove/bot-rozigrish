import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ChatInviteLink,
)
import aiosqlite
import json
import html

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = "8609184511:AAEFlUtUkakJNqAOzOEmeUSmnCrtlwpZiWA"
ADMIN_IDS = [8820552100, 6697920367]
DATABASE_NAME = "giveaway_bot.db"

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

db: aiosqlite.Connection = None

# ------------------------ Инициализация БД ------------------------
async def init_db():
    global db
    db = await aiosqlite.connect(DATABASE_NAME)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            referrer_id INTEGER,
            subscribed INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            invited_by INTEGER
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS contests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            channel_id INTEGER,
            post_id INTEGER,
            text TEXT,
            photo TEXT,
            target INTEGER DEFAULT 0,
            duration INTEGER,
            winners_count INTEGER DEFAULT 1,
            end_type TEXT,
            end_value TEXT,
            status TEXT DEFAULT 'created',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,
            scheduled_at TIMESTAMP,
            required_channels TEXT DEFAULT '[]',
            timer_message_id INTEGER,
            last_bid_user_id INTEGER,
            last_bid_username TEXT,
            last_bid_msg_id INTEGER,
            bids_count INTEGER DEFAULT 0,
            required_invites INTEGER DEFAULT 0,
            comment_timeout INTEGER DEFAULT 0,
            discussion_chat_id INTEGER
        )
    """)
    try:
        await db.execute("ALTER TABLE contests ADD COLUMN discussion_chat_id INTEGER")
    except:
        pass
    await db.execute("""
        CREATE TABLE IF NOT EXISTS contest_participants (
            contest_id INTEGER,
            user_id INTEGER,
            invited INTEGER DEFAULT 0,
            qualified INTEGER DEFAULT 0,
            PRIMARY KEY (contest_id, user_id)
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS required_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER,
            username TEXT,
            title TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS bot_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER,
            username TEXT,
            title TEXT,
            type TEXT
        )
    """)
    await db.commit()

async def get_db() -> aiosqlite.Connection:
    return db

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ------------------------ Работа с пользователями ------------------------
async def add_user(user_id: int, username: str, first_name: str, referrer_id: Optional[int] = None):
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, referrer_id) VALUES (?, ?, ?, ?)",
        (user_id, username, first_name, referrer_id)
    )
    await db.commit()

# ------------------------ Работа с конкурсами ------------------------
async def get_contests(admin_id: int) -> list:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM contests ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    return rows

async def get_contest(contest_id: int) -> Optional[Dict]:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM contests WHERE id=?", (contest_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    cols = [desc[0] for desc in cursor.description]
    return dict(zip(cols, row))

async def update_contest(contest_id: int, **kwargs):
    db = await get_db()
    set_clause = ", ".join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values())
    values.append(contest_id)
    await db.execute(f"UPDATE contests SET {set_clause} WHERE id=?", values)
    await db.commit()

async def add_participant(contest_id: int, user_id: int):
    db = await get_db()
    await db.execute("INSERT OR IGNORE INTO contest_participants (contest_id, user_id) VALUES (?, ?)", (contest_id, user_id))
    await db.commit()

async def increment_invite(contest_id: int, user_id: int) -> int:
    db = await get_db()
    await db.execute(
        "UPDATE contest_participants SET invited = invited + 1 WHERE contest_id=? AND user_id=?",
        (contest_id, user_id)
    )
    cursor = await db.execute("SELECT invited FROM contest_participants WHERE contest_id=? AND user_id=?", (contest_id, user_id))
    row = await cursor.fetchone()
    await db.commit()
    return row[0] if row else 0

async def qualify_participant(contest_id: int, user_id: int):
    db = await get_db()
    await db.execute(
        "UPDATE contest_participants SET qualified=1 WHERE contest_id=? AND user_id=?",
        (contest_id, user_id)
    )
    await db.commit()

async def get_qualified_participants(contest_id: int) -> list:
    db = await get_db()
    cursor = await db.execute(
        "SELECT user_id FROM contest_participants WHERE contest_id=? AND qualified=1",
        (contest_id,)
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]

async def get_last_bid(contest_id: int) -> Optional[Tuple[int, str]]:
    contest = await get_contest(contest_id)
    if contest:
        return contest.get("last_bid_user_id"), contest.get("last_bid_username")
    return None, None

# ------------------------ Каналы и подписки ------------------------
async def add_bot_channel(channel_id: int, username: str, title: str, ctype: str):
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO bot_channels (channel_id, username, title, type) VALUES (?, ?, ?, ?)",
        (channel_id, username, title, ctype)
    )
    await db.commit()

async def remove_bot_channel(channel_id: int):
    db = await get_db()
    await db.execute("DELETE FROM bot_channels WHERE channel_id=?", (channel_id,))
    await db.commit()

async def get_bot_channels() -> list:
    db = await get_db()
    cursor = await db.execute("SELECT * FROM bot_channels")
    rows = await cursor.fetchall()
    return rows

async def get_required_channels(contest_id: int) -> list:
    contest = await get_contest(contest_id)
    if contest and contest["required_channels"]:
        return json.loads(contest["required_channels"])
    return []

async def is_subscribed(user_id: int, channel_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except Exception as e:
        logger.error(f"Ошибка проверки подписки на канал {channel_id}: {e}")
        return False

async def check_all_subscriptions(user_id: int, channel_ids: list) -> bool:
    for cid in channel_ids:
        if not await is_subscribed(user_id, cid):
            return False
    return True

async def get_channel_link(ch_id: int, ch_username: str = None) -> str:
    """Возвращает invite-ссылку или публичную ссылку на канал."""
    try:
        invite = await bot.export_chat_invite_link(chat_id=ch_id)
        return invite
    except Exception:
        pass
    if ch_username:
        return f"https://t.me/{ch_username}"
    return "приватный канал (ссылка недоступна)"

# ------------------------ Отзыв ссылок конкурса ------------------------
async def revoke_contest_links(contest_id: int):
    """Отзывает все invite-ссылки, созданные для указанного конкурса."""
    keys_to_delete = []
    for (uid, cid), link in ref_links_cache.items():
        if cid == contest_id:
            try:
                contest = await get_contest(contest_id)
                if contest:
                    await bot.revoke_chat_invite_link(chat_id=contest["channel_id"], invite_link=link)
                    logger.info(f"Отозвана ссылка {link} для пользователя {uid}")
            except Exception as e:
                logger.error(f"Не удалось отозвать ссылку {link}: {e}")
            keys_to_delete.append((uid, cid))
    for key in keys_to_delete:
        del ref_links_cache[key]

# ------------------------ Клавиатуры ------------------------
def admin_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Создать конкурс")],
            [KeyboardButton(text="📋 Мои конкурсы")],
            [KeyboardButton(text="📢 Мои каналы/чаты")],
        ],
        resize_keyboard=True,
    )

def back_inline_kb(callback_data: str = "back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data=callback_data)]])

def skip_inline_kb(back_data: str = "back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=back_data)]
    ])

# ------------------------ Состояния FSM ------------------------
class ContestCreation(StatesGroup):
    waiting_for_type = State()
    waiting_for_invites_count = State()
    waiting_for_comment_timeout = State()
    waiting_for_required_channels = State()
    waiting_for_channel_selection = State()
    waiting_for_end_type = State()
    waiting_for_end_value = State()
    waiting_for_schedule_choice = State()
    waiting_for_schedule_time = State()
    waiting_for_text = State()
    waiting_for_media = State()

class BroadcastState(StatesGroup):
    waiting_for_message = State()

ref_links_cache = {}

async def generate_ref_link(channel_id: int, user_id: int, contest_id: int) -> Optional[str]:
    cache_key = (user_id, contest_id)
    if cache_key in ref_links_cache:
        return ref_links_cache[cache_key]
    try:
        link: ChatInviteLink = await bot.create_chat_invite_link(
            chat_id=channel_id,
            name=f"{user_id}_{contest_id}",
            creates_join_request=False,
            member_limit=0,
        )
        ref_links_cache[cache_key] = link.invite_link
        return link.invite_link
    except Exception as e:
        logger.error(f"Не удалось создать ссылку для {user_id} в конкурсе {contest_id}: {e}")
        return None

# ------------------------ Команда /start ------------------------
@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    user = message.from_user
    await add_user(user.id, user.username or "", user.first_name or "")
    args = command.args
    if args and args.startswith("contest_"):
        contest_id = int(args.split("_")[1])
        contest = await get_contest(contest_id)
        if not contest or contest["status"] not in ["active", "scheduled"]:
            await message.answer("❌ Конкурс не найден или уже завершён.")
            return

        # 1. Проверка подписки на основной канал (обязательно для всех)
        channel_id = contest["channel_id"]
        if not await is_subscribed(user.id, channel_id):
            try:
                channel_info = await bot.get_chat(channel_id)
                username = channel_info.username
            except:
                username = None
            link = await get_channel_link(channel_id, username)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Подписаться", url=link)]])
            await message.answer("⚠️ Для участия в конкурсе подпишитесь на канал:", reply_markup=kb)
            return

        # 2. Проверка остальных обязательных каналов
        required_channels = await get_required_channels(contest_id)
        channel_ids = [ch[0] for ch in required_channels]
        if not await check_all_subscriptions(user.id, channel_ids):
            channels_text = []
            for ch in required_channels:
                ch_id, ch_username = ch[0], ch[1]
                link = await get_channel_link(ch_id, ch_username)
                channels_text.append(f"• {link}")
            await message.answer(
                f"⚠️ Для участия подпишитесь на каналы:\n" + "\n".join(channels_text)
            )
            return

        if contest["type"] == "comments":
            await message.answer("✍️ Конкурс по комментариям. Просто оставьте комментарий под постом в канале.")
        else:
            invite_link = await generate_ref_link(contest["channel_id"], user.id, contest_id)
            if invite_link:
                await message.answer(
                    f"🎁 Чтобы участвовать, пригласите {contest['required_invites']} человек по вашей ссылке:\n{invite_link}\n"
                    "Бот засчитает вступивших только по этой ссылке."
                )
                await add_participant(contest_id, user.id)
            else:
                await message.answer("❌ Ошибка создания ссылки. Попробуйте позже.")
        return
    if is_admin(user.id):
        await message.answer("👑 Админ-панель", reply_markup=admin_main_kb())
    else:
        await message.answer("👋 Привет! Я бот для розыгрышей.")

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if is_admin(message.from_user.id):
        await message.answer("👑 Админ-панель", reply_markup=admin_main_kb())

# ------------------------ Создание конкурса ------------------------
@router.message(F.text == "📝 Создать конкурс")
async def create_contest_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await state.set_state(ContestCreation.waiting_for_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 По комментариям", callback_data="type_comments")],
        [InlineKeyboardButton(text="👥 По приглашениям", callback_data="type_invites")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel")],
    ])
    await message.answer("Выберите тип конкурса:", reply_markup=kb)

@router.callback_query(F.data == "cancel", StateFilter("*"))
async def cancel_creation(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.message.answer("❌ Создание конкурса отменено.", reply_markup=admin_main_kb())

@router.callback_query(F.data.startswith("type_"), ContestCreation.waiting_for_type)
async def process_type(call: CallbackQuery, state: FSMContext):
    ctype = call.data.split("_")[1]
    await state.update_data(ctype=ctype)
    if ctype == "invites":
        await state.set_state(ContestCreation.waiting_for_invites_count)
        await call.message.edit_text("Введите количество приглашений, необходимое для участия:",
                                     reply_markup=back_inline_kb("back_type"))
    else:
        await state.set_state(ContestCreation.waiting_for_comment_timeout)
        await call.message.edit_text("Введите время таймера в минутах (например, 60):",
                                     reply_markup=back_inline_kb("back_type"))
    await call.answer()

@router.callback_query(F.data == "back_type", ContestCreation.waiting_for_invites_count)
async def back_to_type(call: CallbackQuery, state: FSMContext):
    await state.set_state(ContestCreation.waiting_for_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 По комментариям", callback_data="type_comments")],
        [InlineKeyboardButton(text="👥 По приглашениям", callback_data="type_invites")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel")],
    ])
    await call.message.edit_text("Выберите тип конкурса:", reply_markup=kb)
    await call.answer()

@router.message(ContestCreation.waiting_for_invites_count)
async def process_invites_count(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Введите число.")
    await state.update_data(invites_count=int(message.text))
    await state.set_state(ContestCreation.waiting_for_required_channels)
    await message.answer(
        "Добавьте обязательные каналы для подписки (@username или пересланное сообщение) или нажмите «Пропустить»:",
        reply_markup=skip_inline_kb("back_invites")
    )

@router.callback_query(F.data == "back_invites", ContestCreation.waiting_for_required_channels)
async def back_to_invites(call: CallbackQuery, state: FSMContext):
    await state.set_state(ContestCreation.waiting_for_invites_count)
    await call.message.edit_text("Введите количество приглашений, необходимое для участия:",
                                 reply_markup=back_inline_kb("back_type"))
    await call.answer()

@router.message(ContestCreation.waiting_for_comment_timeout)
async def process_comment_timeout(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("Введите число (минуты).")
    timeout_min = int(message.text)
    await state.update_data(comment_timeout=timeout_min * 60)
    await state.set_state(ContestCreation.waiting_for_required_channels)
    await message.answer(
        "Добавьте обязательные каналы для подписки (@username или пересланное сообщение) или нажмите «Пропустить»:",
        reply_markup=skip_inline_kb("back_timeout")
    )

@router.callback_query(F.data == "back_timeout", ContestCreation.waiting_for_required_channels)
async def back_to_timeout(call: CallbackQuery, state: FSMContext):
    await state.set_state(ContestCreation.waiting_for_comment_timeout)
    await call.message.edit_text("Введите время таймера в минутах (например, 60):",
                                 reply_markup=back_inline_kb("back_type"))
    await call.answer()

@router.message(ContestCreation.waiting_for_required_channels)
async def add_required_channel(message: Message, state: FSMContext):
    data = await state.get_data()
    channels = data.get("required_channels", [])
    if message.forward_from_chat and message.forward_from_chat.type in [ChatType.CHANNEL, ChatType.GROUP]:
        chat = message.forward_from_chat
        channels.append({"id": chat.id, "username": chat.username or "", "title": chat.title or ""})
        await state.update_data(required_channels=channels)
        await message.answer(f"✅ Канал {chat.title} добавлен. Можете добавить ещё или нажать «Готово».")
    elif message.text and message.text.startswith("@"):
        username = message.text.strip()
        try:
            chat = await bot.get_chat(username)
            channels.append({"id": chat.id, "username": username, "title": chat.title or ""})
            await state.update_data(required_channels=channels)
            await message.answer(f"✅ Канал {chat.title} добавлен. Можете добавить ещё или нажать «Готово».")
        except Exception as e:
            await message.answer(f"❌ Не удалось найти канал: {e}")
    else:
        await message.answer("Отправьте @username канала или перешлите сообщение из канала.")

@router.callback_query(F.data == "skip", ContestCreation.waiting_for_required_channels)
async def skip_channels(call: CallbackQuery, state: FSMContext):
    await state.update_data(required_channels=[])
    await proceed_to_channel_selection(call.message, state)

async def proceed_to_channel_selection(message, state: FSMContext):
    channels = await get_bot_channels()
    if not channels:
        await message.answer("❌ Нет доступных каналов. Сначала добавьте канал в «Мои каналы/чаты».")
        await state.clear()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{ch[2] or ch[1]} ({ch[1]})", callback_data=f"select_channel_{ch[1]}")]
        for ch in channels
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_channels")]])
    await state.set_state(ContestCreation.waiting_for_channel_selection)
    await message.edit_text("Выберите канал для публикации:", reply_markup=kb)

@router.callback_query(F.data == "back_to_channels", ContestCreation.waiting_for_channel_selection)
async def back_to_required_channels(call: CallbackQuery, state: FSMContext):
    await state.set_state(ContestCreation.waiting_for_required_channels)
    back_data = "back_invites" if (await state.get_data()).get("ctype") == "invites" else "back_timeout"
    await call.message.edit_text(
        "Добавьте обязательные каналы для подписки (@username или пересланное сообщение) или нажмите «Пропустить»:",
        reply_markup=skip_inline_kb(back_data)
    )
    await call.answer()

@router.callback_query(F.data.startswith("select_channel_"), ContestCreation.waiting_for_channel_selection)
async def channel_selected(call: CallbackQuery, state: FSMContext):
    username = call.data.split("_", 2)[2]
    await state.update_data(channel_username=username)
    data = await state.get_data()
    if data.get("ctype") == "comments":
        await state.set_state(ContestCreation.waiting_for_schedule_choice)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="schedule_now")],
            [InlineKeyboardButton(text="📅 Отложенная публикация", callback_data="schedule_later")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_channel_select")],
        ])
        await call.message.edit_text("Когда опубликовать конкурс?", reply_markup=kb)
    else:
        await state.set_state(ContestCreation.waiting_for_end_type)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏰ По времени", callback_data="end_type_time")],
            [InlineKeyboardButton(text="👥 По количеству участников", callback_data="end_type_participants")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_channel_select")],
        ])
        await call.message.edit_text("Выберите условие завершения конкурса:", reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "back_to_channel_select", ContestCreation.waiting_for_end_type)
async def back_to_channel_select(call: CallbackQuery, state: FSMContext):
    await proceed_to_channel_selection(call.message, state)

@router.callback_query(F.data.startswith("end_type_"), ContestCreation.waiting_for_end_type)
async def end_type_chosen(call: CallbackQuery, state: FSMContext):
    etype = call.data.split("_")[2]
    await state.update_data(end_type=etype)
    if etype == "time":
        await state.set_state(ContestCreation.waiting_for_end_value)
        await call.message.edit_text(
            "Введите дату и время завершения (МСК) в формате ДД.ММ.ГГГГ ЧЧ:ММ (например, 30.07.2026 15:20):",
            reply_markup=back_inline_kb("back_end_type")
        )
    else:
        await state.set_state(ContestCreation.waiting_for_end_value)
        await call.message.edit_text(
            "Введите количество участников для завершения:",
            reply_markup=back_inline_kb("back_end_type")
        )
    await call.answer()

@router.callback_query(F.data == "back_end_type", ContestCreation.waiting_for_end_value)
async def back_to_end_type(call: CallbackQuery, state: FSMContext):
    await state.set_state(ContestCreation.waiting_for_end_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏰ По времени", callback_data="end_type_time")],
        [InlineKeyboardButton(text="👥 По количеству участников", callback_data="end_type_participants")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_channel_select")],
    ])
    await call.message.edit_text("Выберите условие завершения конкурса:", reply_markup=kb)
    await call.answer()

@router.message(ContestCreation.waiting_for_end_value)
async def process_end_value(message: Message, state: FSMContext):
    data = await state.get_data()
    etype = data["end_type"]
    if etype == "time":
        try:
            dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        except ValueError:
            return await message.answer("❌ Неверный формат. Попробуйте ещё раз (ДД.ММ.ГГГГ ЧЧ:ММ):")
        await state.update_data(end_value=dt.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        if not message.text.isdigit():
            return await message.answer("Введите число участников.")
        await state.update_data(end_value=int(message.text))
    await state.set_state(ContestCreation.waiting_for_schedule_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="schedule_now")],
        [InlineKeyboardButton(text="📅 Отложенная публикация", callback_data="schedule_later")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_end_value")],
    ])
    await message.answer("Когда опубликовать конкурс?", reply_markup=kb)

@router.callback_query(F.data == "back_end_value", ContestCreation.waiting_for_schedule_choice)
async def back_to_end_value(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    etype = data["end_type"]
    if etype == "time":
        await state.set_state(ContestCreation.waiting_for_end_value)
        await call.message.edit_text(
            "Введите дату и время завершения (МСК) в формате ДД.ММ.ГГГГ ЧЧ:ММ (например, 30.07.2026 15:20):",
            reply_markup=back_inline_kb("back_end_type")
        )
    else:
        await state.set_state(ContestCreation.waiting_for_end_value)
        await call.message.edit_text(
            "Введите количество участников для завершения:",
            reply_markup=back_inline_kb("back_end_type")
        )
    await call.answer()

@router.callback_query(F.data.startswith("schedule_"), ContestCreation.waiting_for_schedule_choice)
async def schedule_choice(call: CallbackQuery, state: FSMContext):
    choice = call.data.split("_")[1]
    if choice == "now":
        await state.update_data(scheduled_at=None)
        await state.set_state(ContestCreation.waiting_for_text)
        await call.message.edit_text("Введите текст конкурса (HTML-разметка поддерживается):",
                                     reply_markup=back_inline_kb("back_schedule"))
    else:
        await state.set_state(ContestCreation.waiting_for_schedule_time)
        await call.message.edit_text(
            "Введите дату и время публикации (МСК) в формате ДД.ММ.ГГГГ ЧЧ:ММ (например, 30.07.2026 15:20):",
            reply_markup=back_inline_kb("back_schedule")
        )
    await call.answer()

@router.callback_query(F.data == "back_schedule", ContestCreation.waiting_for_text)
async def back_to_schedule(call: CallbackQuery, state: FSMContext):
    await state.set_state(ContestCreation.waiting_for_schedule_choice)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data="schedule_now")],
        [InlineKeyboardButton(text="📅 Отложенная публикация", callback_data="schedule_later")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_end_value")],
    ])
    await call.message.edit_text("Когда опубликовать конкурс?", reply_markup=kb)

@router.message(ContestCreation.waiting_for_schedule_time)
async def process_schedule_time(message: Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        return await message.answer("❌ Неверный формат. Попробуйте ещё раз (ДД.ММ.ГГГГ ЧЧ:ММ):")
    await state.update_data(scheduled_at=dt.strftime("%Y-%m-%d %H:%M:%S"))
    await state.set_state(ContestCreation.waiting_for_text)
    await message.answer("Введите текст конкурса (HTML-разметка поддерживается):",
                         reply_markup=back_inline_kb("back_schedule"))

@router.message(ContestCreation.waiting_for_text)
async def process_text(message: Message, state: FSMContext):
    await state.update_data(text=message.html_text)
    await state.set_state(ContestCreation.waiting_for_media)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без медиа", callback_data="media_skip")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_text")],
    ])
    await message.answer("Отправьте фото, видео или GIF (одно) или нажмите «Без медиа»:", reply_markup=kb)

@router.callback_query(F.data == "back_text", ContestCreation.waiting_for_media)
async def back_to_text(call: CallbackQuery, state: FSMContext):
    await state.set_state(ContestCreation.waiting_for_text)
    await call.message.edit_text("Введите текст конкурса (HTML-разметка поддерживается):",
                                 reply_markup=back_inline_kb("back_schedule"))
    await call.answer()

@router.callback_query(F.data == "media_skip", ContestCreation.waiting_for_media)
async def skip_media(call: CallbackQuery, state: FSMContext):
    await state.update_data(media=None, media_type=None)
    await finish_contest_creation(call.message, state)

@router.message(ContestCreation.waiting_for_media, F.photo | F.video | F.animation)
async def process_media(message: Message, state: FSMContext):
    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        media_type = "video"
    elif message.animation:
        file_id = message.animation.file_id
        media_type = "animation"
    else:
        return
    await state.update_data(media=file_id, media_type=media_type)
    await finish_contest_creation(message, state)

async def finish_contest_creation(message: Message, state: FSMContext):
    data = await state.get_data()
    ctype = data["ctype"]
    channel_username = data["channel_username"]
    try:
        chat = await bot.get_chat(channel_username)
        channel_id = chat.id
    except Exception as e:
        await message.answer(f"❌ Не удалось получить канал: {e}")
        await state.clear()
        return

    required_channels = data.get("required_channels", [])
    pub_channel_id = chat.id
    if not any(ch.get("id") == pub_channel_id for ch in required_channels):
        required_channels.append({"id": pub_channel_id, "username": channel_username, "title": chat.title})

    discussion_chat_id = None
    if ctype == "comments":
        try:
            full_chat = await bot.get_chat(channel_id)
            if full_chat.linked_chat_id:
                discussion_chat_id = full_chat.linked_chat_id
                logger.info(f"Для канала {channel_id} найден привязанный чат {discussion_chat_id}")
        except Exception as e:
            logger.warning(f"Не удалось получить linked_chat_id для {channel_id}: {e}")

    db = await get_db()
    comment_timeout = data.get("comment_timeout", 0)
    cursor = await db.execute(
        """INSERT INTO contests 
        (type, channel_id, text, photo, end_type, end_value, required_channels, required_invites, status, scheduled_at, comment_timeout, discussion_chat_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ctype,
            channel_id,
            data["text"],
            json.dumps(data.get("media")) if data.get("media") else None,
            data.get("end_type"),
            str(data["end_value"]) if data.get("end_value") else None,
            json.dumps([(ch["id"], ch["username"]) for ch in required_channels]),
            data.get("invites_count", 0),
            "scheduled" if data.get("scheduled_at") else "active",
            data.get("scheduled_at"),
            comment_timeout,
            discussion_chat_id,
        )
    )
    contest_id = cursor.lastrowid
    await db.commit()
    await state.clear()
    if data.get("scheduled_at"):
        await message.answer(f"✅ Конкурс #{contest_id} создан и будет опубликован {data['scheduled_at']}.",
                             reply_markup=admin_main_kb())
    else:
        await publish_contest(contest_id)
        await message.answer(f"✅ Конкурс #{contest_id} опубликован в канале.", reply_markup=admin_main_kb())

async def publish_contest(contest_id: int):
    contest = await get_contest(contest_id)
    if not contest:
        return
    text = contest["text"]
    media_file_id = contest.get("photo")
    bot_info = await bot.get_me()
    url = f"https://t.me/{bot_info.username}?start=contest_{contest_id}"
    inline_kb = None
    if contest["type"] != "comments":
        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎉 Участвовать", url=url)]
        ])
    try:
        if media_file_id:
            msg = await bot.send_photo(
                chat_id=contest["channel_id"],
                photo=media_file_id,
                caption=text,
                reply_markup=inline_kb,
            )
        else:
            msg = await bot.send_message(
                chat_id=contest["channel_id"],
                text=text,
                reply_markup=inline_kb,
            )
        post_id = msg.message_id
        await update_contest(contest_id, post_id=post_id, status="active")
    except Exception as e:
        logger.error(f"Ошибка публикации конкурса {contest_id}: {e}")

@router.chat_member()
async def on_user_joined(event: ChatMemberUpdated):
    logger.info(f"Обработчик on_user_joined: new_status={event.new_chat_member.status}, invite_link={event.invite_link}")
    new_status = event.new_chat_member.status
    if new_status != ChatMemberStatus.MEMBER:
        return
    chat = event.chat
    user = event.new_chat_member.user
    invite_link = event.invite_link
    if not invite_link or not invite_link.name:
        return
    try:
        parts = invite_link.name.split("_")
        if len(parts) != 2:
            return
        referrer_id = int(parts[0])
        contest_id = int(parts[1])
    except ValueError:
        return
    if referrer_id == user.id:
        return
    contest = await get_contest(contest_id)
    if not contest or contest["type"] != "invites" or contest["channel_id"] != chat.id or contest["status"] != "active":
        return
    db = await get_db()
    participant = await db.execute(
        "SELECT invited, qualified FROM contest_participants WHERE contest_id=? AND user_id=?",
        (contest_id, referrer_id)
    )
    part = await participant.fetchone()
    if not part:
        return
    new_invited = await increment_invite(contest_id, referrer_id)
    if new_invited >= contest["required_invites"] and not part[1]:
        await qualify_participant(contest_id, referrer_id)
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 Вы выполнили условия конкурса #{contest_id} и теперь участвуете в розыгрыше!"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить пользователя {referrer_id}: {e}")

# ------------------------ Конкурс по комментариям (таймер 5 сек) ------------------------
active_comment_tasks: Dict[int, asyncio.Task] = {}

async def get_bid_count(contest_id: int) -> int:
    contest = await get_contest(contest_id)
    return contest["bids_count"] if contest else 0

@router.message(F.chat.type.in_({ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_comment(message: Message):
    logger.info(f"Получено сообщение от {message.from_user.id} в чате {message.chat.id} (тип {message.chat.type}), текст: {message.text}")
    if message.from_user.is_bot:
        return
    if message.sender_chat and message.sender_chat.type == ChatType.CHANNEL:
        logger.info("Сообщение от канала – пропускаем")
        return

    db = await get_db()
    cursor = await db.execute(
        "SELECT id, post_id, comment_timeout, timer_message_id, last_bid_user_id, last_bid_username, required_channels, discussion_chat_id "
        "FROM contests WHERE type='comments' AND status='active' AND (discussion_chat_id=? OR channel_id=?)",
        (message.chat.id, message.chat.id)
    )
    contest = await cursor.fetchone()
    if not contest:
        logger.info(f"Активный конкурс по комментариям не найден для чата {message.chat.id}")
        return

    contest_id, post_id, timeout, timer_msg_id, last_bid_user_id, last_bid_username, req_channels_json, disc_id = contest
    logger.info(f"Найден конкурс #{contest_id}, post_id={post_id}, timeout={timeout}")

    required_channels = json.loads(req_channels_json) if req_channels_json else []
    user = message.from_user

    if not await check_all_subscriptions(user.id, [ch[0] for ch in required_channels]):
        links = []
        for ch in required_channels:
            ch_id, ch_username = ch[0], ch[1]
            link = await get_channel_link(ch_id, ch_username)
            links.append(link)
        links_text = "\n".join(links)
        try:
            await message.delete()
            await bot.send_message(
                user.id,
                f"⚠️ Вы не подписаны на обязательные каналы. Подпишитесь:\n{links_text}"
            )
        except Exception as e:
            logger.warning(f"Не удалось удалить или уведомить: {e}")
        return

    if last_bid_user_id and last_bid_user_id == user.id:
        logger.info(f"Лидер {user.id} повторно написал – таймер не обновляется")
        return

    if timer_msg_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=timer_msg_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить таймер-сообщение {timer_msg_id}: {e}")

    await update_contest(
        contest_id,
        last_bid_user_id=user.id,
        last_bid_username=user.username or user.first_name,
        last_bid_msg_id=message.message_id,
        bids_count=await get_bid_count(contest_id) + 1,
    )
    await add_participant(contest_id, user.id)
    await qualify_participant(contest_id, user.id)

    if contest_id in active_comment_tasks:
        active_comment_tasks[contest_id].cancel()

    leader_name = user.username or user.first_name
    timeout_min = timeout // 60
    timeout_sec = timeout % 60
    timer_text = f"⏳ <b>Лидер:</b> @{html.escape(str(leader_name))}\n⏰ <b>Таймер:</b> {timeout_min} мин {timeout_sec} сек"
    try:
        bot_msg = await message.answer(timer_text)
        new_timer_msg_id = bot_msg.message_id
        await update_contest(contest_id, timer_message_id=new_timer_msg_id)
    except Exception as e:
        logger.error(f"Не удалось отправить таймер-сообщение: {e}")
        return

    active_comment_tasks[contest_id] = asyncio.create_task(
        comment_timer(contest_id, timeout, new_timer_msg_id, user.id, leader_name)
    )

async def comment_timer(contest_id: int, timeout: int, timer_msg_id: int, leader_id: int, leader_name: str):
    end_time = datetime.now() + timedelta(seconds=timeout)
    try:
        while True:
            remaining = (end_time - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            await asyncio.sleep(min(5, remaining))
            contest = await get_contest(contest_id)
            if not contest or contest["status"] != "active" or contest["last_bid_user_id"] != leader_id:
                return
            remaining = (end_time - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            update_text = f"⏳ <b>Лидер:</b> @{html.escape(leader_name)}\n⏰ <b>Таймер:</b> {mins} мин {secs} сек"
            try:
                chat_id = contest["discussion_chat_id"] if contest["discussion_chat_id"] else contest["channel_id"]
                await bot.edit_message_text(chat_id=chat_id, message_id=timer_msg_id, text=update_text)
            except Exception as e:
                logger.warning(f"Не удалось обновить таймер-сообщение: {e}")
    except asyncio.CancelledError:
        return
    await finish_comment_contest(contest_id, timer_msg_id)

async def finish_comment_contest(contest_id: int, timer_msg_id: int):
    contest = await get_contest(contest_id)
    if not contest or contest["status"] != "active":
        return
    winner_id = contest["last_bid_user_id"]
    winner_username = contest["last_bid_username"]
    await update_contest(contest_id, status="finished", ended_at=datetime.now().isoformat())
    channel_id = contest["discussion_chat_id"] if contest["discussion_chat_id"] else contest["channel_id"]
    try:
        if winner_id and winner_username:
            text = f"🏆 <b>Победитель:</b> @{html.escape(winner_username)} (ID {winner_id})\nКонкурс завершён!"
        else:
            text = "🏆 Конкурс завершён, но никто не оставил комментарий."
        await bot.edit_message_text(chat_id=channel_id, message_id=timer_msg_id, text=text)
    except Exception as e:
        logger.error(f"Не удалось отредактировать финальное сообщение: {e}")
        try:
            await bot.send_message(channel_id, text if winner_id else "🏆 Конкурс завершён.")
        except:
            pass
    if contest_id in active_comment_tasks:
        del active_comment_tasks[contest_id]
    await revoke_contest_links(contest_id)

# ------------------------ Управление конкурсами ------------------------
@router.message(Command("contest"))
async def contest_command(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    try:
        contest_id = int(command.args)
    except:
        await message.answer("Использование: /contest ID")
        return
    await show_contest_menu(message, contest_id)

async def show_contest_menu(message: Message, contest_id: int):
    contest = await get_contest(contest_id)
    if not contest:
        await message.answer("Конкурс не найден.")
        return
    text = f"📋 Конкурс #{contest_id}\nТип: {contest['type']}\nСтатус: {contest['status']}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏆 Выбрать победителя", callback_data=f"pick_winner_{contest_id}")],
        [InlineKeyboardButton(text="⏹ Завершить досрочно", callback_data=f"finish_contest_{contest_id}")],
        [InlineKeyboardButton(text="🗑 Удалить конкурс", callback_data=f"delete_contest_{contest_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="my_contests")],
    ])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data.startswith("pick_winner_"))
async def pick_winner(call: CallbackQuery):
    contest_id = int(call.data.split("_")[2])
    contest = await get_contest(contest_id)
    if not contest or contest["status"] != "active":
        await call.answer("Конкурс не активен.", show_alert=True)
        return
    if contest["type"] == "comments":
        winner_id, winner_username = await get_last_bid(contest_id)
        if winner_id:
            await bot.send_message(contest["channel_id"], f"🏆 Победитель (вручную): @{winner_username}")
            await update_contest(contest_id, status="finished", ended_at=datetime.now().isoformat())
            await call.answer("Победитель объявлен.")
        else:
            await call.answer("Нет комментариев.", show_alert=True)
    else:
        qualified = await get_qualified_participants(contest_id)
        if not qualified:
            await call.answer("Нет квалифицированных участников.", show_alert=True)
            return
        winner_id = random.choice(qualified)
        try:
            winner_info = await bot.get_chat(winner_id)
            winner_username = winner_info.username or winner_info.first_name
        except:
            winner_username = str(winner_id)
        await bot.send_message(contest["channel_id"], f"🏆 Победитель (вручную): @{winner_username}")
        await update_contest(contest_id, status="finished", ended_at=datetime.now().isoformat())
        await call.answer("Победитель выбран.")
    await call.message.delete_reply_markup()
    await revoke_contest_links(contest_id)

@router.callback_query(F.data.startswith("finish_contest_"))
async def finish_contest(call: CallbackQuery):
    contest_id = int(call.data.split("_")[2])
    await update_contest(contest_id, status="finished", ended_at=datetime.now().isoformat())
    if contest_id in active_comment_tasks:
        active_comment_tasks[contest_id].cancel()
        del active_comment_tasks[contest_id]
    await call.message.edit_text("⏹ Конкурс завершён досрочно.")
    await call.answer()
    await revoke_contest_links(contest_id)

@router.callback_query(F.data.startswith("delete_contest_"))
async def delete_contest(call: CallbackQuery):
    contest_id = int(call.data.split("_")[2])
    db = await get_db()
    await db.execute("DELETE FROM contests WHERE id=?", (contest_id,))
    await db.execute("DELETE FROM contest_participants WHERE contest_id=?", (contest_id,))
    await db.commit()
    await call.message.edit_text("🗑 Конкурс удалён.")
    await call.answer()
    await revoke_contest_links(contest_id)

# ------------------------ Меню администратора ------------------------
@router.message(F.text == "📋 Мои конкурсы")
async def my_contests(message: Message):
    if not is_admin(message.from_user.id):
        return
    contests = await get_contests(message.from_user.id)
    if not contests:
        await message.answer("У вас пока нет конкурсов.", reply_markup=admin_main_kb())
        return
    text = "📋 Ваши конкурсы:\n\n"
    for c in contests:
        cid = html.escape(str(c[0]))
        ctype = html.escape(str(c[1]))
        cstatus = html.escape(str(c[11]))
        text += f"<b>#{cid}</b> {ctype} | {cstatus}\n"
    text += "\nДля управления используйте /contest ID"
    await message.answer(text, reply_markup=admin_main_kb())

@router.message(F.text == "📢 Мои каналы/чаты")
async def manage_channels_menu(message: Message, state: FSMContext = None):
    if not is_admin(message.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал/чат", callback_data="add_channel")],
        [InlineKeyboardButton(text="➖ Удалить канал/чат", callback_data="del_channel")],
        [InlineKeyboardButton(text="📋 Список", callback_data="list_channels")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")],
    ])
    await message.answer("Управление каналами/чатами:", reply_markup=kb)

@router.callback_query(F.data == "admin_back")
async def admin_back(call: CallbackQuery):
    await call.message.delete()
    await call.message.answer("👑 Админ-панель", reply_markup=admin_main_kb())

@router.callback_query(F.data == "add_channel")
async def add_channel_prompt(call: CallbackQuery, state: FSMContext):
    await state.set_state("add_channel_username")
    await call.message.edit_text("Отправьте @username канала или перешлите сообщение из канала.", reply_markup=back_inline_kb("admin_back"))

@router.message(StateFilter("add_channel_username"))
async def process_add_channel(message: Message, state: FSMContext):
    if message.forward_from_chat and message.forward_from_chat.type in [ChatType.CHANNEL, ChatType.GROUP]:
        chat = message.forward_from_chat
        try:
            member = await bot.get_chat_member(chat.id, bot.id)
            if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await add_bot_channel(chat.id, chat.username or "", chat.title or "", chat.type)
                await message.answer(f"✅ Канал {chat.title} добавлен.")
                await state.clear()
                return
        except:
            pass
        await message.answer("❌ Бот не является администратором этого канала.")
    elif message.text and message.text.startswith("@"):
        username = message.text.strip()
        try:
            chat = await bot.get_chat(username)
            member = await bot.get_chat_member(chat.id, bot.id)
            if member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await add_bot_channel(chat.id, username, chat.title or "", chat.type)
                await message.answer(f"✅ Канал {chat.title} добавлен.")
                await state.clear()
                return
            else:
                await message.answer("❌ Бот не администратор.")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    else:
        await message.answer("Отправьте @username или перешлите сообщение.")
    await state.clear()

@router.callback_query(F.data == "del_channel")
async def del_channel_list(call: CallbackQuery):
    channels = await get_bot_channels()
    if not channels:
        await call.message.edit_text("Список пуст.", reply_markup=back_inline_kb("admin_back"))
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{ch[2]} ({ch[1]})", callback_data=f"delchannel_{ch[1]}")]
        for ch in channels
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]])
    await call.message.edit_text("Выберите канал для удаления:", reply_markup=kb)

@router.callback_query(F.data.startswith("delchannel_"))
async def delete_channel(call: CallbackQuery):
    channel_id = int(call.data.split("_")[1])
    await remove_bot_channel(channel_id)
    await call.message.edit_text("✅ Канал удалён.", reply_markup=back_inline_kb("admin_back"))

@router.callback_query(F.data == "list_channels")
async def list_channels(call: CallbackQuery):
    channels = await get_bot_channels()
    if not channels:
        text = "Нет добавленных каналов."
    else:
        text = "📢 Добавленные каналы:\n" + "\n".join(f"• {ch[2]} (@{ch[1]})" for ch in channels)
    await call.message.edit_text(text, reply_markup=back_inline_kb("admin_back"))

# ------------------------ Статистика и рассылка ------------------------
@router.message(Command("stats"))
async def stats_command(message: Message):
    if not is_admin(message.from_user.id):
        return
    db = await get_db()
    cursor = await db.execute("SELECT COUNT(*) FROM users")
    users = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM contests")
    contests = (await cursor.fetchone())[0]
    await message.answer(f"📊 Статистика:\nПользователей: {users}\nКонкурсов: {contests}")

@router.message(Command("broadcast"))
async def broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(BroadcastState.waiting_for_message)
    await message.answer("Отправьте текст для рассылки (HTML). Для отмены /cancel")

@router.message(BroadcastState.waiting_for_message)
async def broadcast_message(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    db = await get_db()
    cursor = await db.execute("SELECT user_id FROM users")
    rows = await cursor.fetchall()
    good, bad = 0, 0
    for (uid,) in rows:
        try:
            await bot.send_message(uid, message.html_text)
            good += 1
        except:
            bad += 1
        await asyncio.sleep(0.05)
    await state.clear()
    await message.answer(f"✅ Рассылка завершена: успешно {good}, ошибок {bad}.")

# ------------------------ Автоматический пост с победителем (с уведомлением админам) ------------------------
async def announce_winner(contest_id: int):
    contest = await get_contest(contest_id)
    if not contest or contest["status"] != "finished":
        return
    channel_id = contest["channel_id"]
    winner_id = None
    winner_username = None
    if contest["type"] == "comments":
        winner_id = contest["last_bid_user_id"]
        winner_username = contest["last_bid_username"]
    else:
        qualified = await get_qualified_participants(contest_id)
        if qualified:
            winner_id = random.choice(qualified)
            try:
                winner_info = await bot.get_chat(winner_id)
                winner_username = winner_info.username or winner_info.first_name
            except:
                winner_username = str(winner_id)

    # Уведомление администраторам в ЛС
    if winner_id and winner_username:
        admin_winner_text = f"@{html.escape(winner_username)} (ID {winner_id})"
    else:
        admin_winner_text = "нет победителя"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📢 <b>Конкурс #{contest_id} завершён!</b>\n"
                f"Тип: {contest['type']}\n"
                f"Победитель: {admin_winner_text}"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

    # Публичный пост в канал (без номера конкурса)
    if not winner_id or not winner_username:
        try:
            await bot.send_message(channel_id, "🏆 Конкурс завершён, но никто не выполнил условия.")
        except:
            pass
    else:
        try:
            await bot.send_message(
                channel_id,
                f"🏆 <b>Конкурс завершён!</b>\n\n"
                f"Победитель: @{html.escape(winner_username)}\n\n"
                f"Поздравляем! 🎉"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить пост о победителе для конкурса {contest_id}: {e}")

    await revoke_contest_links(contest_id)

# ------------------------ Планировщик ------------------------
async def scheduler():
    while True:
        await asyncio.sleep(30)
        db = await get_db()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await db.execute(
            "SELECT id FROM contests WHERE status='scheduled' AND scheduled_at <= ?", (now,)
        )
        to_publish = await cursor.fetchall()
        for (cid,) in to_publish:
            await update_contest(cid, status="active")
            await publish_contest(cid)

        cursor = await db.execute(
            "SELECT id FROM contests WHERE status='active' AND end_type='time' AND end_value <= ?", (now,)
        )
        to_finish_time = await cursor.fetchall()
        for (cid,) in to_finish_time:
            contest = await get_contest(cid)
            if contest and contest["type"] == "comments":
                await update_contest(cid, status="finished", ended_at=now)
                await announce_winner(cid)
            else:
                await update_contest(cid, status="finished", ended_at=now)
                await announce_winner(cid)

        cursor = await db.execute(
            "SELECT id, end_value FROM contests WHERE status='active' AND end_type='participants'"
        )
        part_contests = await cursor.fetchall()
        for cid, target in part_contests:
            qualified = await get_qualified_participants(cid)
            if len(qualified) >= int(target):
                await update_contest(cid, status="finished", ended_at=now)
                await announce_winner(cid)

async def main():
    await init_db()
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())