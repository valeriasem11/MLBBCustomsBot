# -*- coding: utf-8 -*-
"""
Telegram-бот для организации кастомок в Mobile Legends.

Что умеет:
- Регистрация профиля игрока (никнейм, ID в игре, 2 роли из 5)
- Админы создают кастомку с указанием времени
- Игроки регистрируются на кастомку через кнопку в беседе
- Бот сам напоминает за 15 минут и в момент старта тегает всех записавшихся
- Админы могут посмотреть список зарегистрированных и отменить кастомку

Установка библиотеки (один раз, в терминале Thonny -> Tools -> Manage packages):
    pip install aiogram

Перед запуском:
1. Впиши свой токен бота в BOT_TOKEN (получить у @BotFather)
2. Впиши Telegram ID админов в ADMIN_IDS (узнать свой ID можно у @userinfobot)
"""

import asyncio
import os
import random
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ============================== НАСТРОЙКИ ==============================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

# Супер-админы: работают в любой беседе, независимо от статуса в Telegram-группе.
# Обычно тут достаточно только твоего ID — все остальные, кто является
# настоящим администратором конкретной беседы в Telegram, получают права
# автоматически, без ручного добавления сюда.
ADMIN_IDS = [828533150]

ROLES = ["Боец", "Лес", "Маг", "Стрелок", "Роум", "Генералист"]

RANKS = ["Эпик", "Легенда", "Мифик", "Мифическая честь", "Мифическая слава", "100+ звёзд"]

DB_PATH = "data/mlbb_bot.db"

# Как заранее присылать напоминание до старта кастомки
REMINDER_BEFORE_MINUTES = 15

# Второе напоминание (только тем, кто ещё не подтвердил участие)
SECOND_REMINDER_BEFORE_MINUTES = 5

# =========================================================================

# Все времена кастомок считаются и хранятся по московскому времени —
# независимо от того, в каком часовом поясе физически находится сервер,
# на котором крутится бот (Амстердам, Москва, где угодно).
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def now_msk() -> datetime:
    """Текущее время по Москве, независимо от часового пояса сервера."""
    return datetime.now(MOSCOW_TZ)


def parse_stored_time(iso_string: str) -> datetime:
    """
    Разбирает время, сохранённое в базе. Если время сохранено без
    часового пояса (данные из старой версии бота, до этого исправления) —
    считаем его московским, чтобы сравнения со временем не падали с ошибкой.
    """
    dt = datetime.fromisoformat(iso_string)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MOSCOW_TZ)
    return dt

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


def add_chat_admin(chat_id: int, user_id: int):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO chat_admins (chat_id, user_id) VALUES (?, ?)",
        (chat_id, user_id)
    )
    conn.commit()
    conn.close()


