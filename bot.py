import asyncio
import logging
import os
from html import escape as html_escape
from typing import Optional, List, Tuple, Set, Dict

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.enums import ChatType, ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext


# ---------------------------------------------------------------
# Переменные окружения
# ---------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Переменная окружения BOT_TOKEN не задана.")

try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0").strip())
except ValueError:
    raise RuntimeError("GROUP_ID должен быть числом.")

if not GROUP_ID:
    raise RuntimeError("Переменная окружения GROUP_ID не задана.")


def parse_admin_ids() -> Set[int]:
    raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID") or ""
    ids: Set[int] = set()

    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            pass

    return ids


ADMIN_IDS = parse_admin_ids()

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_ID или ADMIN_IDS не заданы.")

DB_PATH = os.getenv("DB_PATH", "filter.db")

db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)


router = Router()


# ---------------------------------------------------------------
# Типы контента
# ---------------------------------------------------------------

CONTENT_TYPES: Dict[str, Dict[str, str]] = {
    "text": {"name": "Текстовые сообщения", "code": "t"},
    "photo": {"name": "Фотографии", "code": "p"},
    "video": {"name": "Видео", "code": "v"},
    "sticker": {"name": "Стикеры", "code": "s"},
    "animation": {"name": "GIF", "code": "g"},
    "audio": {"name": "Музыка", "code": "a"},
    "document": {"name": "Файлы", "code": "d"},
    "voice": {"name": "Голосовые сообщения", "code": "o"},
    "video_note": {"name": "Видеосообщения", "code": "n"},
    "poll": {"name": "Опросы", "code": "l"},
}

CONTENT_TYPES_ORDER = [
    "text",
    "photo",
    "video",
    "sticker",
    "animation",
    "audio",
    "document",
    "voice",
    "video_note",
    "poll",
]

CODE_TO_TYPE = {data["code"]: ctype for ctype, data in CONTENT_TYPES.items()}

MODE_ICONS = {
    "allow": "✅",
    "selected": "👥",
    "deny": "🚫",
}

MODE_NAMES = {
    "allow": "Разрешить всем",
    "selected": "Только выбранные пользователи",
    "deny": "Удалять у всех",
}

PAGE_SIZE = 8


class AddUser(StatesGroup):
    wait_id = State()


# ---------------------------------------------------------------
# База данных
# ---------------------------------------------------------------

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                name TEXT,
                PRIMARY KEY (chat_id, topic_id)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS topic_type_rules (
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                ctype TEXT NOT NULL,
                mode TEXT NOT NULL,
                PRIMARY KEY (chat_id, topic_id, ctype)
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_type_rules (
                chat_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                ctype TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                rule TEXT NOT NULL,
                PRIMARY KEY (chat_id, topic_id, ctype, user_id)
            )
        """)

        await db.commit()


async def upsert_topic(chat_id: int, topic_id: int, name: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO topics(chat_id, topic_id, name)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, topic_id) DO UPDATE SET
                name = COALESCE(excluded.name, topics.name)
            """,
            (chat_id, topic_id, name),
        )
        await db.commit()


async def ensure_topic_exists(chat_id: int, topic_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO topics(chat_id, topic_id, name)
            VALUES (?, ?, NULL)
            """,
            (chat_id, topic_id),
        )
        await db.commit()


async def get_topics(chat_id: int) -> List[Tuple[int, Optional[str]]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT topic_id, name
            FROM topics
            WHERE chat_id = ?
            ORDER BY topic_id
            """,
            (chat_id,),
        ) as cur:
            return await cur.fetchall()


async def get_topic_name(chat_id: int, topic_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT name
            FROM topics
            WHERE chat_id = ? AND topic_id = ?
            """,
            (chat_id, topic_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_configured_topic_ids(chat_id: int) -> Set[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT topic_id
            FROM topic_type_rules
            WHERE chat_id = ?
            UNION
            SELECT topic_id
            FROM user_type_rules
            WHERE chat_id = ?
            """,
            (chat_id, chat_id),
        ) as cur:
            rows = await cur.fetchall()
            return {row[0] for row in rows}


async def set_mode(chat_id: int, topic_id: int, ctype: str, mode: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO topic_type_rules(chat_id, topic_id, ctype, mode)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, topic_id, ctype) DO UPDATE SET
                mode = excluded.mode
            """,
            (chat_id, topic_id, ctype, mode),
        )
        await db.commit()


async def get_mode(chat_id: int, topic_id: int, ctype: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT mode
            FROM topic_type_rules
            WHERE chat_id = ? AND topic_id = ? AND ctype = ?
            """,
            (chat_id, topic_id, ctype),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_modes_for_topic(chat_id: int, topic_id: int) -> Dict[str, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT ctype, mode
            FROM topic_type_rules
            WHERE chat_id = ? AND topic_id = ?
            """,
            (chat_id, topic_id),
        ) as cur:
            rows = await cur.fetchall()
            return {row[0]: row[1] for row in rows}


async def get_all_topic_rules(chat_id: int) -> List[Tuple[int, str, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT topic_id, ctype, mode
            FROM topic_type_rules
            WHERE chat_id = ?
            ORDER BY topic_id
            """,
            (chat_id,),
        ) as cur:
            return await cur.fetchall()


async def reset_topic(chat_id: int, topic_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM topic_type_rules
            WHERE chat_id = ? AND topic_id = ?
            """,
            (chat_id, topic_id),
        )
        await db.execute(
            """
            DELETE FROM user_type_rules
            WHERE chat_id = ? AND topic_id = ?
            """,
            (chat_id, topic_id),
        )
        await db.commit()


async def set_user_rule(chat_id: int, topic_id: int, ctype: str, user_id: int, rule: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_type_rules(chat_id, topic_id, ctype, user_id, rule)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, topic_id, ctype, user_id) DO UPDATE SET
                rule = excluded.rule
            """,
            (chat_id, topic_id, ctype, user_id, rule),
        )
        await db.commit()


async def delete_user_rule(chat_id: int, topic_id: int, ctype: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            DELETE FROM user_type_rules
            WHERE chat_id = ? AND topic_id = ? AND ctype = ? AND user_id = ?
            """,
            (chat_id, topic_id, ctype, user_id),
        )
        await db.commit()


async def get_user_rule(chat_id: int, topic_id: int, ctype: str, user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT rule
            FROM user_type_rules
            WHERE chat_id = ? AND topic_id = ? AND ctype = ? AND user_id = ?
            """,
            (chat_id, topic_id, ctype, user_id),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def list_user_rules_for_type(
    chat_id: int,
    topic_id: int,
    ctype: str,
) -> List[Tuple[int, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT user_id, rule
            FROM user_type_rules
            WHERE chat_id = ? AND topic_id = ? AND ctype = ?
            ORDER BY user_id
            """,
            (chat_id, topic_id, ctype),
        ) as cur:
            return await cur.fetchall()


async def count_user_rules_for_type(chat_id: int, topic_id: int, ctype: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM user_type_rules
            WHERE chat_id = ? AND topic_id = ? AND ctype = ?
            """,
            (chat_id, topic_id, ctype),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ---------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------

def is_owner(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def topic_display(topic_id: int, name: Optional[str] = None) -> str:
    if topic_id == 0:
        return name or "General / без ветки"
    return name or f"Ветка #{topic_id}"


def truncate_text(text: str, limit: int = 60) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def detect_content_type(message: Message) -> Optional[str]:
    if message.poll:
        return "poll"

    if message.photo:
        return "photo"

    if message.video_note:
        return "video_note"

    if message.video:
        return "video"

    if message.audio:
        return "audio"

    if message.voice:
        return "voice"

    if message.animation:
        return "animation"

    if message.sticker:
        return "sticker"

    if message.document:
        return "document"

    if message.text:
        return "text"

    return None


async def try_delete(message: Message):
    try:
        await message.delete()
    except Exception as e:
        logging.warning("Не удалось удалить сообщение: %s", e)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


async def safe_edit(message: Message, text: str, reply_markup: InlineKeyboardMarkup):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        if "message is not modified" not in str(e):
            logging.warning("Ошибка редактирования сообщения: %s", e)


async def ensure_admin_cb(cb: CallbackQuery) -> bool:
    if not cb.message:
        await cb.answer()
        return False

    if cb.message.chat.type != ChatType.PRIVATE:
        await cb.answer("Панель доступна только в личных сообщениях.", show_alert=True)
        return False

    if not is_owner(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return False

    return True


# ---------------------------------------------------------------
# Клавиатуры и рендеринг меню
# ---------------------------------------------------------------

def main_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Ветки", callback_data="tl:0")],
            [InlineKeyboardButton(text="📊 Текущие настройки", callback_data="sum")],
            [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
        ]
    )


def back_main_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="ml")]
        ]
    )