def remove_chat_admin(chat_id: int, user_id: int):
    conn = db()
    conn.execute("DELETE FROM chat_admins WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()


def get_chat_admins(chat_id: int):
    conn = db()
    rows = conn.execute("SELECT user_id FROM chat_admins WHERE chat_id = ?", (chat_id,)).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def clear_chat_admins(chat_id: int):
    conn = db()
    conn.execute("DELETE FROM chat_admins WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


async def is_admin(user_id: int, chat_id: int = None) -> bool:
    """
    Проверяет права администратора бота.

    Порядок проверки:
    1. ADMIN_IDS — супер-админы, работают в любой беседе всегда.
    2. Если для этой конкретной беседы кто-то явно настроил список
       "ботов-админов" (командой /addbotadmin) — доступ есть только
       у людей из этого списка, и всё.
    3. Если для беседы такой список не настраивали — по умолчанию
       доступ есть у любого реального администратора/создателя этой
       беседы в самом Telegram.
    """
    if user_id in ADMIN_IDS:
        return True
    if chat_id is None:
        return False

    custom_admins = get_chat_admins(chat_id)
    if custom_admins:
        return user_id in custom_admins

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def is_chat_creator(user_id: int, chat_id: int) -> bool:
    """Только создатель беседы (или супер-админ) может менять список ботов-админов."""
    if user_id in ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status == "creator"
    except Exception:
        return False


# ------------------------------ Меню команд (то, что видно по кнопке "/") ------------------------------

PLAYER_COMMANDS = [
    BotCommand(command="start", description="Создать / посмотреть профиль"),
    BotCommand(command="profile", description="Профиль и его изменение"),
    BotCommand(command="active", description="Активная кастомка сейчас"),
    BotCommand(command="teams", description="Посмотреть команды"),
    BotCommand(command="history", description="Прошлые результаты кастомок"),
    BotCommand(command="mystats", description="Моя статистика в этой беседе"),
    BotCommand(command="leaderboard", description="Рейтинг беседы"),
]

ADMIN_COMMANDS = PLAYER_COMMANDS + [
    BotCommand(command="newcustom", description="Создать кастомку"),
    BotCommand(command="list", description="Список зарегистрированных"),
    BotCommand(command="removeplayer", description="Удалить участника из кастомки"),
    BotCommand(command="maketeams", description="Разбить игроков на команды"),
    BotCommand(command="setresult", description="Указать победителя и MVP"),
    BotCommand(command="cancelcustom", description="Отменить кастомку"),
    BotCommand(command="addbotadmin", description="Добавить админа бота (Reply, только создатель)"),
    BotCommand(command="removebotadmin", description="Убрать админа бота (Reply, только создатель)"),
    BotCommand(command="listbotadmins", description="Кто может управлять ботом"),
    BotCommand(command="resetbotadmins", description="Сбросить список админов бота"),
    BotCommand(command="resetstats", description="Очистить статистику и историю (только создатель)"),
]

# ------------------------------ Автообновление меню команд ------------------------------
# Чтобы не нужно было вручную запускать /setupcommands после каждого обновления
# кода: при первом сообщении/нажатии кнопки в каждой беседе за время работы
# бота (например, сразу после перезапуска) меню команд для этой беседы
# настраивается само, в фоне, без участия админа.

_menu_configured_chats = set()


async def _ensure_chat_menu_configured(chat):
    if chat is None or chat.type not in ("group", "supergroup"):
        return
    if chat.id in _menu_configured_chats:
        return
    _menu_configured_chats.add(chat.id)  # помечаем сразу, чтобы не дублировать попытки при параллельных апдейтах
    try:
        await bot.set_my_commands(PLAYER_COMMANDS, scope=BotCommandScopeChat(chat_id=chat.id))
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChatAdministrators(chat_id=chat.id))
    except Exception:
        pass


async def _auto_commands_middleware(handler, event, data):
    chat = getattr(event, "chat", None)
    if chat is None and getattr(event, "message", None) is not None:
        chat = event.message.chat
    await _ensure_chat_menu_configured(chat)
    return await handler(event, data)


router.message.middleware(_auto_commands_middleware)
router.callback_query.middleware(_auto_commands_middleware)


# ------------------------------ БАЗА ДАННЫХ ------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            nickname TEXT,
            game_id TEXT,
            role1 TEXT,
            role2 TEXT,
            rank TEXT
        )
    """)
    # Миграция: если база создавалась раньше (без колонки rank), добавляем её
    try:
        cur.execute("ALTER TABLE users ADD COLUMN rank TEXT")
    except sqlite3.OperationalError:
        pass  # колонка уже есть
    cur.execute("""
        CREATE TABLE IF NOT EXISTS customs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            event_time TEXT,
            status TEXT DEFAULT 'active',
            reminder_sent INTEGER DEFAULT 0,
            second_reminder_sent INTEGER DEFAULT 0,
            list_message_id INTEGER,
            winner_team INTEGER,
            mvp_user_id INTEGER
        )
    """)
    # Миграции для баз, созданных до этих изменений
    for column_sql in [
        "ALTER TABLE customs ADD COLUMN second_reminder_sent INTEGER DEFAULT 0",
        "ALTER TABLE customs ADD COLUMN winner_team INTEGER",
        "ALTER TABLE customs ADD COLUMN mvp_user_id INTEGER",
    ]:
        try:
            cur.execute(column_sql)
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    cur.execute("""
        CREATE TABLE IF NOT EXISTS registrations (
            custom_id INTEGER,
            user_id INTEGER,
            team_number INTEGER,
            team_role TEXT,
            attendance TEXT,
            PRIMARY KEY (custom_id, user_id)
        )
    """)
    # Миграция: если база создавалась раньше (без колонки team_number/attendance/team_role), добавляем их
    for column_sql in [
        "ALTER TABLE registrations ADD COLUMN team_number INTEGER",
        "ALTER TABLE registrations ADD COLUMN attendance TEXT",
        "ALTER TABLE registrations ADD COLUMN team_role TEXT",
    ]:
        try:
            cur.execute(column_sql)
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_admins (
            chat_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    conn.commit()
    conn.close()
    migrate_old_role_names()


# Если названия ролей в коде когда-то менялись, здесь можно один раз
# указать соответствие "старое название -> новое", и бот сам поправит
# всем, у кого в профиле осталось старое значение.
ROLE_RENAME_MAP = {
    "Exp": "Боец",
    "Jungle": "Лес",
    "Mid": "Мид",
    "Gold (Marksman)": "Стрелок",
    "Roam": "Роум",
}


def migrate_old_role_names():
    conn = db()
    for old_name, new_name in ROLE_RENAME_MAP.items():
        conn.execute("UPDATE users SET role1 = ? WHERE role1 = ?", (new_name, old_name))
        conn.execute("UPDATE users SET role2 = ? WHERE role2 = ?", (new_name, old_name))
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def save_user(user_id: int, nickname: str, game_id: str, role1: str, role2: str, rank: str):
    conn = db()
    conn.execute("""
        INSERT INTO users (user_id, nickname, game_id, role1, role2, rank)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            nickname=excluded.nickname,
            game_id=excluded.game_id,
            role1=excluded.role1,
            role2=excluded.role2,
            rank=excluded.rank
    """, (user_id, nickname, game_id, role1, role2, rank))
    conn.commit()
    conn.close()


# Поля профиля, которые можно менять по отдельности (ключ -> колонка в БД)
EDITABLE_FIELDS = {
    "nickname": "nickname",
    "game_id": "game_id",
    "rank": "rank",
}


def update_user_field(user_id: int, field: str, value: str):
    """Обновляет одно текстовое поле профиля (никнейм, ID или ранг)."""
    column = EDITABLE_FIELDS[field]
    conn = db()
    conn.execute(f"UPDATE users SET {column} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()


def update_user_roles(user_id: int, role1: str, role2: str):
    conn = db()
    conn.execute("UPDATE users SET role1 = ?, role2 = ? WHERE user_id = ?", (role1, role2, user_id))
    conn.commit()
    conn.close()


def get_active_custom(chat_id: int):
    conn = db()
    row = conn.execute(
        "SELECT * FROM customs WHERE chat_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (chat_id,)
    ).fetchone()
    conn.close()
    return row


def create_custom(chat_id: int, event_time: datetime) -> int:
    conn = db()
    cur = conn.execute(
        "INSERT INTO customs (chat_id, event_time, status) VALUES (?, ?, 'active')",
        (chat_id, event_time.isoformat())
    )
    conn.commit()
    custom_id = cur.lastrowid
    conn.close()
    return custom_id


def register_to_custom(custom_id: int, user_id: int) -> bool:
    conn = db()
    try:
        conn.execute(
            "INSERT INTO registrations (custom_id, user_id) VALUES (?, ?)",
            (custom_id, user_id)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def is_registered(custom_id: int, user_id: int) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT 1 FROM registrations WHERE custom_id = ? AND user_id = ?",
        (custom_id, user_id)
    ).fetchone()
    conn.close()
    return row is not None


def unregister_from_custom(custom_id: int, user_id: int):
    conn = db()
    conn.execute(
        "DELETE FROM registrations WHERE custom_id = ? AND user_id = ?",
        (custom_id, user_id)
    )
    conn.commit()
    conn.close()


def get_registrations(custom_id: int):
    conn = db()
    rows = conn.execute("""
        SELECT u.*, r.team_number, r.team_role, r.attendance FROM registrations r
        JOIN users u ON u.user_id = r.user_id
        WHERE r.custom_id = ?
    """, (custom_id,)).fetchall()
    conn.close()
    return rows


def mark_reminder_sent(custom_id: int):
    conn = db()
    conn.execute("UPDATE customs SET reminder_sent = 1 WHERE id = ?", (custom_id,))
    conn.commit()
    conn.close()


def mark_second_reminder_sent(custom_id: int):
    conn = db()
    conn.execute("UPDATE customs SET second_reminder_sent = 1 WHERE id = ?", (custom_id,))
    conn.commit()
    conn.close()


def update_custom_time(custom_id: int, new_event_time: datetime):
    """Меняет время начала кастомки и сбрасывает флаги напоминаний,
    чтобы они сработали заново относительно нового времени."""
    conn = db()
    conn.execute(
        "UPDATE customs SET event_time = ?, reminder_sent = 0, second_reminder_sent = 0 WHERE id = ?",
        (new_event_time.isoformat(), custom_id)
    )
    conn.commit()
    conn.close()


def set_attendance(custom_id: int, user_id: int, value: str):
    conn = db()
    conn.execute(
        "UPDATE registrations SET attendance = ? WHERE custom_id = ? AND user_id = ?",
        (value, custom_id, user_id)
    )
    conn.commit()
    conn.close()


def get_attendance(custom_id: int, user_id: int):
    conn = db()
    row = conn.execute(
        "SELECT attendance FROM registrations WHERE custom_id = ? AND user_id = ?",
        (custom_id, user_id)
    ).fetchone()
    conn.close()
    return row["attendance"] if row else None


def mark_unconfirmed_as_no_show(custom_id: int):
    """Всем, кто зарегистрировался, но так и не нажал «Готов» — ставим отметку."""
    conn = db()
    conn.execute(
        "UPDATE registrations SET attendance = 'no_show' WHERE custom_id = ? AND attendance IS NULL",
        (custom_id,)
    )
    conn.commit()
    conn.close()


def finish_custom(custom_id: int):
    conn = db()
    conn.execute("UPDATE customs SET status = 'finished' WHERE id = ?", (custom_id,))
    conn.commit()
    conn.close()


def cancel_custom_db(custom_id: int):
    conn = db()
    conn.execute("UPDATE customs SET status = 'cancelled' WHERE id = ?", (custom_id,))
    conn.commit()
    conn.close()


def get_all_active_customs():
    conn = db()
    rows = conn.execute("SELECT * FROM customs WHERE status = 'active'").fetchall()
    conn.close()
    return rows


def get_custom_by_id(custom_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM customs WHERE id = ?", (custom_id,)).fetchone()
    conn.close()
    return row


def get_last_finished_custom(chat_id: int):
    conn = db()
    row = conn.execute(
        "SELECT * FROM customs WHERE chat_id = ? AND status = 'finished' ORDER BY id DESC LIMIT 1",
        (chat_id,)
    ).fetchone()
    conn.close()
    return row


def set_custom_winner(custom_id: int, team_number: int):
    conn = db()
    conn.execute("UPDATE customs SET winner_team = ? WHERE id = ?", (team_number, custom_id))
    conn.commit()
    conn.close()


def set_custom_mvp(custom_id: int, user_id: int):
    conn = db()
    conn.execute("UPDATE customs SET mvp_user_id = ? WHERE id = ?", (user_id, custom_id))
    conn.commit()
    conn.close()


def get_results_history(chat_id: int, limit: int = 5):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM customs WHERE chat_id = ? AND winner_team IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    conn.close()
    return rows


def count_finished_customs(chat_id: int) -> int:
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM customs WHERE chat_id = ? AND status = 'finished'",
        (chat_id,)
    ).fetchone()
    conn.close()
    return row["c"]


def reset_chat_stats(chat_id: int):
    """
    Полностью удаляет завершённые кастомки этой беседы (и их регистрации) —
    то есть всё, из чего считаются /mystats, /leaderboard и /history.
    Активную (если есть) и отменённые кастомки не трогает.
    """
    conn = db()
    conn.execute("""
        DELETE FROM registrations
        WHERE custom_id IN (SELECT id FROM customs WHERE chat_id = ? AND status = 'finished')
    """, (chat_id,))
    conn.execute("DELETE FROM customs WHERE chat_id = ? AND status = 'finished'", (chat_id,))
    conn.commit()
    conn.close()


# ------------------------------ Статистика и рейтинг ------------------------------
# Статистика не хранится отдельно, а считается на лету из уже имеющихся
# данных (кастомки, команды, результаты) — так она всегда точная и не
# может "разъехаться" с реальной историей игр.

def get_player_stats(chat_id: int, user_id: int):
    conn = db()
    row = conn.execute("""
        SELECT
            COUNT(*) AS games,
            SUM(CASE WHEN c.winner_team = r.team_number THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN c.mvp_user_id = r.user_id THEN 1 ELSE 0 END) AS mvps
        FROM registrations r
        JOIN customs c ON c.id = r.custom_id
        WHERE c.chat_id = ? AND c.status = 'finished'
          AND r.team_number IS NOT NULL AND r.user_id = ?
    """, (chat_id, user_id)).fetchone()
    conn.close()
    games = row["games"] or 0
    wins = row["wins"] or 0
    mvps = row["mvps"] or 0
    return {"games": games, "wins": wins, "mvps": mvps}


def get_leaderboard(chat_id: int, limit: int = 10):
    conn = db()
    rows = conn.execute("""
        SELECT
            u.user_id, u.nickname,
            COUNT(*) AS games,
            SUM(CASE WHEN c.winner_team = r.team_number THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN c.mvp_user_id = r.user_id THEN 1 ELSE 0 END) AS mvps
        FROM registrations r
        JOIN customs c ON c.id = r.custom_id
        JOIN users u ON u.user_id = r.user_id
        WHERE c.chat_id = ? AND c.status = 'finished' AND r.team_number IS NOT NULL
        GROUP BY u.user_id
        ORDER BY wins DESC, mvps DESC, games DESC
        LIMIT ?
    """, (chat_id, limit)).fetchall()
    conn.close()
    return rows


# ------------------------------ Команды (Team 1, 2, 3...) ------------------------------

def set_team_number(custom_id: int, user_id: int, team_number, team_role: str = None):
    conn = db()
    conn.execute(
        "UPDATE registrations SET team_number = ?, team_role = ? WHERE custom_id = ? AND user_id = ?",
        (team_number, team_role, custom_id, user_id)
    )
    conn.commit()
    conn.close()


def clear_teams(custom_id: int):
    conn = db()
    conn.execute(
        "UPDATE registrations SET team_number = NULL, team_role = NULL WHERE custom_id = ?",
        (custom_id,)
    )
    conn.commit()
    conn.close()


def swap_players_teams(custom_id: int, user_id_1: int, user_id_2: int):
    """Меняет местами двух игроков между их текущими командами."""
    conn = db()
    row1 = conn.execute(
        "SELECT team_number FROM registrations WHERE custom_id = ? AND user_id = ?",
        (custom_id, user_id_1)
    ).fetchone()
    row2 = conn.execute(
        "SELECT team_number FROM registrations WHERE custom_id = ? AND user_id = ?",
        (custom_id, user_id_2)
    ).fetchone()
    team1 = row1["team_number"] if row1 else None
    team2 = row2["team_number"] if row2 else None
    conn.execute(
        "UPDATE registrations SET team_number = ? WHERE custom_id = ? AND user_id = ?",
        (team2, custom_id, user_id_1)
    )
    conn.execute(
        "UPDATE registrations SET team_number = ? WHERE custom_id = ? AND user_id = ?",
        (team1, custom_id, user_id_2)
    )
    conn.commit()
    conn.close()


def get_teams_grouped(custom_id: int):
    """Возвращает (словарь {номер_команды: [игроки]}, список запасных без команды)."""
    players = get_registrations(custom_id)
    teams = {}
    substitutes = []
    for p in players:
        team_number = p["team_number"]
        if team_number:
            teams.setdefault(team_number, []).append(p)
        else:
            substitutes.append(p)
    return teams, substitutes


def make_teams(custom_id: int, num_teams: int = None) -> int:
    """
    Формирует команды строго по 5 человек, стараясь закрыть все 5 базовых
    ролей (Боец/Лес/Маг/Стрелок/Роум) без повторов внутри одной команды.

    Для каждой роли сначала ищется игрок, у которого это основная роль,
    если такого нет — берётся тот, у кого это вторая роль, если и такого
    нет — берётся "Генералист" (он может закрыть любую роль). Роли,
    на которые меньше всего кандидатов, закрываются в первую очередь,
    чтобы редкие роли (например Роум) не "съедались" более популярными
    комбинациями раньше времени.

    Игроки, которые не поместились ни в одну команду, остаются
    запасными (team_number = NULL) — не набиваются насильно в команды.

    Возвращает итоговое число сформированных команд.
    """
    players = [dict(p) for p in get_registrations(custom_id)]
    total = len(players)

    if num_teams is None:
        num_teams = total // 5  # только полные команды по 5, остальное — в запас

    clear_teams(custom_id)

    if num_teams == 0:
        return 0

    core_roles = [r for r in ROLES if r != "Генералист"]

    def rank_strength(p):
        return RANKS.index(p["rank"]) if p["rank"] in RANKS else -1

    pool = players[:]
    random.shuffle(pool)  # чтобы при равных условиях состав был случайным, а не всегда одним и тем же

    teams = []

    for _ in range(num_teams):
        used_ids = {p["user_id"] for t in teams for p in t.values()}
        available = [p for p in pool if p["user_id"] not in used_ids]
        unfilled_roles = list(core_roles)
        assigned = {}

        # --- Фаза 1: закрываем роли только "профильными" игроками ---
        # (у кого эта роль реально указана как основная или вторая).
        # Генералисты в этой фазе не участвуют — их приберегаем на потом.
        while unfilled_roles:
            candidates_by_role = {}
            for role in unfilled_roles:
                cands = [p for p in available if p["role1"] == role or p["role2"] == role]
                if cands:
                    candidates_by_role[role] = cands

            if not candidates_by_role:
                break  # среди доступных специалистов больше нет ни на одну оставшуюся роль

            # закрываем сначала самую дефицитную роль (с наименьшим числом кандидатов)
            scarcest_role = min(candidates_by_role, key=lambda r: len(candidates_by_role[r]))
            candidates = candidates_by_role[scarcest_role]

            # приоритет: совпадение по основной роли > по второй, среди равных — выше ранг
            def priority(p, role=scarcest_role):
                tier = 0 if p["role1"] == role else 1
                return (tier, -rank_strength(p))

            candidates.sort(key=priority)
            chosen = candidates[0]

            assigned[scarcest_role] = chosen
            available.remove(chosen)
            unfilled_roles.remove(scarcest_role)

        # --- Фаза 2: оставшиеся пустые роли закрываем Генералистами ---
        if unfilled_roles:
            generalists = [
                p for p in available
                if p["role1"] == "Генералист" or p["role2"] == "Генералист"
            ]
            generalists.sort(key=lambda p: -rank_strength(p))
            for role in list(unfilled_roles):
                if not generalists:
                    break
                chosen = generalists.pop(0)
                assigned[role] = chosen
                available.remove(chosen)
                unfilled_roles.remove(role)

        # Если по ролям всё равно набралось меньше 5 (роли совсем не хватило
        # ни специалистов, ни генералистов) — добираем любыми оставшимися.
        # разнообразия ролей среди зарегистрированных) — добираем команду
        # до 5 лучшими из оставшихся, даже если роль при этом повторится.
        # Полная команда с повтором роли лучше, чем недоукомплектованная.
        if available:
            available.sort(key=lambda p: -rank_strength(p))
        while len(assigned) < 5 and available:
            filler = available.pop(0)
            key = filler["role1"]
            n = 2
            while key in assigned:
                key = f"{filler['role1']} ({n})"
                n += 1
            assigned[key] = filler

        teams.append(assigned)  # словарь {роль: игрок} для этой команды

    for team_number, role_map in enumerate(teams, start=1):
        for role, p in role_map.items():
            # если роль получилась с пометкой "(2)" и т.п. — сохраняем без неё
            clean_role = role.split(" (")[0]
            set_team_number(custom_id, p["user_id"], team_number, team_role=clean_role)

    return num_teams


# ------------------------------ FSM состояния ------------------------------

class Registration(StatesGroup):
    nickname = State()
    game_id = State()
    role1 = State()
    role2 = State()
    rank = State()


class EditProfile(StatesGroup):
    nickname = State()
    game_id = State()
    role1 = State()
    role2 = State()
    rank = State()


class NewCustom(StatesGroup):
    waiting_time = State()


class EditCustomTime(StatesGroup):
    waiting_time = State()


# ------------------------------ Клавиатуры ------------------------------

def roles_keyboard(exclude: str = None, exclude_generalist: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    for role in ROLES:
        if role == exclude:
            continue
        if exclude_generalist and role == "Генералист":
            continue
        buttons.append([InlineKeyboardButton(text=role, callback_data=f"role:{role}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def register_button(custom_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Зарегистрироваться", callback_data=f"reg:{custom_id}")],
        [InlineKeyboardButton(text="❌ Отменить регистрацию", callback_data=f"unreg:{custom_id}")],
        [InlineKeyboardButton(text="🕒 Изменить время", callback_data=f"edittime:{custom_id}")],
    ])


def rank_keyboard(prefix: str = "rank") -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=r, callback_data=f"{prefix}:{r}")] for r in RANKS]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def edit_profile_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_menu")]
    ])


def edit_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Никнейм", callback_data="editfield:nickname")],
        [InlineKeyboardButton(text="ID в игре", callback_data="editfield:game_id")],
        [InlineKeyboardButton(text="Роли", callback_data="editfield:roles")],
        [InlineKeyboardButton(text="Ранг", callback_data="editfield:rank")],
    ])


def ready_button(custom_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готов", callback_data=f"ready:{custom_id}")]
    ])


def attendance_icon(value) -> str:
    if value == "ready":
        return "✅"
    if value == "no_show":
        return "❌"
    return "⏳"


def winner_pick_keyboard(custom_id: int) -> InlineKeyboardMarkup:
    teams, _ = get_teams_grouped(custom_id)
    buttons = [
        [InlineKeyboardButton(text=f"Команда {n}", callback_data=f"result:team:{custom_id}:{n}")]
        for n in sorted(teams.keys())
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def mvp_pick_keyboard(custom_id: int, team_number: int) -> InlineKeyboardMarkup:
    teams, _ = get_teams_grouped(custom_id)
    players = teams.get(team_number, [])
    buttons = [
        [InlineKeyboardButton(text=p["nickname"], callback_data=f"result:mvp:{custom_id}:{p['user_id']}")]
        for p in players
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def teams_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔀 Обменять игроков", callback_data="teams:pick1")]
    ])


def remove_player_keyboard(custom_id: int) -> InlineKeyboardMarkup:
    players = get_registrations(custom_id)
    buttons = [
        [InlineKeyboardButton(
            text=f"{attendance_icon(p['attendance'])} {p['nickname']}",
            callback_data=f"removeplayer:{custom_id}:{p['user_id']}"
        )]
        for p in players
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="removeplayer:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def teams_player_picker_keyboard(custom_id: int, exclude_uid: int = None, first_uid: int = None) -> InlineKeyboardMarkup:
    teams, substitutes = get_teams_grouped(custom_id)
    buttons = []

    def add_button(p):
        if p["user_id"] == exclude_uid:
            return
        label = f"Команда {p['team_number']}" if p["team_number"] else "Запасной"
        text = f"{label} • {p['nickname']}"
        if first_uid is None:
            callback_data = f"teams:pick1sel:{p['user_id']}"
        else:
            callback_data = f"teams:pick2sel:{first_uid}:{p['user_id']}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=callback_data)])

    for team_number in sorted(teams.keys()):
        for p in teams[team_number]:
            add_button(p)
    for p in substitutes:
        add_button(p)

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="teams:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def render_teams_text(custom_id: int) -> str:
    teams, substitutes = get_teams_grouped(custom_id)
    if not teams and not substitutes:
        return "Пока никто не зарегистрировался на эту кастомку."
    if not teams:
        return "Команды ещё не сформированы.\nАдмин может создать их командой /maketeams."

    lines = ["🧩 <b>Команды</b>"]
    for team_number in sorted(teams.keys()):
        lines.append(f"\n<b>Команда {team_number}</b>")
        for p in teams[team_number]:
            role_shown = p["team_role"] or p["role1"]
            lines.append(f"• {p['nickname']} — {role_shown} — {p['rank']}")

    if substitutes:
        lines.append("\n<b>Запасные</b>")
        for p in substitutes:
            lines.append(f"• {p['nickname']} — {p['role1']}/{p['role2']} — {p['rank']}")

    return "\n".join(lines)


def profile_text(user_row) -> str:
    return (
        f"<blockquote>"
        f"Никнейм: {user_row['nickname']}\n\n"
        f"ID: <code>{user_row['game_id']}</code>\n\n"
        f"🏆 <b>Максимальный ранг</b>\n"
        f"{user_row['rank']}\n\n"
        f"🎮 <b>Основные роли</b>\n"
        f"• {user_row['role1']}\n"
        f"• {user_row['role2']}"
        f"</blockquote>"
    )


def mention(user_row) -> str:
    """HTML-ссылка с упоминанием игрока по его никнейму из профиля."""
    name = user_row["nickname"] or "Игрок"
    return f'<a href="tg://user?id={user_row["user_id"]}">{name}</a>'


# ------------------------------ Регистрация профиля /start ------------------------------

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if user:
        await message.reply(
            f"Привет! Твой профиль уже создан:\n\n{profile_text(user)}",
            reply_markup=edit_profile_button(),
            parse_mode="HTML"
        )
        return

    await message.reply(
        "Привет! Давай создадим твой игровой профиль для кастомок.\n\n"
        "Напиши свой никнейм в игре:"
    )
    await state.set_state(Registration.nickname)


@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user:
        await message.reply(
            "У тебя ещё нет профиля. Давай создадим его.\n\nНапиши свой никнейм в игре:"
        )
        await state.set_state(Registration.nickname)
        return

    await message.reply(
        f"👤 <b>ПРОФИЛЬ ИГРОКА</b>\n\n{profile_text(user)}",
        reply_markup=edit_profile_button(),
        parse_mode="HTML"
    )


@router.message(Registration.nickname)
async def reg_nickname(message: Message, state: FSMContext):
    await state.update_data(nickname=message.text.strip())
    await message.reply(
        "Теперь введите ваш игровой ID в формате: <code>123456789(1234)</code>",
        parse_mode="HTML"
    )
    await state.set_state(Registration.game_id)


@router.message(Registration.game_id)
async def reg_game_id(message: Message, state: FSMContext):
    await state.update_data(game_id=message.text.strip())
    await message.reply("Выбери первую роль:", reply_markup=roles_keyboard())
    await state.set_state(Registration.role1)


@router.callback_query(Registration.role1, F.data.startswith("role:"))
async def reg_role1(callback: CallbackQuery, state: FSMContext):
    role1 = callback.data.split(":", 1)[1]
    await state.update_data(role1=role1)

    if role1 == "Генералист":
        # Генералист — самостоятельный выбор, вторую роль не спрашиваем
        await state.update_data(role2="Генералист")
        await callback.message.edit_text(
            "Роль: Генералист (играет любую роль)\n\nТеперь выбери свой максимальный ранг:",
            reply_markup=rank_keyboard()
        )
        await state.set_state(Registration.rank)
        await callback.answer()
        return

    await callback.message.edit_text(
        f"Первая роль: {role1}\n\nТеперь выбери вторую роль:",
        reply_markup=roles_keyboard(exclude=role1, exclude_generalist=True)
    )
    await state.set_state(Registration.role2)
    await callback.answer()


@router.callback_query(Registration.role2, F.data.startswith("role:"))
async def reg_role2(callback: CallbackQuery, state: FSMContext):
    role2 = callback.data.split(":", 1)[1]
    await state.update_data(role2=role2)
    await callback.message.edit_text(
        f"Роли: {(await state.get_data())['role1']}, {role2}\n\nТеперь выбери свой максимальный ранг:",
        reply_markup=rank_keyboard()
    )
    await state.set_state(Registration.rank)
    await callback.answer()


@router.callback_query(Registration.rank, F.data.startswith("rank:"))
async def reg_rank(callback: CallbackQuery, state: FSMContext):
    rank = callback.data.split(":", 1)[1]
    data = await state.get_data()
    save_user(
        user_id=callback.from_user.id,
        nickname=data["nickname"],
        game_id=data["game_id"],
        role1=data["role1"],
        role2=data["role2"],
        rank=rank,
    )
    user = get_user(callback.from_user.id)
    await callback.message.edit_text(
        f"Готово! Твой профиль сохранён ✅\n\n{profile_text(user)}\n\n"
        f"Теперь можешь регистрироваться на кастомки в беседе.",
        reply_markup=edit_profile_button(),
        parse_mode="HTML"
    )
    await state.clear()
    await callback.answer()


# ------------------------------ Изменение отдельных полей профиля ------------------------------

@router.callback_query(F.data == "edit_menu")
async def cb_edit_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "Что хочешь изменить?",
        reply_markup=edit_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "editfield:nickname")
async def cb_edit_nickname(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Напиши новый никнейм:")
    await state.set_state(EditProfile.nickname)
    await callback.answer()


@router.message(EditProfile.nickname)
async def edit_nickname_save(message: Message, state: FSMContext):
    update_user_field(message.from_user.id, "nickname", message.text.strip())
    user = get_user(message.from_user.id)
    await message.reply(
        f"Никнейм обновлён ✅\n\n{profile_text(user)}",
        reply_markup=edit_profile_button(),
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "editfield:game_id")
async def cb_edit_game_id(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Введите новый игровой ID в формате: <code>123456789(1234)</code>",
        parse_mode="HTML"
    )
    await state.set_state(EditProfile.game_id)
    await callback.answer()


@router.message(EditProfile.game_id)
async def edit_game_id_save(message: Message, state: FSMContext):
    update_user_field(message.from_user.id, "game_id", message.text.strip())
    user = get_user(message.from_user.id)
    await message.reply(
        f"ID обновлён ✅\n\n{profile_text(user)}",
        reply_markup=edit_profile_button(),
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "editfield:roles")
async def cb_edit_roles(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выбери первую роль:", reply_markup=roles_keyboard())
    await state.set_state(EditProfile.role1)
    await callback.answer()


@router.callback_query(EditProfile.role1, F.data.startswith("role:"))
async def edit_role1(callback: CallbackQuery, state: FSMContext):
    role1 = callback.data.split(":", 1)[1]

    if role1 == "Генералист":
        update_user_roles(callback.from_user.id, "Генералист", "Генералист")
        user = get_user(callback.from_user.id)
        await callback.message.edit_text(
            f"Роли обновлены ✅\n\n{profile_text(user)}",
            reply_markup=edit_profile_button(),
            parse_mode="HTML"
        )
        await state.clear()
        await callback.answer()
        return

    await state.update_data(role1=role1)
    await callback.message.edit_text(
        f"Первая роль: {role1}\n\nТеперь выбери вторую роль:",
        reply_markup=roles_keyboard(exclude=role1, exclude_generalist=True)
    )
    await state.set_state(EditProfile.role2)
    await callback.answer()


@router.callback_query(EditProfile.role2, F.data.startswith("role:"))
async def edit_role2(callback: CallbackQuery, state: FSMContext):
    role2 = callback.data.split(":", 1)[1]
    data = await state.get_data()
    update_user_roles(callback.from_user.id, data["role1"], role2)
    user = get_user(callback.from_user.id)
    await callback.message.edit_text(
        f"Роли обновлены ✅\n\n{profile_text(user)}",
        reply_markup=edit_profile_button(),
        parse_mode="HTML"
    )
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "editfield:rank")
async def cb_edit_rank(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выбери новый максимальный ранг:", reply_markup=rank_keyboard())
    await state.set_state(EditProfile.rank)
    await callback.answer()


@router.callback_query(EditProfile.rank, F.data.startswith("rank:"))
async def edit_rank_save(callback: CallbackQuery, state: FSMContext):
    rank = callback.data.split(":", 1)[1]
    update_user_field(callback.from_user.id, "rank", rank)
    user = get_user(callback.from_user.id)
    await callback.message.edit_text(
        f"Максимальный ранг обновлён ✅\n\n{profile_text(user)}",
        reply_markup=edit_profile_button(),
        parse_mode="HTML"
    )
    await state.clear()
    await callback.answer()


@router.message(Command("setupcommands"))
async def cmd_setup_commands(message: Message):
    if not await is_admin(message.from_user.id, message.chat.id):
        await message.reply("Эта команда только для админов.")
        return

    if message.chat.type not in ("group", "supergroup"):
        await message.reply(
            "Запусти эту команду прямо в той беседе, где будет использоваться бот "
            "(не в личных сообщениях)."
        )
        return

    # Обычным участникам этой беседы — короткое меню
    await bot.set_my_commands(PLAYER_COMMANDS, scope=BotCommandScopeChat(chat_id=message.chat.id))

    # Всем реальным администраторам этой беседы (автоматически, включая
    # будущих — Telegram сам определяет, кто сейчас админ) — полное меню
    await bot.set_my_commands(
        ADMIN_COMMANDS,
        scope=BotCommandScopeChatAdministrators(chat_id=message.chat.id)
    )

    text = (
        "Меню команд настроено ✅\n\n"
        "Обычные участники видят базовый набор команд, а любой, кто является "
        "администратором этой беседы в Telegram — расширенный (с /newcustom, /list, /cancelcustom и т.д.), "
        "автоматически, без ручной настройки."
    )
    await message.reply(text)


# ------------------------------ Настройка круга людей, кто может управлять ботом ------------------------------

@router.message(Command("addbotadmin"))
async def cmd_add_bot_admin(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Эту команду нужно использовать прямо в беседе.")
        return

    if not await is_chat_creator(message.from_user.id, message.chat.id):
        await message.reply(
            "Настраивать список людей, которые могут управлять кастомками, "
            "может только создатель этой беседы (или супер-админ бота)."
        )
        return

    if not message.reply_to_message:
        await message.reply(
            "Ответь этой командой (Reply) на любое сообщение того человека, "
            "которого хочешь сделать админом бота в этой беседе."
        )
        return

    target = message.reply_to_message.from_user
    add_chat_admin(message.chat.id, target.id)
    await message.reply(
        f"Готово ✅ {target.full_name} теперь может управлять кастомками в этой беседе.\n\n"
        f"Обрати внимание: как только список настроен хотя бы для одного человека, "
        f"обычные админы Telegram-беседы больше НЕ получают доступ автоматически — "
        f"только те, кто явно добавлен через /addbotadmin."
    )


@router.message(Command("removebotadmin"))
async def cmd_remove_bot_admin(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Эту команду нужно использовать прямо в беседе.")
        return

    if not await is_chat_creator(message.from_user.id, message.chat.id):
        await message.reply(
            "Настраивать список людей, которые могут управлять кастомками, "
            "может только создатель этой беседы (или супер-админ бота)."
        )
        return

    if not message.reply_to_message:
        await message.reply(
            "Ответь этой командой (Reply) на сообщение того, кого нужно убрать из списка."
        )
        return

    target = message.reply_to_message.from_user
    remove_chat_admin(message.chat.id, target.id)
    await message.reply(f"{target.full_name} больше не может управлять кастомками в этой беседе ❌")


@router.message(Command("listbotadmins"))
async def cmd_list_bot_admins(message: Message):
    admin_ids = get_chat_admins(message.chat.id)

    if not admin_ids:
        await message.reply(
            "Отдельный список не настроен.\n\n"
            "Сейчас управлять кастомками может любой реальный администратор этой "
            "беседы в Telegram. Чтобы сузить круг лиц — создатель беседы может "
            "добавить конкретных людей командой /addbotadmin (ответом на их сообщение)."
        )
        return

    lines = ["👑 Кастомками в этой беседе могут управлять:"]
    for uid in admin_ids:
        try:
            member = await bot.get_chat_member(message.chat.id, uid)
            name = member.user.full_name
        except Exception:
            name = f"ID {uid}"
        lines.append(f"• {name}")
    await message.reply("\n".join(lines))


@router.message(Command("resetbotadmins"))
async def cmd_reset_bot_admins(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Эту команду нужно использовать прямо в беседе.")
        return

    if not await is_chat_creator(message.from_user.id, message.chat.id):
        await message.reply(
            "Сбросить список может только создатель этой беседы (или супер-админ бота)."
        )
        return

    clear_chat_admins(message.chat.id)
    await message.reply(
        "Список сброшен ✅\n\n"
        "Теперь снова любой реальный администратор этой беседы в Telegram "
        "может управлять кастомками."
    )


# ------------------------------ Создание кастомки (только админы) ------------------------------

def parse_custom_time(text: str):
    """Разбирает время из текста ('21:30' или '15.07.2026 21:30'), считая его московским.
    None, если формат неверный."""
    text = text.strip()
    now = now_msk()
    try:
        if len(text.split()) == 1:
            # только время -> сегодня (или завтра, если время уже прошло)
            t = datetime.strptime(text, "%H:%M").time()
            event_time = datetime.combine(now.date(), t, tzinfo=MOSCOW_TZ)
            if event_time < now:
                event_time += timedelta(days=1)
        else:
            event_time = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW_TZ)
        return event_time
    except ValueError:
        return None


@router.message(Command("newcustom"))
async def cmd_newcustom(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id, message.chat.id):
        await message.reply("Эта команда только для админов.")
        return

    if get_active_custom(message.chat.id):
        await message.reply(
            "В этой беседе уже есть активная кастомка. "
            "Сначала заверши её командой /cancelcustom, если нужно создать новую."
        )
        return

    await message.reply(
        "Во сколько начинается кастомка?\n\n"
        "Напиши время в формате ЧЧ:ММ (например 21:30) — это будет сегодня, "
        "или ДД.ММ.ГГГГ ЧЧ:ММ, если это другой день."
    )
    await state.set_state(NewCustom.waiting_time)


@router.message(NewCustom.waiting_time)
async def newcustom_time(message: Message, state: FSMContext):
    event_time = parse_custom_time(message.text)
    if event_time is None:
        await message.reply(
            "Не понял формат времени 🙈\n"
            "Напиши так: 21:30  или так: 15.07.2026 21:30"
        )
        return

    custom_id = create_custom(message.chat.id, event_time)
    await state.clear()

    text_msg = (
        f"🎮 <b>Открыта регистрация на кастомку!</b>\n\n"
        f"🕒 Начало: {event_time.strftime('%d.%m.%Y %H:%M')} по МСК\n\n"
        f"Нажми на кнопку ниже, чтобы записаться.\n"
        f"Если у тебя ещё нет профиля — сначала напиши боту в личные сообщения /start"
    )
    await message.reply(text_msg, reply_markup=register_button(custom_id), parse_mode="HTML")


@router.callback_query(F.data.startswith("edittime:"))
async def cb_edit_time_prompt(callback: CallbackQuery, state: FSMContext):
    if not await is_admin(callback.from_user.id, callback.message.chat.id):
        await callback.answer("Менять время может только админ.", show_alert=True)
        return

    custom_id = int(callback.data.split(":", 1)[1])
    custom = get_custom_by_id(custom_id)
    if not custom or custom["status"] != "active":
        await callback.answer("Эта кастомка уже неактивна.", show_alert=True)
        return

    await state.update_data(edit_custom_id=custom_id)
    await state.set_state(EditCustomTime.waiting_time)
    await callback.message.reply(
        "Во сколько теперь начинается кастомка?\n\n"
        "Напиши время в формате ЧЧ:ММ (например 21:30) — это будет сегодня, "
        "или ДД.ММ.ГГГГ ЧЧ:ММ, если это другой день."
    )
    await callback.answer()


@router.message(EditCustomTime.waiting_time)
async def edit_custom_time_save(message: Message, state: FSMContext):
    event_time = parse_custom_time(message.text)
    if event_time is None:
        await message.reply(
            "Не понял формат времени 🙈\n"
            "Напиши так: 21:30  или так: 15.07.2026 21:30"
        )
        return

    data = await state.get_data()
    custom_id = data["edit_custom_id"]
    await state.clear()

    update_custom_time(custom_id, event_time)

    text_msg = (
        f"🕒 Время кастомки изменено!\n\n"
        f"Новое начало: {event_time.strftime('%d.%m.%Y %H:%M')} по МСК\n\n"
        f"Уже зарегистрированные остаются в списке — регистрация всё ещё открыта."
    )
    await message.reply(text_msg, reply_markup=register_button(custom_id))


@router.message(Command("active"))
async def cmd_active(message: Message):
    custom = get_active_custom(message.chat.id)
    if not custom:
        await message.reply("Сейчас в этой беседе нет активной кастомки.")
        return

    event_time = parse_stored_time(custom["event_time"])
    players = get_registrations(custom["id"])
    await message.reply(
        f"🎮 Активная кастомка\n\n"
        f"🕒 Начало: {event_time.strftime('%d.%m.%Y %H:%M')} по МСК\n"
        f"👥 Зарегистрировано: {len(players)}",
        reply_markup=register_button(custom["id"])
    )


@router.message(Command("cancelcustom"))
async def cmd_cancel_custom(message: Message):
    if not await is_admin(message.from_user.id, message.chat.id):
        await message.reply("Эта команда только для админов.")
        return

    custom = get_active_custom(message.chat.id)
    if not custom:
        await message.reply("Активной кастомки в этой беседе сейчас нет.")
        return

    cancel_custom_db(custom["id"])
    await message.reply("Кастомка отменена ❌")


@router.message(Command("list"))
async def cmd_list(message: Message):
    if not await is_admin(message.from_user.id, message.chat.id):
        await message.reply("Эта команда только для админов.")
        return

    custom = get_active_custom(message.chat.id)
    if not custom:
        await message.reply("Активной кастомки в этой беседе сейчас нет.")
        return

    players = get_registrations(custom["id"])
    if not players:
        await message.reply("Пока никто не зарегистрировался.")
        return

    lines = [f"📋 Зарегистрировано: {len(players)}\n(✅ подтвердил участие · ❌ не подтвердил · ⏳ ещё не спрашивали)\n"]
    for i, p in enumerate(players, start=1):
        lines.append(
            f"{i}. {attendance_icon(p['attendance'])} {p['nickname']} (ID: {p['game_id']}) — "
            f"{p['role1']}, {p['role2']} — {p['rank']}"
        )
    await message.reply("\n".join(lines))


@router.message(Command("removeplayer"))
async def cmd_remove_player(message: Message):
    if not await is_admin(message.from_user.id, message.chat.id):
        await message.reply("Эта команда только для админов.")
        return

    custom = get_active_custom(message.chat.id)
    if not custom:
        await message.reply("Активной кастомки в этой беседе сейчас нет.")
        return

    players = get_registrations(custom["id"])
    if not players:
        await message.reply("Пока никто не зарегистрировался.")
        return

    await message.reply(
        "Кого удалить из списка зарегистрированных?",
        reply_markup=remove_player_keyboard(custom["id"])
    )


@router.callback_query(F.data.startswith("removeplayer:"))
async def cb_remove_player(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id, callback.message.chat.id):
        await callback.answer("Удалять участников могут только админы.", show_alert=True)
        return

    if callback.data == "removeplayer:cancel":
        await callback.message.edit_text("Отменено.")
        await callback.answer()
        return

    _, custom_id, user_id = callback.data.split(":")
    custom_id, user_id = int(custom_id), int(user_id)

    custom = get_custom_by_id(custom_id)
    if not custom or custom["status"] != "active":
        await callback.answer("Эта кастомка уже неактивна.", show_alert=True)
        return

    user = get_user(user_id)
    name = user["nickname"] if user else f"ID {user_id}"
    unregister_from_custom(custom_id, user_id)

    await callback.message.edit_text(f"{name} удалён(а) из списка зарегистрированных ❌")
    await callback.answer("Готово")


# ------------------------------ Формирование команд (только админы) ------------------------------

@router.message(Command("maketeams"))
async def cmd_maketeams(message: Message):
    if not await is_admin(message.from_user.id, message.chat.id):
        await message.reply("Эта команда только для админов.")
        return

    custom = get_active_custom(message.chat.id)
    if not custom:
        await message.reply("Активной кастомки в этой беседе сейчас нет.")
        return

    players = get_registrations(custom["id"])
    if len(players) < 5:
        await message.reply(
            f"Пока зарегистрировано только {len(players)} игрок(ов) — "
            f"для хотя бы одной команды нужно минимум 5."
        )
        return

    # Можно явно указать число команд: /maketeams 3
    parts = message.text.split()
    num_teams = None
    if len(parts) > 1 and parts[1].isdigit():
        num_teams = int(parts[1])
        if num_teams * 5 > len(players):
            await message.reply(
                f"Для {num_teams} команд нужно минимум {num_teams * 5} игроков, "
                f"а зарегистрировано только {len(players)}."
            )
            return

    actual_teams = make_teams(custom["id"], num_teams)
    if actual_teams == 0:
        await message.reply("Не получилось сформировать ни одной полной команды из 5 человек.")
        return

    await message.reply(
        f"Команды сформированы ({actual_teams}) 🎲\n\n" + render_teams_text(custom["id"]),
        parse_mode="HTML",
        reply_markup=teams_actions_keyboard()
    )


@router.message(Command("teams"))
async def cmd_teams(message: Message):
    custom = get_active_custom(message.chat.id)
    if not custom:
        await message.reply("Активной кастомки в этой беседе сейчас нет.")
        return

    await message.reply(
        render_teams_text(custom["id"]),
        parse_mode="HTML",
        reply_markup=teams_actions_keyboard()
    )


@router.callback_query(F.data == "teams:pick1")
async def cb_teams_pick1(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id, callback.message.chat.id):
        await callback.answer("Менять игроков местами могут только админы.", show_alert=True)
        return

    custom = get_active_custom(callback.message.chat.id)
    if not custom:
        await callback.answer("Активной кастомки сейчас нет.", show_alert=True)
        return

    await callback.message.edit_text(
        "Кого поменять местами?\nВыбери первого игрока:",
        reply_markup=teams_player_picker_keyboard(custom["id"])
    )
    await callback.answer()


@router.callback_query(F.data.startswith("teams:pick1sel:"))
async def cb_teams_pick1sel(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id, callback.message.chat.id):
        await callback.answer("Менять игроков местами могут только админы.", show_alert=True)
        return

    custom = get_active_custom(callback.message.chat.id)
    if not custom:
        await callback.answer("Активной кастомки сейчас нет.", show_alert=True)
        return

    uid1 = int(callback.data.split(":")[2])
    user1 = get_user(uid1)

    await callback.message.edit_text(
        f"Первый игрок: {user1['nickname']}\n\nТеперь выбери, с кем поменять его местами:",
        reply_markup=teams_player_picker_keyboard(custom["id"], exclude_uid=uid1, first_uid=uid1)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("teams:pick2sel:"))
async def cb_teams_pick2sel(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id, callback.message.chat.id):
        await callback.answer("Менять игроков местами могут только админы.", show_alert=True)
        return

    custom = get_active_custom(callback.message.chat.id)
    if not custom:
        await callback.answer("Активной кастомки сейчас нет.", show_alert=True)
        return

    _, _, uid1, uid2 = callback.data.split(":")
    swap_players_teams(custom["id"], int(uid1), int(uid2))

    await callback.message.edit_text(
        "Игроки поменяны местами ✅\n\n" + render_teams_text(custom["id"]),
        parse_mode="HTML",
        reply_markup=teams_actions_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "teams:cancel")
async def cb_teams_cancel(callback: CallbackQuery):
    custom = get_active_custom(callback.message.chat.id)
    text = render_teams_text(custom["id"]) if custom else "Активной кастомки сейчас нет."
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=teams_actions_keyboard())
    await callback.answer()


# ------------------------------ Регистрация на кастомку (кнопка) ------------------------------

@router.callback_query(F.data.startswith("reg:"))
async def cb_register(callback: CallbackQuery):
    custom_id = int(callback.data.split(":", 1)[1])
    user = get_user(callback.from_user.id)

    if not user:
        await callback.answer(
            "Сначала создай профиль — напиши боту в личные сообщения /start",
            show_alert=True
        )
        return

    added = register_to_custom(custom_id, callback.from_user.id)
    if added:
        await callback.answer("Ты зарегистрирован(а)✅", show_alert=True)
    else:
        await callback.answer("Ты уже зарегистрирован(а)", show_alert=True)


@router.callback_query(F.data.startswith("unreg:"))
async def cb_unregister(callback: CallbackQuery):
    custom_id = int(callback.data.split(":", 1)[1])
    custom = get_custom_by_id(custom_id)

    if not custom or custom["status"] != "active":
        await callback.answer("Эта кастомка уже неактивна.", show_alert=True)
        return

    if not is_registered(custom_id, callback.from_user.id):
        await callback.answer("Ты и не был(а) зарегистрирован(а) на эту кастомку.", show_alert=True)
        return

    event_time = parse_stored_time(custom["event_time"])
    if event_time - now_msk() < timedelta(minutes=30):
        await callback.answer(
            "Отменить регистрацию можно не позднее чем за 30 минут до начала кастомки.",
            show_alert=True
        )
        return

    unregister_from_custom(custom_id, callback.from_user.id)
    await callback.answer("Регистрация отменена ❌", show_alert=True)


# ------------------------------ Подтверждение готовности ("Готов") ------------------------------

@router.callback_query(F.data.startswith("ready:"))
async def cb_ready(callback: CallbackQuery):
    custom_id = int(callback.data.split(":", 1)[1])

    conn = db()
    row = conn.execute(
        "SELECT * FROM registrations WHERE custom_id = ? AND user_id = ?",
        (custom_id, callback.from_user.id)
    ).fetchone()
    conn.close()

    if not row:
        await callback.answer("Ты не был(а) зарегистрирован(а) на эту кастомку.", show_alert=True)
        return

    if row["attendance"] == "ready":
        await callback.answer("Ты уже подтвердил(а) участие ✅", show_alert=True)
        return

    set_attendance(custom_id, callback.from_user.id, "ready")
    await callback.answer("Участие подтверждено! Увидимся на кастомке ✅", show_alert=True)


# ------------------------------ Результаты кастомки: победитель и MVP (только админы) ------------------------------

@router.message(Command("setresult"))
async def cmd_setresult(message: Message):
    if not await is_admin(message.from_user.id, message.chat.id):
        await message.reply("Эта команда только для админов.")
        return

    custom = get_last_finished_custom(message.chat.id)
    if not custom:
        await message.reply("В этой беседе пока нет завершённых кастомок.")
        return

    teams, _ = get_teams_grouped(custom["id"])
    if not teams:
        await message.reply(
            "Для последней кастомки не были сформированы команды (/maketeams), "
            "поэтому указать команду-победителя нельзя."
        )
        return

    await message.reply(
        "🏆 Какая команда победила?",
        reply_markup=winner_pick_keyboard(custom["id"])
    )


@router.callback_query(F.data.startswith("result:team:"))
async def cb_result_team(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id, callback.message.chat.id):
        await callback.answer("Указывать результат могут только админы.", show_alert=True)
        return

    _, _, custom_id, team_number = callback.data.split(":")
    custom_id, team_number = int(custom_id), int(team_number)

    set_custom_winner(custom_id, team_number)

    await callback.message.edit_text(
        f"Победила Команда {team_number} 🏆\n\nКто был MVP этой команды?",
        reply_markup=mvp_pick_keyboard(custom_id, team_number)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("result:mvp:"))
async def cb_result_mvp(callback: CallbackQuery):
    if not await is_admin(callback.from_user.id, callback.message.chat.id):
        await callback.answer("Указывать результат могут только админы.", show_alert=True)
        return

    _, _, custom_id, user_id = callback.data.split(":")
    custom_id, user_id = int(custom_id), int(user_id)

    set_custom_mvp(custom_id, user_id)

    custom = get_custom_by_id(custom_id)
    mvp_user = get_user(user_id)
    teams, _ = get_teams_grouped(custom_id)
    winner_team = custom["winner_team"]
    team_names = ", ".join(p["nickname"] for p in teams.get(winner_team, []))

    result_text = (
        f"🏆 <b>Результаты кастомки</b>\n\n"
        f"Победила <b>Команда {winner_team}</b>: {team_names}\n\n"
        f"⭐ MVP: {mention(mvp_user)}"
    )
    await callback.message.edit_text(result_text, parse_mode="HTML")
    await callback.answer("Результат сохранён ✅")


@router.message(Command("history"))
async def cmd_history(message: Message):
    results = get_results_history(message.chat.id, limit=5)
    if not results:
        await message.reply("Пока нет сохранённых результатов кастомок.")
        return

    lines = ["📜 <b>Последние результаты</b>\n"]
    for r in results:
        event_time = parse_stored_time(r["event_time"])
        mvp_user = get_user(r["mvp_user_id"]) if r["mvp_user_id"] else None
        mvp_name = mvp_user["nickname"] if mvp_user else "—"
        lines.append(
            f"🕒 {event_time.strftime('%d.%m.%Y %H:%M')} — Команда {r['winner_team']} 🏆, MVP: {mvp_name}"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("mystats"))
async def cmd_mystats(message: Message):
    user = get_user(message.from_user.id)
    if not user:
        await message.reply("У тебя ещё нет профиля. Напиши /start, чтобы его создать.")
        return

    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Напиши эту команду прямо в беседе, где проходят кастомки.")
        return

    stats = get_player_stats(message.chat.id, message.from_user.id)
    games = stats["games"]

    if games == 0:
        await message.reply(
            f"📊 Пока нет завершённых кастомок с твоим участием в этой беседе.\n"
            f"Сыграй хотя бы одну — и тут появится статистика!"
        )
        return

    winrate = round(stats["wins"] / games * 100)
    await message.reply(
        f"📊 <b>Твоя статистика в этой беседе</b>\n\n"
        f"🎮 Игр сыграно: {games}\n"
        f"🏆 Побед: {stats['wins']} ({winrate}%)\n"
        f"⭐ MVP: {stats['mvps']}",
        parse_mode="HTML"
    )


@router.message(Command("leaderboard"))
async def cmd_leaderboard(message: Message):
    rows = get_leaderboard(message.chat.id, limit=10)
    if not rows:
        await message.reply("Пока нет завершённых кастомок с результатами в этой беседе.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Рейтинг беседы</b>\n"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        winrate = round(r["wins"] / r["games"] * 100) if r["games"] else 0
        lines.append(
            f"{prefix} {r['nickname']} — {r['wins']} побед из {r['games']} ({winrate}%), MVP: {r['mvps']}"
        )
    await message.reply("\n".join(lines), parse_mode="HTML")


@router.message(Command("resetstats"))
async def cmd_reset_stats(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Эту команду нужно использовать прямо в беседе.")
        return

    if not await is_chat_creator(message.from_user.id, message.chat.id):
        await message.reply(
            "Очищать статистику и историю может только создатель этой беседы "
            "(или супер-админ бота) — действие необратимо."
        )
        return

    count = count_finished_customs(message.chat.id)
    if count == 0:
        await message.reply("В этой беседе пока нет сохранённой статистики или истории — нечего очищать.")
        return

    await message.reply(
        f"⚠️ Это удалит статистику, рейтинг и историю по {count} завершённ"
        f"{'ой кастомке' if count == 1 else 'ым кастомкам'} в этой беседе. "
        f"Отменить это будет нельзя.\n\nТочно очистить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Да, очистить", callback_data="resetstats:confirm"),
            InlineKeyboardButton(text="Отмена", callback_data="resetstats:cancel"),
        ]])
    )


@router.callback_query(F.data.startswith("resetstats:"))
async def cb_reset_stats(callback: CallbackQuery):
    if not await is_chat_creator(callback.from_user.id, callback.message.chat.id):
        await callback.answer("Только создатель беседы может это подтвердить.", show_alert=True)
        return

    if callback.data == "resetstats:cancel":
        await callback.message.edit_text("Отменено, ничего не удалено.")
        await callback.answer()
        return

    reset_chat_stats(callback.message.chat.id)
    await callback.message.edit_text(
        "Готово ✅ Статистика, рейтинг и история кастомок в этой беседе очищены.\n\n"
        "Активная кастомка (если была) не затронута."
    )
    await callback.answer("Очищено")


# ------------------------------ Фоновая проверка времени (напоминания) ------------------------------

async def scheduler_loop():
    while True:
        now = now_msk()
        for custom in get_all_active_customs():
            try:
                event_time = parse_stored_time(custom["event_time"])
                first_reminder_time = event_time - timedelta(minutes=REMINDER_BEFORE_MINUTES)
                second_reminder_time = event_time - timedelta(minutes=SECOND_REMINDER_BEFORE_MINUTES)

                # первое напоминание — тегаем всех, даём кнопку "Готов"
                if not custom["reminder_sent"] and now >= first_reminder_time and now < event_time:
                    players = get_registrations(custom["id"])
                    mentions = " ".join(mention(p) for p in players) if players else "(пока никто не записался)"
                    await bot.send_message(
                        custom["chat_id"],
                        f"⏰ До кастомки осталось {REMINDER_BEFORE_MINUTES} минут!\n"
                        f"Нажмите «Готов», чтобы подтвердить участие:\n\n{mentions}",
                        parse_mode="HTML",
                        reply_markup=ready_button(custom["id"])
                    )
                    mark_reminder_sent(custom["id"])

                # второе напоминание — тегаем только тех, кто ещё не подтвердил
                if not custom["second_reminder_sent"] and now >= second_reminder_time and now < event_time:
                    pending = [p for p in get_registrations(custom["id"]) if p["attendance"] != "ready"]
                    if pending:
                        mentions = " ".join(mention(p) for p in pending)
                        await bot.send_message(
                            custom["chat_id"],
                            f"⏰ Осталось {SECOND_REMINDER_BEFORE_MINUTES} минут! "
                            f"Вы ещё не подтвердили участие:\n\n{mentions}",
                            parse_mode="HTML",
                            reply_markup=ready_button(custom["id"])
                        )
                    mark_second_reminder_sent(custom["id"])

                # старт кастомки
                if now >= event_time:
                    mark_unconfirmed_as_no_show(custom["id"])
                    players = get_registrations(custom["id"])
                    ready_players = [p for p in players if p["attendance"] == "ready"]
                    no_show_players = [p for p in players if p["attendance"] == "no_show"]

                    mentions = " ".join(mention(p) for p in ready_players) if ready_players else "(никто не подтвердил участие)"
                    text = f"🚨 Кастомка начинается прямо сейчас!\n\n{mentions}"
                    if no_show_players:
                        names = ", ".join(p["nickname"] for p in no_show_players)
                        text += f"\n\n❌ Не подтвердили участие: {names}"

                    await bot.send_message(custom["chat_id"], text, parse_mode="HTML")
                    finish_custom(custom["id"])
            except Exception as e:
                # Не даём одной ошибке (например, недоступной беседе или сбое сети)
                # остановить проверку остальных кастомок и все будущие напоминания
                print(f"[scheduler_loop] Ошибка при обработке кастомки {custom['id']}: {e}")

        await asyncio.sleep(30)  # проверка каждые 30 секунд


# ------------------------------ Запуск ------------------------------

async def main():
    init_db()
    # Базовое меню (личка с ботом и беседы, где ещё не запускали /setupcommands)
    await bot.set_my_commands(PLAYER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(PLAYER_COMMANDS, scope=BotCommandScopeDefault())
    asyncio.create_task(scheduler_loop())
    print("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