def cancel_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ]
    )


async def build_topics_page_markup(chat_id: int, page: int) -> Tuple[InlineKeyboardMarkup, int]:
    topics = await get_topics(chat_id)
    configured = await get_configured_topic_ids(chat_id)

    total_pages = max(1, (len(topics) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    rows = []

    if topics:
        start = page * PAGE_SIZE
        end = start + PAGE_SIZE

        for topic_id, name in topics[start:end]:
            marker = "⚙️" if topic_id in configured else "・"

            if topic_id == 0:
                base_name = name or "General"
            else:
                base_name = name or "Ветка"

            label = f"{marker} {base_name} | {topic_id}"
            label = truncate_text(label, 60)

            rows.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=f"tp:{topic_id}",
                    )
                ]
            )

        nav = []

        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="⬅️",
                    callback_data=f"tl:{page - 1}",
                )
            )

        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    text="➡️",
                    callback_data=f"tl:{page + 1}",
                )
            )

        if nav:
            rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Главное меню",
                callback_data="ml",
            )
        ]
    )

    return InlineKeyboardMarkup(inline_keyboard=rows), page


async def render_main(message: Message, edit: bool = False):
    text = (
        "🤖 <b>Панель управления фильтром</b>\n\n"
        f"Группа: <code>{GROUP_ID}</code>\n\n"
        "Разделы:\n"
        "📋 Ветки — настройка тем\n"
        "📊 Текущие настройки — посмотреть, что включено\n"
        "ℹ️ Помощь — инструкция"
    )

    markup = main_markup()

    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def render_help(message: Message, edit: bool = False):
    text = (
        "ℹ️ <b>Помощь</b>\n\n"
        "1. Отправь в личке боту /admin.\n"
        "2. Нажми «Ветки».\n"
        "3. Выбери тему.\n"
        "4. Нажми на тип сообщений.\n"
        "5. Выбери режим:\n"
        "✅ Разрешить всем\n"
        "👥 Только выбранные пользователи\n"
        "🚫 Удалять у всех\n\n"
        "Если выбран режим «Только выбранные пользователи», "
        "то нужно добавить разрешённые user_id.\n\n"
        "Ввести ID можно кнопкой «✍️ Ввести ID» или командой:\n"
        "<code>/setid topic_id type_code user_id allow|delete</code>\n\n"
        "Пример:\n"
        "<code>/setid 12 p 123456 allow</code>\n\n"
        "Если тема не отображается в списке, отправь в нужную ветку группы команду:\n"
        "<code>/addtopic Название ветки</code>\n\n"
        "Темы без настроек не трогаются.\n\n"
        "Кнопка «Сбросить ветку» полностью убирает настройки ветки."
    )

    markup = back_main_markup()

    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def render_topic_list(message: Message, page: int = 0, edit: bool = False):
    topics = await get_topics(GROUP_ID)
    markup, _ = await build_topics_page_markup(GROUP_ID, page)

    if topics:
        text = (
            "📋 <b>Ветки</b>\n\n"
            "Выбери ветку для настройки.\n\n"
            "Обозначения:\n"
            "⚙️ — есть правила\n"
            "・ — правил пока нет"
        )
    else:
        text = (
            "📋 <b>Ветки</b>\n\n"
            "Пока нет известных боту веток.\n\n"
            "Чтобы добавить ветку в панель, отправь в нужную ветку группы команду:\n"
            "<code>/addtopic Название ветки</code>\n\n"
            "Также бот может увидеть тему при её создании или переименовании."
        )

    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def render_topic_settings(message: Message, topic_id: int, edit: bool = False):
    name = await get_topic_name(GROUP_ID, topic_id)
    modes = await get_modes_for_topic(GROUP_ID, topic_id)

    text = (
        f"⚙️ <b>{html_escape(topic_display(topic_id, name))}</b>\n"
        f"ID: <code>{topic_id}</code>\n\n"
        "Нажми на тип сообщений, чтобы настроить.\n\n"
        "Обозначения:\n"
        "✅ — разрешено всем\n"
        "👥 — только выбранные пользователи\n"
        "🚫 — удалять у всех\n"
        "➖ — правило не задано"
    )

    rows = []

    for ctype in CONTENT_TYPES_ORDER:
        code = CONTENT_TYPES[ctype]["code"]
        mode = modes.get(ctype)
        icon = MODE_ICONS.get(mode, "➖")

        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {CONTENT_TYPES[ctype]['name']}",
                    callback_data=f"ct:{topic_id}:{code}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🧹 Сбросить ветку",
                callback_data=f"rst:{topic_id}",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К веткам",
                callback_data="tl:0",
            )
        ]
    )

    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def render_type_settings(message: Message, topic_id: int, ctype: str, edit: bool = False):
    code = CONTENT_TYPES[ctype]["code"]
    topic_name = await get_topic_name(GROUP_ID, topic_id)
    mode = await get_mode(GROUP_ID, topic_id, ctype)
    users_count = await count_user_rules_for_type(GROUP_ID, topic_id, ctype)

    mode_text = MODE_NAMES.get(mode, "Не настроено")

    text = (
        f"Настройка типа: <b>{html_escape(CONTENT_TYPES[ctype]['name'])}</b>\n"
        f"Ветка: <b>{html_escape(topic_display(topic_id, topic_name))}</b>\n"
        f"ID ветки: <code>{topic_id}</code>\n\n"
        f"Текущий режим: <b>{html_escape(mode_text)}</b>\n"
        f"Пользователей в списке: <code>{users_count}</code>\n\n"
        "Режим «Только выбранные пользователи» разрешает отправку только тем, "
        "кто добавлен в список с правилом ✅ allow."
    )

    rows = []

    allow_label = "✅ Разрешить всем"
    selected_label = "👥 Только выбранные"
    deny_label = "🚫 Удалять у всех"

    if mode == "allow":
        allow_label = "• ✅ Разрешить всем"
    elif mode == "selected":
        selected_label = "• 👥 Только выбранные"
    elif mode == "deny":
        deny_label = "• 🚫 Удалять у всех"

    rows.append(
        [
            InlineKeyboardButton(
                text=allow_label,
                callback_data=f"sm:{topic_id}:{code}:allow",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text=selected_label,
                callback_data=f"sm:{topic_id}:{code}:selected",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text=deny_label,
                callback_data=f"sm:{topic_id}:{code}:deny",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text=f"👤 Пользователи ({users_count})",
                callback_data=f"su:{topic_id}:{code}",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="✍️ Ввести ID",
                callback_data=f"au:{topic_id}:{code}",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"tp:{topic_id}",
            )
        ]
    )

    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def render_user_list(message: Message, topic_id: int, ctype: str, edit: bool = False):
    code = CONTENT_TYPES[ctype]["code"]
    topic_name = await get_topic_name(GROUP_ID, topic_id)
    rules = await list_user_rules_for_type(GROUP_ID, topic_id, ctype)
    mode = await get_mode(GROUP_ID, topic_id, ctype)

    mode_text = MODE_NAMES.get(mode, "Не настроено")

    text = (
        f"👤 <b>Пользователи</b>\n"
        f"Тип: <b>{html_escape(CONTENT_TYPES[ctype]['name'])}</b>\n"
        f"Ветка: <b>{html_escape(topic_display(topic_id, topic_name))}</b>\n\n"
        f"Режим типа: <b>{html_escape(mode_text)}</b>\n\n"
        "Нажатие по пользователю переключает:\n"
        "✅ allow ↔ 🗑 delete\n\n"
        "✖️ — удалить правило пользователя."
    )

    rows = []

    if rules:
        for user_id, rule in rules[:20]:
            icon = "✅" if rule == "allow" else "🗑"

            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{icon} {user_id}",
                        callback_data=f"ut:{topic_id}:{code}:{user_id}",
                    ),
                    InlineKeyboardButton(
                        text="✖️",
                        callback_data=f"du:{topic_id}:{code}:{user_id}",
                    ),
                ]
            )

        if len(rules) > 20:
            text += "\n\nПоказаны первые 20 пользователей."
    else:
        text += "\n\nСписок пуст."

    rows.append(
        [
            InlineKeyboardButton(
                text="✍️ Ввести ID",
                callback_data=f"au:{topic_id}:{code}",
            )
        ]
    )

    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"ct:{topic_id}:{code}",
            )
        ]
    )

    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def render_summary(message: Message, edit: bool = False):
    rules = await get_all_topic_rules(GROUP_ID)

    if not rules:
        text = (
            "📊 <b>Текущие настройки</b>\n\n"
            "Нет настроенных веток.\n"
            "Темы без настроек не трогаются."
        )
        markup = back_main_markup()

        if edit:
            await safe_edit(message, text, markup)
        else:
            await message.answer(text, reply_markup=markup)
        return

    by_topic: Dict[int, Dict[str, str]] = {}

    for topic_id, ctype, mode in rules:
        by_topic.setdefault(topic_id, {})[ctype] = mode

    lines = ["📊 <b>Текущие настройки</b>\n"]

    shown_topics = list(by_topic.items())[:20]

    for topic_id, modes in shown_topics:
        topic_name = await get_topic_name(GROUP_ID, topic_id)
        lines.append(f"\n<b>{html_escape(topic_display(topic_id, topic_name))}</b>")

        for ctype in CONTENT_TYPES_ORDER:
            mode = modes.get(ctype)
            if mode:
                lines.append(
                    f"{CONTENT_TYPES[ctype]['name']}: {html_escape(MODE_NAMES.get(mode, mode))}"
                )

    if len(by_topic) > 20:
        lines.append("\n\nПоказаны первые 20 настроенных веток.")

    text = "\n".join(lines)
    markup = back_main_markup()

    if edit:
        await safe_edit(message, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------
# Команды в личке
# ---------------------------------------------------------------

@router.message(Command("start"), F.chat.type == ChatType.PRIVATE)
async def cmd_start_private(message: Message):
    if not message.from_user:
        return

    if not is_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    await render_main(message, edit=False)


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def cmd_admin_private(message: Message):
    if not message.from_user:
        return

    if not is_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    await render_main(message, edit=False)


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cmd_cancel_private(message: Message, state: FSMContext):
    if not message.from_user:
        return

    if not is_owner(message.from_user.id):
        return

    await state.clear()
    await render_main(message, edit=False)


@router.message(Command("setid"), F.chat.type == ChatType.PRIVATE)
async def cmd_set_user_id(message: Message):
    if not message.from_user:
        return

    if not is_owner(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    usage = (
        "Использование:\n"
        "<code>/setid topic_id type_code user_id allow|delete</code>\n\n"
        "Пример:\n"
        "<code>/setid 12 p 123456 allow</code>\n\n"
        "Коды типов:\n"
        "t — текст\n"
        "p — фото\n"
        "v — видео\n"
        "s — стикеры\n"
        "g — GIF\n"
        "a — музыка\n"
        "d — файлы\n"
        "o — голосовые\n"
        "n — видеосообщения\n"
        "l — опросы"
    )

    args = (message.text or "").split()

    if len(args) != 5:
        await message.answer(usage)
        return

    try:
        topic_id = int(args[1])
        user_id = int(args[3])
    except ValueError:
        await message.answer("topic_id и user_id должны быть числами.")
        return

    code = args[2].lower()
    rule = args[4].lower()

    ctype = CODE_TO_TYPE.get(code)

    if not ctype:
        await message.answer(usage)
        return

    if rule not in {"allow", "delete"}:
        await message.answer("Правило должно быть allow или delete.")
        return

    await ensure_topic_exists(GROUP_ID, topic_id)
    await set_user_rule(GROUP_ID, topic_id, ctype, user_id, rule)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⬅️ К пользователям",
                    callback_data=f"su:{topic_id}:{code}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ К типу",
                    callback_data=f"ct:{topic_id}:{code}",
                )
            ],
        ]
    )

    await message.answer(
        f"✅ Сохранено.\n"
        f"Тема: <code>{topic_id}</code>\n"
        f"Тип: <b>{html_escape(CONTENT_TYPES[ctype]['name'])}</b>\n"
        f"Пользователь: <code>{user_id}</code>\n"
        f"Правило: <b>{rule}</b>",
        reply_markup=markup,
    )


# ---------------------------------------------------------------
# Обработка ввода user_id через кнопку
# ---------------------------------------------------------------

@router.message(AddUser.wait_id, F.chat.type == ChatType.PRIVATE)
async def process_user_id(message: Message, state: FSMContext):
    if not message.from_user:
        return

    if not is_owner(message.from_user.id):
        return

    raw_text = (message.text or "").strip()

    if raw_text.lower() in ("/cancel", "cancel"):
        await state.clear()
        await render_main(message, edit=False)
        return

    if raw_text.startswith("/"):
        await message.answer(
            "Сейчас нужно ввести ID пользователя.\n\n"
            "Пример:\n"
            "<code>123456 allow</code>\n\n"
            "Для отмены отправь /cancel."
        )
        return

    parts = raw_text.split(maxsplit=1)

    try:
        user_id = int(parts[0])
    except (ValueError, IndexError):
        await message.answer(
            "Нужно отправить ID пользователя.\n\n"
            "Примеры:\n"
            "<code>123456 allow</code> — разрешить\n"
            "<code>123456 delete</code> — удалять\n\n"
            "Или отправь только ID, и я предложу выбрать правило кнопками."
        )
        return

    data = await state.get_data()
    topic_id = data.get("topic_id")
    ctype = data.get("ctype")

    if not topic_id or not ctype:
        await state.clear()
        await render_main(message, edit=False)
        return

    code = CONTENT_TYPES[ctype]["code"]

    # Если прислали сразу: 123456 allow или 123456 delete
    if len(parts) == 2:
        rule = parts[1].strip().lower()

        if rule not in {"allow", "delete"}:
            await message.answer(
                "Правило должно быть <code>allow</code> или <code>delete</code>.\n\n"
                "Пример:\n"
                "<code>123456 allow</code>"
            )
            return

        await state.clear()
        await ensure_topic_exists(GROUP_ID, topic_id)
        await set_user_rule(GROUP_ID, topic_id, ctype, user_id, rule)

        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⬅️ К пользователям",
                        callback_data=f"su:{topic_id}:{code}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ К типу",
                        callback_data=f"ct:{topic_id}:{code}",
                    )
                ],
            ]
        )

        await message.answer(
            f"✅ Сохранено.\n"
            f"Пользователь: <code>{user_id}</code>\n"
            f"Правило: <b>{rule}</b>",
            reply_markup=markup,
        )

        return

    # Если прислали только ID, предлагаем выбрать правило кнопками
    await state.clear()
    await ensure_topic_exists(GROUP_ID, topic_id)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Разрешить",
                    callback_data=f"ar:{topic_id}:{code}:{user_id}:allow",
                ),
                InlineKeyboardButton(
                    text="🗑 Удалять",
                    callback_data=f"ar:{topic_id}:{code}:{user_id}:delete",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Отмена",
                    callback_data=f"su:{topic_id}:{code}",
                )
            ],
        ]
    )

    await message.answer(
        f"Выбери правило для пользователя <code>{user_id}</code>:",
        reply_markup=markup,
    )


# ---------------------------------------------------------------
# Инлайн-кнопки в личке
# ---------------------------------------------------------------

@router.callback_query(F.data == "ml")
async def cb_main(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    await render_main(cb.message, edit=True)
    await cb.answer()


@router.callback_query(F.data == "sum")
async def cb_summary(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    await render_summary(cb.message, edit=True)
    await cb.answer()


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    await render_help(cb.message, edit=True)
    await cb.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    if not await ensure_admin_cb(cb):
        return

    await state.clear()
    await render_main(cb.message, edit=True)
    await cb.answer("Отменено.")


@router.callback_query(F.data.startswith("tl:"))
async def cb_topic_list(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    try:
        page = int(cb.data.split(":")[1])
    except Exception:
        page = 0

    await render_topic_list(cb.message, page=page, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("tp:"))
async def cb_topic_settings(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    try:
        topic_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Ошибка темы.")
        return

    await render_topic_settings(cb.message, topic_id, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("ct:"))
async def cb_type_settings(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer()
        return

    try:
        topic_id = int(parts[1])
    except Exception:
        await cb.answer("Ошибка темы.")
        return

    code = parts[2]
    ctype = CODE_TO_TYPE.get(code)

    if not ctype:
        await cb.answer("Неизвестный тип.")
        return

    await render_type_settings(cb.message, topic_id, ctype, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("sm:"))
async def cb_set_mode(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    parts = cb.data.split(":")
    if len(parts) != 4:
        await cb.answer()
        return

    try:
        topic_id = int(parts[1])
    except Exception:
        await cb.answer("Ошибка темы.")
        return

    code = parts[2]
    mode = parts[3]

    ctype = CODE_TO_TYPE.get(code)

    if not ctype or mode not in {"allow", "selected", "deny"}:
        await cb.answer("Некорректные данные.")
        return

    await ensure_topic_exists(GROUP_ID, topic_id)
    await set_mode(GROUP_ID, topic_id, ctype, mode)
    await render_type_settings(cb.message, topic_id, ctype, edit=True)
    await cb.answer("Режим обновлён.")


@router.callback_query(F.data.startswith("rst:"))
async def cb_reset_topic(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    try:
        topic_id = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Ошибка темы.")
        return

    await reset_topic(GROUP_ID, topic_id)
    await render_topic_settings(cb.message, topic_id, edit=True)
    await cb.answer("Ветка сброшена.")


@router.callback_query(F.data.startswith("su:"))
async def cb_user_list(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer()
        return

    try:
        topic_id = int(parts[1])
    except Exception:
        await cb.answer("Ошибка темы.")
        return

    code = parts[2]
    ctype = CODE_TO_TYPE.get(code)

    if not ctype:
        await cb.answer("Неизвестный тип.")
        return

    await render_user_list(cb.message, topic_id, ctype, edit=True)
    await cb.answer()


@router.callback_query(F.data.startswith("au:"))
async def cb_add_user_start(cb: CallbackQuery, state: FSMContext):
    if not await ensure_admin_cb(cb):
        return

    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer()
        return

    try:
        topic_id = int(parts[1])
    except Exception:
        await cb.answer("Ошибка темы.")
        return

    code = parts[2]
    ctype = CODE_TO_TYPE.get(code)

    if not ctype:
        await cb.answer("Неизвестный тип.")
        return

    await state.set_state(AddUser.wait_id)
    await state.update_data(topic_id=topic_id, ctype=ctype)

    await safe_edit(
        cb.message,
        "✍️ Отправь ID пользователя и правило через пробел.\n\n"
        "Примеры:\n"
        "<code>123456 allow</code> — разрешить\n"
        "<code>123456 delete</code> — удалять\n\n"
        "Если отправишь только ID, я предложу выбрать правило кнопками.\n\n"
        "Для отмены нажми кнопку ниже или отправь /cancel.",
        cancel_markup(),
    )

    await cb.answer()


@router.callback_query(F.data.startswith("ar:"))
async def cb_add_user_rule(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    parts = cb.data.split(":")
    if len(parts) != 5:
        await cb.answer()
        return

    try:
        topic_id = int(parts[1])
        user_id = int(parts[3])
    except Exception:
        await cb.answer("Ошибка данных.")
        return

    code = parts[2]
    rule = parts[4]

    ctype = CODE_TO_TYPE.get(code)

    if not ctype or rule not in {"allow", "delete"}:
        await cb.answer("Некорректные данные.")
        return

    await ensure_topic_exists(GROUP_ID, topic_id)
    await set_user_rule(GROUP_ID, topic_id, ctype, user_id, rule)
    await render_user_list(cb.message, topic_id, ctype, edit=True)
    await cb.answer("Пользователь добавлен.")


@router.callback_query(F.data.startswith("du:"))
async def cb_delete_user_rule(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    parts = cb.data.split(":")
    if len(parts) != 4:
        await cb.answer()
        return

    try:
        topic_id = int(parts[1])
        user_id = int(parts[3])
    except Exception:
        await cb.answer("Ошибка данных.")
        return

    code = parts[2]
    ctype = CODE_TO_TYPE.get(code)

    if not ctype:
        await cb.answer("Неизвестный тип.")
        return

    await delete_user_rule(GROUP_ID, topic_id, ctype, user_id)
    await render_user_list(cb.message, topic_id, ctype, edit=True)
    await cb.answer("Правило пользователя удалено.")


@router.callback_query(F.data.startswith("ut:"))
async def cb_toggle_user_rule(cb: CallbackQuery):
    if not await ensure_admin_cb(cb):
        return

    parts = cb.data.split(":")
    if len(parts) != 4:
        await cb.answer()
        return

    try:
        topic_id = int(parts[1])
        user_id = int(parts[3])
    except Exception:
        await cb.answer("Ошибка данных.")
        return

    code = parts[2]
    ctype = CODE_TO_TYPE.get(code)

    if not ctype:
        await cb.answer("Неизвестный тип.")
        return

    current = await get_user_rule(GROUP_ID, topic_id, ctype, user_id)

    if current == "allow":
        new_rule = "delete"
    else:
        new_rule = "allow"

    await set_user_rule(GROUP_ID, topic_id, ctype, user_id, new_rule)
    await render_user_list(cb.message, topic_id, ctype, edit=True)
    await cb.answer("Правило пользователя переключено.")


# ---------------------------------------------------------------
# Команды в группе
# ---------------------------------------------------------------

@router.message(Command("addtopic"), F.chat.id == GROUP_ID)
async def cmd_add_topic(message: Message, bot: Bot):
    if not message.from_user:
        return

    admin_ok = is_owner(message.from_user.id) or await is_chat_admin(
        bot,
        message.chat.id,
        message.from_user.id,
    )

    if not admin_ok:
        await message.answer("⛔ Эту команду может использовать только администратор.")
        return

    topic_id = message.message_thread_id or 0

    parts = (message.text or "").split(maxsplit=1)
    name = parts[1].strip() if len(parts) > 1 else None

    await upsert_topic(GROUP_ID, topic_id, name)

    await message.answer(
        f"✅ Ветка сохранена в панели:\n"
        f"<b>{html_escape(topic_display(topic_id, name))}</b>"
    )


# ---------------------------------------------------------------
# Отслеживание тем
# ---------------------------------------------------------------

@router.message(F.chat.id == GROUP_ID, F.forum_topic_created)
async def on_forum_topic_created(message: Message):
    topic_id = message.message_thread_id or 0

    if not topic_id:
        return

    name = None
    if message.forum_topic_created:
        name = message.forum_topic_created.name

    await upsert_topic(GROUP_ID, topic_id, name)


@router.message(F.chat.id == GROUP_ID, F.forum_topic_edited)
async def on_forum_topic_edited(message: Message):
    topic_id = message.message_thread_id or 0

    if not topic_id:
        return

    name = None

    if message.forum_topic_edited:
        name = getattr(message.forum_topic_edited, "name", None)

    if name:
        await upsert_topic(GROUP_ID, topic_id, name)


# ---------------------------------------------------------------
# Фильтрация сообщений
# ---------------------------------------------------------------

@router.message(F.chat.id == GROUP_ID)
async def filter_messages(message: Message, bot: Bot):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not message.from_user:
        return

    if message.from_user.id == bot.id:
        return

    # Команды не удаляем, чтобы не ломать управление ботом.
    if message.text and message.text.startswith("/"):
        return

    topic_id = message.message_thread_id or 0

    # Если видим сообщение в теме, запоминаем её ID.
    if topic_id:
        await ensure_topic_exists(GROUP_ID, topic_id)

    ctype = detect_content_type(message)

    if not ctype:
        return

    user_rule = await get_user_rule(
        GROUP_ID,
        topic_id,
        ctype,
        message.from_user.id,
    )

    # Персональное правило пользователя имеет высокий приоритет.
    if user_rule == "delete":
        await try_delete(message)
        return

    if user_rule == "allow":
        return

    mode = await get_mode(GROUP_ID, topic_id, ctype)

    # Если тема/тип не настроены — не трогаем.
    if mode is None:
        return

    if mode == "allow":
        return

    if mode == "deny":
        await try_delete(message)
        return

    # selected: если user_rule allow уже отработал выше,
    # значит пользователь не в белом списке.
    if mode == "selected":
        await try_delete(message)
        return


# ---------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------

async def main():
    logging.basicConfig(level=logging.INFO)

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
