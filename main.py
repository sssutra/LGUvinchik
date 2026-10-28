"""ЛГУвинчик — Telegram-бот знакомств студентов (pyTelegramBotAPI).

Установка в терминале PyCharm:
    python -m pip install -U pyTelegramBotAPI

Запуск: Run -> main.py. Таблицы SQLite создаются автоматически.
Существующая university_match.sqlite3 сохраняется и расширяется при необходимости.
"""

import logging
import time
import os
from requests.exceptions import RequestException
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import telebot
from telebot import types

# ────────────────────── НАСТРОЙКИ ──────────────────────
BOT_TOKEN = "9213274251:AAGFdvkc8T0ImzUQ38aFUm0tVAg_2GYo-0E"
ADMIN_IDS = {123456789}   # ID администраторов статистики
OWNER_ID = 8465547805  # ОБЯЗАТЕЛЬНО укажи СВОЙ числовой Telegram ID; только он получает жалобы
DB_PATH = Path(__file__).with_name("university_match.sqlite3")
USE_CUSTOM_EMOJI = True  # Если Telegram выдаёт ошибку прав на emoji, поставь False.
# Чтобы get_chat_member мог проверять подписки, добавь бота в @komfortnah
# как АДМИНИСТРАТОРА канала. Иначе доступ будет закрыт для всех.
REQUIRED_CHANNEL = "@komfortnah"
REQUIRED_CHANNEL_URL = "https://t.me/komfortnah"

# Новая редакция правил: пользователь повторно принимает их, анкета сохраняется.
RULES_VERSION = 5
MIN_AGE = 16
MAX_AGE = 120  # Проверка корректности введённого возраста, без отдельного подтверждения
# После первого пропуска анкету можно увидеть ещё один раз за календарный день.
# Следующее повторное появление — не раньше следующих суток по Москве (UTC+3).
LOCAL_DAY_TZ = timezone(timedelta(hours=3))
RESET_PROFILE_ICON = "5258420634785947640"
MAX_LIKE_MESSAGE = 300
MIN_REPORT_LENGTH = 10
MAX_REPORT_LENGTH = 1500
REPORT_COOLDOWN_HOURS = 24
BAN_PERIODS = {"1h": ("1 час", timedelta(hours=1)),
               "1d": ("1 день", timedelta(days=1)),
               "3d": ("3 дня", timedelta(days=3)),
               "7d": ("7 дней", timedelta(days=7)),
               "30d": ("30 дней", timedelta(days=30)),
               "forever": ("бессрочно", None)}
FACULTIES_PAGE_SIZE = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("LGUvinchik")

GENDERS = {"male": "Парень", "female": "Девушка", "unspecified": "Не указывать"}
# Старое значение оставлено только для совместимости с уже сохранёнными анкетами.
SELECTABLE_GENDERS = {"male", "female"}
LOOKING = {"male": "Парней", "female": "Девушек", "all": "Всех"}
FACULTIES = (
    "Факультет специального (дефектологического) образования",
    "Факультет истории и социальных наук",
    "Факультет математики и информатики",
    "Экономический факультет",
    "Факультет естествознания, географии и туризма",
    "Факультет иностранных языков",
    "Факультет психологии",
    "Факультет физической культуры и спорта",
    "Факультет философии, культурологии и искусства",
    "Филологический факультет",
    "Юридический факультет",
)

EDITABLE = {"display_name", "age", "gender", "preferred_gender", "faculty",
            "course", "bio", "photo_file_id"}


# ────────────────────── БАЗА ДАННЫХ ──────────────────────
def normalize_faculty(value):
    """Нормализация названия для сравнения кириллицы и лишних пробелов."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(value.split()).casefold()


@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=20000")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db_connection() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                accepted_rules INTEGER NOT NULL DEFAULT 0,
                display_name TEXT,
                age INTEGER,
                gender TEXT,
                preferred_gender TEXT NOT NULL DEFAULT 'all',
                faculty TEXT,
                course INTEGER,
                bio TEXT,
                share_username INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL
            )
        """)
        columns = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        additions = {
            "rules_version": "INTEGER NOT NULL DEFAULT 0",
            "preferred_gender_set": "INTEGER NOT NULL DEFAULT 0",
            "photo_file_id": "TEXT",
            "is_visible": "INTEGER NOT NULL DEFAULT 1",
            "edit_field": "TEXT",
            "inline_migrated": "INTEGER NOT NULL DEFAULT 0",
            "faculty_key": "TEXT",
            "reset_restore_visible": "INTEGER",
            "reset_previous_age": "INTEGER",
        }
        for column, definition in additions.items():
            if column not in columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS swipes (
                viewer_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                decision TEXT NOT NULL CHECK(decision IN ('like', 'skip')),
                created_at TEXT NOT NULL,
                PRIMARY KEY(viewer_id, target_id),
                CHECK(viewer_id != target_id)
            );
            CREATE INDEX IF NOT EXISTS ix_swipes_target ON swipes(target_id, decision);

            CREATE TABLE IF NOT EXISTS matches (
                user_low_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                user_high_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_low_id, user_high_id),
                CHECK(user_low_id < user_high_id)
            );
            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                blocked_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY(blocker_id, blocked_id)
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                reported_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(reporter_id, reported_id)
            );
            CREATE TABLE IF NOT EXISTS browse_sessions (
                viewer_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hidden_faculties (
                owner_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                faculty_key TEXT NOT NULL,
                faculty_label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner_id, faculty_key)
            );
            CREATE INDEX IF NOT EXISTS ix_hidden_faculties_key
                ON hidden_faculties(faculty_key, owner_id);

            CREATE TABLE IF NOT EXISTS daily_skip_repeats (
                viewer_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                local_day TEXT NOT NULL,
                shown_at TEXT NOT NULL,
                PRIMARY KEY(viewer_id, target_id, local_day)
            );
            CREATE INDEX IF NOT EXISTS ix_daily_skip_repeats_day
                ON daily_skip_repeats(viewer_id, local_day);

            CREATE TABLE IF NOT EXISTS pending_likes (
                sender_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                mode TEXT NOT NULL CHECK(mode IN ('search','incoming')),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_users_lookup
                ON users(is_visible, gender, preferred_gender, age);
        """)
        # Расширяем существующую таблицу жалоб без удаления прежних жалоб.
        report_columns = {r["name"] for r in db.execute("PRAGMA table_info(reports)")}
        for column, sql in {
            "status": "TEXT NOT NULL DEFAULT 'open'",
            "reviewed_at": "TEXT",
            "reviewer_id": "INTEGER",
            "resolution": "TEXT",
        }.items():
            if column not in report_columns:
                db.execute(f"ALTER TABLE reports ADD COLUMN {column} {sql}")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS pending_reports (
                reporter_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                reported_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                source TEXT NOT NULL CHECK(source IN ('search','incoming')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS moderation_bans (
                user_id INTEGER PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
                active INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                moderator_id INTEGER NOT NULL,
                report_id INTEGER,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_moderation_bans_active
                ON moderation_bans(active, expires_at);
            CREATE TABLE IF NOT EXISTS moderation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                report_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            );
        """)
        swipe_columns = {r["name"] for r in db.execute("PRAGMA table_info(swipes)")}
        if "like_message" not in swipe_columns:
            db.execute("ALTER TABLE swipes ADD COLUMN like_message TEXT")
        for row in db.execute("SELECT telegram_id, faculty, faculty_key FROM users WHERE faculty IS NOT NULL"):
            key = normalize_faculty(row["faculty"])
            if row["faculty_key"] != key:
                db.execute("UPDATE users SET faculty_key=? WHERE telegram_id=?",
                           (key, row["telegram_id"]))
    logger.info("База готова: %s", DB_PATH)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def local_day():
    """Календарный день в Москве (университетский часовой пояс, UTC+3)."""
    return datetime.now(LOCAL_DAY_TZ).date().isoformat()


def touch(user):
    now = utc_now()
    with db_connection() as db:
        db.execute("""
            INSERT INTO users(telegram_id, username, created_at, last_active_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              username=excluded.username, last_active_at=excluded.last_active_at
        """, (user.id, user.username, now, now))


def user_by_id(uid):
    with db_connection() as db:
        return db.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()


def set_fields(uid, **values):
    columns = EDITABLE | {"accepted_rules", "rules_version", "preferred_gender_set",
                          "share_username", "is_visible", "edit_field", "inline_migrated", "faculty_key",
                          "reset_restore_visible", "reset_previous_age"}
    if not values or not set(values).issubset(columns):
        raise ValueError("Попытка изменить недопустимые поля")
    if "faculty" in values:
        values["faculty_key"] = normalize_faculty(values["faculty"])
    statement = ", ".join(f"{field}=?" for field in values)
    with db_connection() as db:
        db.execute(f"UPDATE users SET {statement} WHERE telegram_id=?", (*values.values(), uid))


def missing_field(u):
    if u is None:
        return "rules"
    if not u["accepted_rules"] or u["rules_version"] != RULES_VERSION or not u["share_username"]:
        return "rules"
    for field in ("display_name", "age", "gender", "preferred_gender_set", "faculty",
                  "course", "bio", "photo_file_id"):
        if u[field] is None or (field == "preferred_gender_set" and not u[field]):
            return "preferred_gender" if field == "preferred_gender_set" else field
    return None


def ready(u):
    return u is not None and missing_field(u) is None


# ────────────────────── ЖАЛОБЫ И МОДЕРАЦИЯ ──────────────────────
def active_ban(db, uid):
    """Возвращает действующую блокировку. Истёкший срок учитывается как снятый.

    Для автоматического восстановления доступа достаточно следующего действия
    пользователя. Постоянно работающий планировщик не требуется.
    """
    now = utc_now()
    return db.execute("""
        SELECT * FROM moderation_bans WHERE user_id=? AND active=1
          AND (expires_at IS NULL OR expires_at>?)
    """, (uid, now)).fetchone()


def get_active_ban(uid):
    with db_connection() as db:
        return active_ban(db, uid)


def ban_notice(ban):
    if ban["expires_at"] is None:
        period = "Бессрочно"
    else:
        end = datetime.fromisoformat(ban["expires_at"]).astimezone(timezone.utc)
        period = f"До {end:%d.%m.%Y %H:%M} UTC"
    tail = ("Для снятия бессрочной блокировки обратись к владельцу бота."
            if ban["expires_at"] is None else
            "После завершения срока доступ восстановится автоматически. "
            "Если считаешь решение ошибочным, обратись к владельцу бота.")
    return ("⛔ <b>Твоя анкета заблокирована</b>\n\n"
            f"Срок: <b>{period}</b>\n"
            f"Причина: {escape(ban['reason'])}\n\n" + tail)


def check_ban(chat_id, uid, callback_id=None):
    ban = get_active_ban(uid)
    if ban is None:
        return False
    if callback_id:
        try:
            bot.answer_callback_query(callback_id, "Доступ ограничен", show_alert=True)
        except telebot.apihelper.ApiTelegramException:
            pass
    bot.send_message(chat_id, ban_notice(ban))
    return True


def start_report(uid, target, source):
    """Проверка, что жалоба отправляется только на реально показанную анкету."""
    if source not in ("search", "incoming") or uid == target:
        return False
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if source == "search":
            found = db.execute("SELECT 1 FROM browse_sessions WHERE viewer_id=? AND target_id=?",
                               (uid, target)).fetchone()
        else:
            found = incoming_available(db, uid, target)
        if not found:
            db.rollback()
            return False
        db.execute("""
            INSERT INTO pending_reports(reporter_id,reported_id,source,created_at)
            VALUES(?,?,?,?) ON CONFLICT(reporter_id) DO UPDATE SET
            reported_id=excluded.reported_id,source=excluded.source,created_at=excluded.created_at
        """, (uid,target,source,utc_now()))
        db.commit()
    clear_pending_like(uid)
    return True


def pending_report(uid):
    with db_connection() as db:
        return db.execute("SELECT * FROM pending_reports WHERE reporter_id=?", (uid,)).fetchone()


def clear_pending_report(uid):
    with db_connection() as db:
        db.execute("DELETE FROM pending_reports WHERE reporter_id=?", (uid,))


def submit_report(uid, content):
    """Пишет жалобу, блокирует пару только для отправителя и возвращает report_id.

    Повторные открытые жалобы на одну пару не создают поток уведомлений.
    """
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("SELECT * FROM pending_reports WHERE reporter_id=?", (uid,)).fetchone()
        if pending is None:
            db.rollback()
            return "missing", None
        target = pending["reported_id"]
        if pending["source"] == "search":
            found = db.execute("SELECT 1 FROM browse_sessions WHERE viewer_id=? AND target_id=?",
                               (uid,target)).fetchone()
        else:
            found = incoming_available(db,uid,target)
        if not found:
            db.execute("DELETE FROM pending_reports WHERE reporter_id=?", (uid,))
            db.commit()
            return "stale", None
        old = db.execute("SELECT id,status,created_at FROM reports WHERE reporter_id=? AND reported_id=?",
                         (uid,target)).fetchone()
        cutoff = (datetime.now(timezone.utc)-timedelta(hours=REPORT_COOLDOWN_HOURS)).isoformat()
        if old and (old["status"] == "open" or old["created_at"] > cutoff):
            db.execute("DELETE FROM pending_reports WHERE reporter_id=?", (uid,))
            db.commit()
            return "duplicate",old["id"]
        if old:
            db.execute("""UPDATE reports SET reason=?,created_at=?,status='open',
                        reviewed_at=NULL,reviewer_id=NULL,resolution=NULL WHERE id=?""",
                       (content,utc_now(),old["id"]))
            report_id = old["id"]
        else:
            cursor = db.execute("""
                INSERT INTO reports(reporter_id,reported_id,reason,created_at,status)
                VALUES(?,?,?,?,'open')
            """,(uid,target,content,utc_now()))
            report_id = cursor.lastrowid
        db.execute("INSERT OR IGNORE INTO blocks(blocker_id,blocked_id,created_at) VALUES(?,?,?)",
                   (uid,target,utc_now()))
        db.execute("DELETE FROM browse_sessions WHERE viewer_id=?", (uid,))
        db.execute("DELETE FROM pending_reports WHERE reporter_id=?", (uid,))
        db.commit()
    return "ok",report_id


def report_keyboard(report_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(icon_button("Посмотреть анкету", f"mod:view:{report_id}", ICONS["view"]))
    kb.row(icon_button("Заблокировать", f"mod:banmenu:{report_id}", ICONS["block"]),
           icon_button("Отклонить", f"mod:dismiss:{report_id}", ICONS["skip"]))
    return kb


def notify_owner_report(report_id):
    """Никому, кроме OWNER_ID, уведомление о жалобе не отправляется."""
    with db_connection() as db:
        r = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        count = db.execute("SELECT COUNT(*) FROM reports WHERE reported_id=?", (r["reported_id"],)).fetchone()[0] if r else 0
    if not r:
        return False
    text = (f"🚩 <b>Новая жалоба №{report_id}</b>\n\n"
            f"На пользователя: <code>{r['reported_id']}</code>\n"
            f"От пользователя: <code>{r['reporter_id']}</code>\n"
            f"Всего записей жалоб на анкету: {count}\n\n"
            f"<b>Текст жалобы:</b>\n{escape(r['reason'])}\n\n"
            "Жалоба не означает автоматическую блокировку.")
    try:
        bot.send_message(OWNER_ID,text,reply_markup=report_keyboard(report_id))
        return True
    except Exception as exc:
        # Сохранённая в SQLite жалоба остаётся доступна через /reports.
        logger.error("Не удалось уведомить владельца о жалобе %s: %s", report_id, exc)
        return False


def ban_user(target, owner_id, report_id, period_key):
    if owner_id != OWNER_ID or period_key not in BAN_PERIODS:
        return False
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        r = db.execute("SELECT * FROM reports WHERE id=? AND status='open'", (report_id,)).fetchone()
        if r is None or r["reported_id"] != target or target == OWNER_ID:
            db.rollback()
            return False
        label, delta = BAN_PERIODS[period_key]
        expires = (datetime.now(timezone.utc)+delta).isoformat() if delta else None
        now = utc_now()
        reason = "Нарушение правил сообщества"
        db.execute("""
            INSERT INTO moderation_bans(user_id,active,expires_at,reason,created_at,moderator_id,report_id,updated_at)
            VALUES(?,1,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
              active=1, expires_at=excluded.expires_at,reason=excluded.reason,
              moderator_id=excluded.moderator_id,report_id=excluded.report_id,
              updated_at=excluded.updated_at
        """, (target,expires,reason,now,owner_id,report_id,now))
        db.execute("""
            INSERT INTO moderation_log(user_id,actor_id,action,report_id,details,created_at)
            VALUES(?,?,'ban',?,?,?)
        """,(target,owner_id,report_id,period_key,now))
        db.execute("""
            UPDATE reports SET status='banned',reviewed_at=?,reviewer_id=?,resolution=? WHERE id=?
        """,(now,owner_id,label,report_id))
        db.execute("DELETE FROM browse_sessions WHERE viewer_id=? OR target_id=?", (target,target))
        db.execute("DELETE FROM pending_likes WHERE sender_id=? OR target_id=?", (target,target))
        db.commit()
    return True


def dismiss_report(report_id, owner_id):
    if owner_id != OWNER_ID:
        return False
    with db_connection() as db:
        result = db.execute("""UPDATE reports SET status='dismissed',reviewed_at=?,reviewer_id=?,resolution='Отклонено'
                           WHERE id=? AND status='open'""", (utc_now(),owner_id,report_id))
        return result.rowcount == 1


def unban_user(uid, owner_id):
    if owner_id != OWNER_ID:
        return False
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("UPDATE moderation_bans SET active=0,updated_at=? WHERE user_id=? AND active=1",
                             (utc_now(),uid)).rowcount
        if changed:
            db.execute("""INSERT INTO moderation_log(user_id,actor_id,action,details,created_at)
                          VALUES(?,?,'unban','Досрочное снятие',?)""", (uid,owner_id,utc_now()))
        db.commit()
    return bool(changed)


def moderation_ban_keyboard(report_id):
    kb = types.InlineKeyboardMarkup(row_width=2)
    choices = [("1 час","1h"),("1 день","1d"),("3 дня","3d"),
               ("7 дней","7d"),("30 дней","30d"),("Навсегда","forever")]
    for i in range(0,len(choices),2):
        kb.row(*(icon_button(label, f"mod:ban:{report_id}:{key}")
                 for label,key in choices[i:i+2]))
    kb.add(icon_button("Назад к жалобе", f"mod:back:{report_id}", ICONS["back"]))
    return kb


def notify_ban(target):
    ban = get_active_ban(target)
    if ban:
        try:
            bot.send_message(target, ban_notice(ban))
        except Exception as exc:
            logger.warning("Не удалось уведомить заблокированного %s: %s",target,exc)

# ────────────────────── КНОПКИ И ТЕКСТЫ ──────────────────────
# Telegram Bot API 9.4+: иконка custom emoji — отдельное поле кнопки,
# а не текстовая подстановка emoji. Нужен pyTelegramBotAPI >= 4.33.
ICONS = {
    "settings": "5258096772776991776",
    "search": "5429571366384842791",
    "profile": "5316727448644103237",
    "like": "5258179403652801593",
    "reply": "5258179403652801593",
    "view": "5258501105293205250",
    "skip": "5258084656674250503",
    "block": "5258318620722733379",
    "report": "5275969776668134187",
    "accept_rules": "5260726538302660868",
    "back": "5258236805890710909",
    "other_faculties": "5260687681733533075",
}

# Десять иконок по порядку: восемь полей, переключатель анкеты, меню.
SETTING_ICONS = [
    "5256143829672672750",  # фотография
    "5258215635996908355",  # описание
    "5258331647358540449",  # имя
    "5325604415900504150",  # возраст
    "5258513401784573443",  # пол
    "5429571366384842791",  # кого искать
    "5258334872878980409",  # факультет
    "5258260149037965799",  # курс
    "5258318620722733379",  # отключить/включить анкету
    "5316727448644103237",  # моя анкета
]


def icon_button(title, callback_data, icon_id=None, style=None):
    options = {"callback_data": callback_data}
    if icon_id and USE_CUSTOM_EMOJI:
        options["icon_custom_emoji_id"] = icon_id
    # Все кнопки «Моя анкета» — зелёные (Bot API style=success).
    if style or title == "Моя анкета":
        options["style"] = style or "success"
    return types.InlineKeyboardButton(title, **options)


def inline_buttons(prefix, items):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for title, value in items:
        kb.add(icon_button(title, f"{prefix}:{value}"))
    return kb


def main_menu():
    # Только Inline-кнопки, под сообщением бота.
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(icon_button("Искать знакомства", "menu:search", ICONS["search"]))
    kb.add(icon_button("Моя анкета", "menu:profile", ICONS["profile"]))
    kb.add(icon_button("Настройки анкеты", "menu:settings", ICONS["settings"]))
    return kb


def rules_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(icon_button("Принимаю правила", "rules:accept", ICONS["accept_rules"]))
    return kb


def subscription_keyboard():
    """Подписка и ручная повторная проверка — Inline-кнопки в чате."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📢 Подписаться на канал", url=REQUIRED_CHANNEL_URL))
    kb.add(icon_button("✅ Проверить подписку", "subscription:check"))
    return kb


def subscription_status(uid):
    """True — участник канала, False — не участник, None — ошибка Telegram API.

    Не кэшируем результат: выход из канала сразу закрывает функционал при
    следующем действии пользователя. Боту нужны права администратора канала.
    """
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, uid)
        if member.status in ("creator", "administrator", "member"):
            return True
        if member.status == "restricted":
            return bool(getattr(member, "is_member", False))
        return False
    except Exception as exc:
        # При ошибке сети или недостатке прав не выдаём подписку за подтверждённую.
        logger.warning("Не удалось проверить подписку %s в %s: %s", uid, REQUIRED_CHANNEL, exc)
        return None


def ensure_subscription(chat_id, uid, callback_id=None):
    """Единый контроль доступа для команд, сообщений и ВСЕХ старых Inline-кнопок."""
    result = subscription_status(uid)
    if result is True:
        return True
    if callback_id is not None:
        try:
            bot.answer_callback_query(callback_id)
        except telebot.apihelper.ApiTelegramException:
            pass
    if result is False:
        text = ("🔒 <b>Для использования ЛГУвинчика подпишись на наш канал.</b>\n\n"
                "После подписки нажми «Проверить подписку», и бот откроет анкету."
                "\n\nКанал: @komfortnah")
    else:
        text = ("⚠️ <b>Не удалось проверить подписку.</b>\n\n"
                "Попробуй снова через кнопку ниже. "
                "Если ошибка повторяется, администратору нужно проверить "
                "доступ бота к каналу @komfortnah.")
    bot.send_message(chat_id, text, reply_markup=subscription_keyboard())
    return False


def back_buttons(destination="menu"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(icon_button("Назад", f"nav:{destination}", ICONS["back"]))
    return kb


def settings_keyboard(u):
    items = [
        ("Изменить фотографию", "photo_file_id"),
        ("Изменить описание", "bio"),
        ("Изменить имя", "display_name"),
        ("Изменить возраст", "age"),
        ("Изменить пол", "gender"),
        ("Кого искать", "preferred_gender"),
        ("Изменить факультет", "faculty"),
        ("Изменить курс", "course"),
    ]
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        icon_button(title, f"edit:{value}", SETTING_ICONS[index])
        for index, (title, value) in enumerate(items)
    ]
    for index in range(0, len(buttons), 2):
        kb.row(*buttons[index:index + 2])
    kb.row(
        icon_button("Отключить анкету" if u["is_visible"] else "Включить анкету",
                    "settings:toggle", SETTING_ICONS[8]),
        icon_button("Скрыть от факультетов", "settings:faculties", ICONS["settings"]),
    )
    kb.row(icon_button("Заполнить анкету заново", "settings:reset_confirm",
                       RESET_PROFILE_ICON, style="danger"))
    kb.row(icon_button("Моя анкета", "menu:profile", ICONS["profile"]))
    return kb


def reset_confirmation_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(icon_button("Да, заполнить заново", "settings:reset_start",
                       RESET_PROFILE_ICON, style="danger"))
    kb.add(icon_button("Назад", "menu:settings", ICONS["back"]))
    return kb


def reset_profile(uid):
    """Перезапустить анкету без удаления аккаунта, лайков и совпадений.

    Во время новой регистрации профиль скрыт. Прежнюю видимость восстанавливаем
    только после заполнения всех обязательных полей. Смена возрастной группы
    удаляет несовместимые лайки и совпадения.
    """
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        u = db.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not ready(u) or u["reset_restore_visible"] is not None:
            db.rollback()
            return False
        db.execute("""
            UPDATE users SET
                reset_restore_visible=is_visible, reset_previous_age=age,
                is_visible=0, display_name=NULL, age=NULL, gender=NULL,
                preferred_gender='all', preferred_gender_set=0,
                faculty=NULL, faculty_key=NULL, course=NULL, bio=NULL,
                photo_file_id=NULL, edit_field=NULL
            WHERE telegram_id=?
        """, (uid,))
        db.execute("DELETE FROM browse_sessions WHERE viewer_id=? OR target_id=?", (uid, uid))
        db.execute("DELETE FROM pending_likes WHERE sender_id=? OR target_id=?", (uid, uid))
        db.execute("DELETE FROM pending_reports WHERE reporter_id=? OR reported_id=?", (uid, uid))
        db.commit()
    return True


def finish_profile_reset(uid):
    """Завершить повторное заполнение и восстановить предыдущее состояние видимости."""
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        u = db.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if u is None or not ready(u) or u["reset_restore_visible"] is None:
            db.rollback()
            return False
        db.execute("""
            UPDATE users SET is_visible=reset_restore_visible,
                reset_restore_visible=NULL, reset_previous_age=NULL
            WHERE telegram_id=?
        """, (uid,))
        db.commit()
    return True


def menu_navigation(*actions):
    """Кнопки переходов с иконками пользователя и общими callback_data."""
    labels = {
        "search": "Искать знакомства",
        "settings": "Настройки анкеты",
        "profile": "Моя анкета",
    }
    kb = types.InlineKeyboardMarkup(row_width=1)
    for action in actions:
        if action == "home":  # Совместимость со старыми вызовами.
            action = "profile"
        kb.add(icon_button(labels[action], f"menu:{action}", ICONS.get(action)))
    return kb


def gender_buttons(prefix="register_gender"):
    return inline_buttons(prefix, [("👨 Парень", "male"), ("👩 Девушка", "female")])


def looking_buttons(prefix="register_looking"):
    return inline_buttons(prefix, [("👨 Парней", "male"), ("👩 Девушек", "female"),
                                   ("👥 Всех", "all")])


def profile_text(u, own=False):
    name = escape(u["display_name"] or "Студент")
    text = (f"👤 <b>{name}, {u['age']}</b>\n"
            f"Пол: {GENDERS.get(u['gender'], 'Не указан')}\n"
            f"🎓 Факультет: {escape(u['faculty'] or '')}\n"
            f"📚 Курс: {u['course']}\n\n"
            f"💬 {escape(u['bio'] or 'О себе пока ничего не написано')}")
    if own:
        text += (f"\n\n🔎 Ищу: {LOOKING.get(u['preferred_gender'], 'Всех')}"
                 f"\nСтатус: {'✅ активна' if u['is_visible'] else '⛔ отключена'}")
        hidden = get_hidden_faculty_labels(u["telegram_id"])
        if hidden:
            text += f"\nСкрыта от факультетов: {len(hidden)}"
    return text


def username_link(u):
    if not u["share_username"]:
        return "Пользователь не разрешил раскрывать username."
    username = u["username"]
    if not username or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return "Username не установлен в Telegram."
    safe = escape(username)
    return f'<a href="https://t.me/{safe}">@{safe}</a>'


def send_profile(chat_id, u, own=False, reply_markup=None):
    caption = profile_text(u, own=own)
    if u["photo_file_id"]:
        try:
            return bot.send_photo(chat_id, u["photo_file_id"], caption=caption,
                                  reply_markup=reply_markup)
        except telebot.apihelper.ApiTelegramException:
            logger.warning("Фото пользователя %s больше недоступно", u["telegram_id"])
    return bot.send_message(chat_id, caption + "\n\n⚠️ Фото недоступно.",
                            reply_markup=reply_markup)


def remove_old_reply_keyboard(chat_id, uid):
    # Выполняется один раз: убирает клавиатуру старой версии бота под вводом.
    u = user_by_id(uid)
    if u is not None and not u["inline_migrated"]:
        # Старая Reply-клавиатура убирается одноразовым служебным сообщением.
        # Оно сразу удаляется, поэтому лишнего сообщения при /start не будет.
        try:
            temporary = bot.send_message(chat_id, "🎓 ЛГУвинчик",
                                         reply_markup=types.ReplyKeyboardRemove())
            set_fields(uid, inline_migrated=1)
            try:
                bot.delete_message(chat_id, temporary.message_id)
            except telebot.apihelper.ApiTelegramException:
                logger.debug("Не удалось удалить служебное сообщение с очисткой клавиатуры")
        except telebot.apihelper.ApiTelegramException as exc:
            logger.warning("Не удалось убрать старую клавиатуру: %s", exc)


def show_menu(chat_id):
    # Моя анкета теперь главный экран: отсюда доступны настройки и поиск.
    uid = chat_id
    finish_profile_reset(uid)
    if ready(user_by_id(uid)):
        show_own_profile(chat_id, uid)
    else:
        ask_registration(chat_id, uid)


def show_settings(chat_id, uid):
    u = user_by_id(uid)
    status = "✅ Анкета включена" if u["is_visible"] else "⛔ Анкета отключена"
    bot.send_message(chat_id,
        "⚙️ <b>Настройки анкеты</b>\n\n"
        "Здесь можно изменить данные, фотографию, выбор знакомств и скрыть анкету "
        "от выбранных факультетов.\n\n"
        f"{status}\n"
        "При отключении анкета исчезает из поиска, но не удаляется из базы. "
        "Её можно будет включить обратно.",
        reply_markup=settings_keyboard(u))


def show_own_profile(chat_id, uid):
    finish_profile_reset(uid)
    u = user_by_id(uid)
    kb = menu_navigation("search", "settings")
    send_profile(chat_id, u, own=True, reply_markup=kb)


def ask_registration(chat_id, uid):
    u = user_by_id(uid)
    step = missing_field(u)
    if step == "rules":
        bot.send_message(chat_id,
            "🎓 <b>Добро пожаловать в ЛГУвинчик!</b>\n\n"
            "Это тестовый бот знакомств студентов одного университета. "
            "Проверка студенческого статуса пока не проводится.\n\n"
            "<b>Правила:</b> уважай других, не отправляй спам, не используй чужие фото, "
            "не публикуй чужие персональные данные и указывай правдивые сведения в анкете. "
            "Для безопасности несовершеннолетние и совершеннолетние анкеты "
            "не смешиваются.\n\n"
            "Когда ты ставишь лайк, другой пользователь получает уведомление и может "
            "посмотреть твою анкету. Если симпатия взаимная, бот автоматически покажет "
            "каждому участнику Telegram-username другого, если он установлен. "
            "Принимая правила, ты соглашаешься с таким раскрытием своего username.\n\n"
            "В настройках можно временно отключить анкету или скрыть её "
            "от студентов выбранных факультетов.\n"
            "При отправке лайка с сообщением его текст станет виден получателю, "
            "когда он откроет твою анкету.\n\n"
            "Жалобы рассматривает только владелец бота: ему передаются текст жалобы "
            "и Telegram ID её отправителя и указанного пользователя. "
            "Владелец может ограничить доступ за нарушение правил.\n\n"
            "<b>Согласен с правилами?</b>",
            reply_markup=rules_keyboard())
    elif step == "gender":
        bot.send_message(chat_id, "Укажи свой пол:", reply_markup=gender_buttons())
    elif step == "preferred_gender":
        bot.send_message(chat_id, "🔎 Кого ты хочешь искать?", reply_markup=looking_buttons())
    elif step == "faculty":
        bot.send_message(chat_id, "🎓 Выбери свой факультет:",
                         reply_markup=faculty_select_keyboard("register_faculty"))
    elif step is None:
        finish_profile_reset(uid)
        bot.send_message(chat_id, "✅ Анкета готова!")
        show_menu(chat_id)
    else:
        prompts = {
            "display_name": "Как тебя зовут? Напиши имя (2–40 символов).",
            "age": f"Сколько тебе полных лет? Напиши число от {MIN_AGE} до {MAX_AGE}.",
            "course": "На каком ты курсе? Напиши число от 1 до 6.",
            "bio": "Расскажи о себе (до 500 символов).",
            "photo_file_id": "📷 Пришли фотографию для анкеты обычным фото Telegram (не файлом).",
        }
        if step == "bio":
            # Описание необязательно: пустая строка обозначает осознанный пропуск.
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(icon_button("Пропустить", "registration:skip_bio", ICONS["back"]))
            bot.send_message(chat_id, prompts[step] + "\n\nМожно пропустить этот шаг.",
                             reply_markup=kb)
        else:
            bot.send_message(chat_id, prompts[step])


# ────────────────────── ПОИСК И ЛАЙКИ ──────────────────────
def get_hidden_faculty_labels(uid):
    with db_connection() as db:
        return [r["faculty_label"] for r in db.execute(
            "SELECT faculty_label FROM hidden_faculties WHERE owner_id=? ORDER BY faculty_label",
            (uid,)).fetchall()]


def mutually_visible_faculties(db, me, other):
    """Никому не показываем профиль в обход выбранного ограничения факультета."""
    for owner, hidden_faculty in ((me, other), (other, me)):
        if not hidden_faculty["faculty_key"]:
            continue
        if db.execute("""
            SELECT 1 FROM hidden_faculties
            WHERE owner_id=? AND faculty_key=?
        """, (owner["telegram_id"], hidden_faculty["faculty_key"])).fetchone():
            return False
    return True


def compatible(me, partner, db=None):
    valid = bool(
        me is not None and partner is not None and ready(me) and ready(partner)
        and me["is_visible"] and partner["is_visible"] and me["telegram_id"] != partner["telegram_id"]
        and (me["age"] < 18) == (partner["age"] < 18)
        and (me["preferred_gender"] == "all" or me["preferred_gender"] == partner["gender"])
        and (partner["preferred_gender"] == "all" or partner["preferred_gender"] == me["gender"])
    )
    return valid and (db is None or (not active_ban(db,me["telegram_id"])
                                     and not active_ban(db,partner["telegram_id"])
                                     and mutually_visible_faculties(db, me, partner)))


def faculty_choices():
    """Только утверждённые 11 факультетов, в порядке университета."""
    return [(normalize_faculty(label), label) for label in FACULTIES]


def faculty_select_keyboard(prefix):
    """Названия без сокращений; один факультет на строку."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    for index, label in enumerate(FACULTIES):
        kb.add(icon_button(label, f"{prefix}:{index}"))
    if prefix == "edit_faculty":
        kb.add(icon_button("Назад", "nav:cancel_edit", ICONS["back"]))
    return kb


def faculty_hiding_keyboard(uid, page=0):
    choices = faculty_choices()
    total_pages = (len(choices) + FACULTIES_PAGE_SIZE - 1) // FACULTIES_PAGE_SIZE
    page = min(max(0, page), total_pages - 1)
    start = page * FACULTIES_PAGE_SIZE
    with db_connection() as db:
        hidden = {r[0] for r in db.execute(
            "SELECT faculty_key FROM hidden_faculties WHERE owner_id=?", (uid,))}
    kb = types.InlineKeyboardMarkup(row_width=1)
    for index in range(start, min(start + FACULTIES_PAGE_SIZE, len(choices))):
        key, title = choices[index]
        marker = "✅ " if key in hidden else "▫️ "
        # Индекс не меняется, пока фиксированный справочник факультетов неизменен.
        kb.add(icon_button(marker + title, f"faculty:toggle:{page}:{index}"))
    if total_pages > 1:
        # Одна кнопка "Другие" переключает страницы в обе стороны.
        next_page = page + 1 if page < total_pages - 1 else 0
        kb.row(icon_button("Другие", f"faculty:page:{next_page}",
                           ICONS["other_faculties"]))
    kb.row(icon_button("Сбросить скрытие", "faculty:reset"))
    kb.row(icon_button("Настройки анкеты", "menu:settings", ICONS["settings"]),
           icon_button("Моя анкета", "menu:profile", ICONS["profile"]))
    return kb


def show_faculty_hiding(chat_id, uid, page=0):
    hidden = get_hidden_faculty_labels(uid)
    hidden_text = ", ".join(hidden[:12])
    if len(hidden) > 12:
        hidden_text += f" и ещё {len(hidden) - 12}"
    text = ("🎓 <b>Скрыть анкету от факультетов</b>\n\n"
            "Выбери факультеты, студенты которых не должны видеть твою анкету. "
            "✅ — скрыто, ▫️ — показывается.\n\n"
            "Ограничение работает в обе стороны: анкеты этих факультетов "
            "также не появятся в твоём поиске.\n\n"
            + ("Сейчас скрыто: " + escape(hidden_text) if hidden else "Пока ничего не скрыто."))
    bot.send_message(chat_id, text, reply_markup=faculty_hiding_keyboard(uid, page))


def blocked_pair(db, first_id, second_id):
    return db.execute("""
        SELECT 1 FROM blocks WHERE
        (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)
    """, (first_id, second_id, second_id, first_id)).fetchone() is not None


def candidate_for(uid):
    me = user_by_id(uid)
    if not ready(me) or not me["is_visible"]:
        return None
    today = local_day()
    with db_connection() as db:
        candidates = db.execute("""
            SELECT other.* FROM users other
            WHERE other.telegram_id != :uid
              AND other.accepted_rules=1 AND other.rules_version=:rules
              AND other.is_visible=1 AND other.photo_file_id IS NOT NULL
              AND other.preferred_gender_set=1 AND other.share_username=1
              AND NOT EXISTS (
                  SELECT 1 FROM moderation_bans b
                  WHERE b.user_id=other.telegram_id AND b.active=1
                    AND (b.expires_at IS NULL OR b.expires_at>:now)
              )
              AND ((:age < 18 AND other.age BETWEEN 16 AND 17)
                   OR (:age >= 18 AND other.age >= 18))
              AND (:looking='all' OR other.gender=:looking)
              AND (other.preferred_gender='all' OR other.preferred_gender=:my_gender)
              AND NOT EXISTS(
                  SELECT 1 FROM swipes s
                  WHERE s.viewer_id=:uid AND s.target_id=other.telegram_id
                    AND s.decision='like'
              )
              AND NOT EXISTS(
                  SELECT 1 FROM daily_skip_repeats r
                  WHERE r.viewer_id=:uid AND r.target_id=other.telegram_id
                    AND r.local_day=:today
              )
              AND NOT EXISTS(
                  SELECT 1 FROM matches m WHERE
                  (m.user_low_id=:uid AND m.user_high_id=other.telegram_id)
                  OR (m.user_high_id=:uid AND m.user_low_id=other.telegram_id)
              )
              AND NOT EXISTS(
                  SELECT 1 FROM blocks b WHERE
                  (b.blocker_id=:uid AND b.blocked_id=other.telegram_id)
                  OR (b.blocker_id=other.telegram_id AND b.blocked_id=:uid)
              )
              AND NOT EXISTS(
                  SELECT 1 FROM hidden_faculties h
                  WHERE h.owner_id=other.telegram_id AND h.faculty_key=:my_faculty
              )
              AND NOT EXISTS(
                  SELECT 1 FROM hidden_faculties h
                  WHERE h.owner_id=:uid AND h.faculty_key=other.faculty_key
              )
            ORDER BY RANDOM() LIMIT 1
        """, {"uid": uid, "rules": RULES_VERSION, "age": me["age"],
               "looking": me["preferred_gender"], "my_gender": me["gender"],
               "my_faculty": me["faculty_key"], "today": today,
               "now": utc_now()}).fetchone()
    return candidates


def show_next(chat_id, uid):
    me = user_by_id(uid)
    if not ready(me):
        ask_registration(chat_id, uid)
        return
    if not me["is_visible"]:
        bot.send_message(chat_id, "⛔ Твоя анкета отключена. Включи её в настройках, чтобы искать знакомства.",
                         reply_markup=menu_navigation("settings", "profile"))
        return
    partner = candidate_for(uid)
    if partner is None:
        with db_connection() as db:
            db.execute("DELETE FROM browse_sessions WHERE viewer_id=?", (uid,))
        bot.send_message(chat_id, "🔎 Пока нет новых подходящих анкет. Загляни позже или измени "
                                  "выбор «Кого искать» в настройках анкеты.",
                         reply_markup=menu_navigation("settings", "profile"))
        return
    target = partner["telegram_id"]
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        # Если профиль ранее пропускался, разрешаем только один повторный
        # показ в каждый календарный день. Записываем именно показ, а не
        # повторный пропуск, чтобы закрытие карточки не обнуляло лимит.
        skipped = db.execute("""
            SELECT 1 FROM swipes WHERE viewer_id=? AND target_id=? AND decision='skip'
        """, (uid, target)).fetchone()
        if skipped:
            inserted = db.execute("""
                INSERT OR IGNORE INTO daily_skip_repeats
                    (viewer_id, target_id, local_day, shown_at)
                VALUES(?,?,?,?)
            """, (uid, target, local_day(), utc_now())).rowcount
            if not inserted:
                db.rollback()
                show_next(chat_id, uid)
                return
        db.execute("""
            INSERT INTO browse_sessions(viewer_id, target_id, created_at) VALUES(?, ?, ?)
            ON CONFLICT(viewer_id) DO UPDATE SET
            target_id=excluded.target_id, created_at=excluded.created_at
        """, (uid, target, utc_now()))
        db.commit()
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(icon_button("Нравится", f"swipe:like:{target}", ICONS["like"]),
           icon_button("Пропустить", f"swipe:skip:{target}", ICONS["skip"]))
    kb.row(icon_button("Нравится с сообщением", f"swipe:message:{target}", ICONS["like"]))
    kb.row(icon_button("Жалоба", f"report:{target}", ICONS["report"]),
           icon_button("Блок", f"block:{target}", ICONS["block"]))
    kb.add(icon_button("Моя анкета", "menu:profile", ICONS["profile"]))
    send_profile(chat_id, partner, reply_markup=kb)


def active_session(uid, target):
    with db_connection() as db:
        return db.execute("SELECT 1 FROM browse_sessions WHERE viewer_id=? AND target_id=?",
                          (uid, target)).fetchone() is not None


def store_match(db, uid, target):
    reciprocal = db.execute("""
        SELECT 1 FROM swipes WHERE viewer_id=? AND target_id=? AND decision='like'
    """, (target, uid)).fetchone()
    if not reciprocal:
        return False
    low, high = sorted((uid, target))
    return db.execute("""
        INSERT OR IGNORE INTO matches(user_low_id, user_high_id, created_at)
        VALUES(?, ?, ?)
    """, (low, high, utc_now())).rowcount == 1


def process_swipe(uid, target, decision, message=None):
    """Возвращает (статус, создано_совпадение, новый_лайк).

    Пропуск сохраняется; повторный показ регулирует daily_skip_repeats.
    """
    if decision not in {"like", "skip"}:
        return "invalid", False, False
    if message is not None and (decision != "like" or not 1 <= len(message) <= MAX_LIKE_MESSAGE):
        return "invalid", False, False
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT target_id FROM browse_sessions WHERE viewer_id=?", (uid,)).fetchone()
        if row is None or row["target_id"] != target:
            db.rollback()
            return "stale", False, False
        me = db.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
        other = db.execute("SELECT * FROM users WHERE telegram_id=?", (target,)).fetchone()
        if not compatible(me, other, db) or blocked_pair(db, uid, target):
            db.execute("DELETE FROM browse_sessions WHERE viewer_id=?", (uid,))
            db.execute("DELETE FROM pending_likes WHERE sender_id=?", (uid,))
            db.commit()
            return "unavailable", False, False
        previous = db.execute("SELECT decision, created_at FROM swipes WHERE viewer_id=? AND target_id=?",
                              (uid, target)).fetchone()
        # Лайк повторно не разрешаем. Пропуск разрешён и при повторном
        # показе в тот же день (а число показов ограничено отдельной таблицей).
        if previous and previous["decision"] == "like":
            db.rollback()
            return "stale", False, False
        db.execute("""
            INSERT INTO swipes(viewer_id, target_id, decision, created_at, like_message)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(viewer_id, target_id) DO UPDATE SET
                decision=excluded.decision, created_at=excluded.created_at,
                like_message=excluded.like_message
        """, (uid, target, decision, utc_now(), message))
        db.execute("DELETE FROM browse_sessions WHERE viewer_id=?", (uid,))
        db.execute("DELETE FROM pending_likes WHERE sender_id=?", (uid,))
        matched = store_match(db, uid, target) if decision == "like" else False
        db.commit()
        return "ok", matched, decision == "like"


def begin_like_message(uid, target, mode):
    """Создание временного состояния ожидания текста к лайку."""
    if mode not in {"search", "incoming"}:
        return False
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if mode == "search":
            active = db.execute("SELECT target_id FROM browse_sessions WHERE viewer_id=?",
                                (uid,)).fetchone()
            other = db.execute("SELECT * FROM users WHERE telegram_id=?", (target,)).fetchone()
            me = db.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
            valid = (active is not None and active["target_id"] == target
                     and compatible(me, other, db) and not blocked_pair(db, uid, target))
        else:
            valid = incoming_available(db, uid, target) is not None
        if not valid:
            db.rollback()
            return False
        db.execute("""
            INSERT INTO pending_likes(sender_id, target_id, mode, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(sender_id) DO UPDATE SET
               target_id=excluded.target_id, mode=excluded.mode,
               created_at=excluded.created_at
        """, (uid, target, mode, utc_now()))
        db.commit()
        return True


def get_pending_like(uid):
    with db_connection() as db:
        return db.execute("SELECT * FROM pending_likes WHERE sender_id=?", (uid,)).fetchone()


def clear_pending_like(uid):
    with db_connection() as db:
        db.execute("DELETE FROM pending_likes WHERE sender_id=?", (uid,))


def incoming_available(db, uid, liker_id):
    me = db.execute("SELECT * FROM users WHERE telegram_id=?", (uid,)).fetchone()
    liker = db.execute("SELECT * FROM users WHERE telegram_id=?", (liker_id,)).fetchone()
    if not compatible(me, liker, db) or blocked_pair(db, uid, liker_id):
        return None
    existing = db.execute("""
        SELECT 1 FROM swipes WHERE viewer_id=? AND target_id=? AND decision='like'
    """, (liker_id, uid)).fetchone()
    return liker if existing else None


def show_incoming_profile(chat_id, uid, liker_id):
    with db_connection() as db:
        liker = incoming_available(db, uid, liker_id)
        my_swipe = db.execute("""
            SELECT decision FROM swipes WHERE viewer_id=? AND target_id=?
        """, (uid, liker_id)).fetchone()
    if liker is None:
        bot.send_message(chat_id, "Анкета больше недоступна.", reply_markup=menu_navigation("profile"))
        return
    if my_swipe is not None and my_swipe["decision"] == "like":
        bot.send_message(chat_id, "❤️ Ты уже ответил(а) взаимной симпатией.", reply_markup=menu_navigation("profile"))
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(icon_button("Ответить", f"incoming:like:{liker_id}", ICONS["reply"]),
           icon_button("Пропустить", f"incoming:skip:{liker_id}", ICONS["skip"]))
    kb.add(icon_button("Ответить с сообщением", f"incoming:message:{liker_id}", ICONS["reply"]))
    kb.row(icon_button("Жалоба", f"incoming:report:{liker_id}", ICONS["report"]),
           icon_button("Заблокировать", f"incoming:block:{liker_id}", ICONS["block"]))
    kb.add(icon_button("Моя анкета", "menu:profile", ICONS["profile"]))
    with db_connection() as db:
        incoming = db.execute("""
            SELECT like_message FROM swipes WHERE viewer_id=? AND target_id=? AND decision='like'
        """, (liker_id, uid)).fetchone()
    message = incoming["like_message"] if incoming else None
    note = "\n\n💌 <b>Сообщение к симпатии:</b>\n" + escape(message) if message else ""
    bot.send_message(chat_id, "💌 <b>Этот студент поставил тебе лайк:</b>" + note)
    send_profile(chat_id, liker, reply_markup=kb)


def process_incoming(uid, liker_id, decision, message=None):
    """Обработка входящего лайка без изменения текущей сессии поиска."""
    if decision not in {"like", "skip"}:
        return "invalid", False
    if message is not None and (decision != "like" or not 1 <= len(message) <= MAX_LIKE_MESSAGE):
        return "invalid", False
    with db_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if incoming_available(db, uid, liker_id) is None:
            db.rollback()
            return "unavailable", False
        old = db.execute("""
            SELECT decision FROM swipes WHERE viewer_id=? AND target_id=?
        """, (uid, liker_id)).fetchone()
        if old is not None and old["decision"] == "like":
            db.rollback()
            return "already", False
        db.execute("""
            INSERT INTO swipes(viewer_id, target_id, decision, created_at, like_message)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(viewer_id, target_id) DO UPDATE SET
                decision=excluded.decision, created_at=excluded.created_at,
                like_message=excluded.like_message
        """, (uid, liker_id, decision, utc_now(), message))
        db.execute("DELETE FROM pending_likes WHERE sender_id=?", (uid,))
        matched = store_match(db, uid, liker_id) if decision == "like" else False
        db.commit()
        return "ok", matched


def has_like_message(sender_id, recipient_id):
    with db_connection() as db:
        row = db.execute("""
            SELECT like_message FROM swipes WHERE viewer_id=? AND target_id=? AND decision='like'
        """, (sender_id, recipient_id)).fetchone()
    return bool(row and row["like_message"])


def notify_like(sender_id, recipient_id):
    # Не отправляем входящие симпатии людям, которые отписались от канала.
    if get_active_ban(sender_id) or get_active_ban(recipient_id):
        return
    if subscription_status(recipient_id) is not True:
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(icon_button("Посмотреть анкету", f"incoming:view:{sender_id}", ICONS["view"]))
    try:
        bot.send_message(recipient_id,
            "❤️ <b>Твою анкету лайкнули!</b>\n\n"
            "Кто-то хочет познакомиться с тобой. Нажми кнопку ниже, чтобы посмотреть анкету "
            "и решить, отвечать ли взаимной симпатией." +
            ("\n\n💌 К лайку приложено сообщение." if has_like_message(sender_id, recipient_id) else ""),
            reply_markup=kb)
    except telebot.apihelper.ApiTelegramException as exc:
        logger.warning("Не получилось отправить уведомление о лайке %s: %s", recipient_id, exc)


def notify_match(uid, partner_id):
    for recipient, other_id in ((uid, partner_id), (partner_id, uid)):
        if get_active_ban(recipient) or get_active_ban(other_id):
            continue
        if subscription_status(recipient) is not True:
            continue
        person = user_by_id(other_id)
        if person is None:
            continue
        name = escape(person["display_name"] or "Студент")
        with db_connection() as db:
            note_row = db.execute("""
                SELECT like_message FROM swipes
                WHERE viewer_id=? AND target_id=? AND decision='like'
            """, (other_id, recipient)).fetchone()
        note = ("\n\n💌 <b>Сообщение:</b>\n" + escape(note_row["like_message"])
                if note_row and note_row["like_message"] else "")
        message = ("💞 <b>Взаимная симпатия!</b>\n\n"
                   f"Вы с {name} понравились друг другу.\n\n"
                   f"Telegram: {username_link(person)}" + note + "\n\n"
                   "Будь осторожен(на) при общении с незнакомыми людьми.")
        try:
            bot.send_message(recipient, message,
                             reply_markup=menu_navigation("search", "profile"),
                             disable_web_page_preview=True)
        except telebot.apihelper.ApiTelegramException as exc:
            logger.warning("Не удалось уведомить %s о совпадении: %s", recipient, exc)


def clear_old_buttons(call):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=None)
    except (telebot.apihelper.ApiTelegramException, AttributeError):
        pass


# ────────────────────── РЕГИСТРАЦИЯ / РЕДАКТИРОВАНИЕ ──────────────────────
def validate_text(field, text):
    value = text.strip()
    if field == "display_name" and 2 <= len(value) <= 40:
        return value
    if field == "age" and value.isdecimal() and MIN_AGE <= int(value) <= MAX_AGE:
        return int(value)
    if field == "course" and value.isdecimal() and 1 <= int(value) <= 6:
        return int(value)
    if field == "bio" and 1 <= len(value) <= 500:
        return value
    return None


def change_age(uid, age):
    me = user_by_id(uid)
    old_age = me["reset_previous_age"] if me["reset_previous_age"] is not None else me["age"]
    if old_age is not None and (old_age < 18) != (age < 18):
        # Между возрастными группами не должны сохраняться старые совпадения.
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM matches WHERE user_low_id=? OR user_high_id=?", (uid, uid))
            db.execute("DELETE FROM swipes WHERE viewer_id=? OR target_id=?", (uid, uid))
            db.execute("DELETE FROM browse_sessions WHERE viewer_id=? OR target_id=?", (uid, uid))
            db.execute("UPDATE users SET age=?, edit_field=NULL WHERE telegram_id=?", (age, uid))
            db.commit()
    else:
        set_fields(uid, age=age, edit_field=None)


def edit_prompt(chat_id, uid, field):
    if field not in EDITABLE:
        return
    set_fields(uid, edit_field=field)
    if field == "gender":
        bot.send_message(chat_id, "Выбери свой пол:", reply_markup=gender_buttons("edit_gender"))
    elif field == "preferred_gender":
        bot.send_message(chat_id, "Кого показывать в поиске?", reply_markup=looking_buttons("edit_looking"))
    elif field == "faculty":
        bot.send_message(chat_id, "🎓 Выбери факультет из списка:",
                         reply_markup=faculty_select_keyboard("edit_faculty"))
    else:
        prompts = {
            "display_name": "Напиши новое имя (2–40 символов).",
            "age": f"Укажи возраст ({MIN_AGE}–{MAX_AGE}).",
            "course": "Укажи курс (1–6).",
            "bio": "Напиши новое описание (1–500 символов).",
            "photo_file_id": "📷 Отправь новую фотографию обычным фото Telegram.",
        }
        bot.send_message(chat_id, prompts[field], reply_markup=back_buttons("cancel_edit"))


def require_access(message):
    if not message.from_user or message.chat.type != "private":
        return False
    if check_ban(message.chat.id, message.from_user.id):
        return False
    return ensure_subscription(message.chat.id, message.from_user.id)


# ────────────────────── TELEGRAM ──────────────────────
if BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
    raise SystemExit("Укажи BOT_TOKEN вверху файла main.py")
if not isinstance(OWNER_ID, int) or OWNER_ID <= 0:
    raise SystemExit("Укажи свой числовой Telegram ID в OWNER_ID (в начале main.py).")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)


@bot.message_handler(commands=["start"])
def cmd_start(message):
    if not require_access(message):
        return
    uid = message.from_user.id
    is_new = user_by_id(uid) is None
    touch(message.from_user)
    clear_pending_like(uid)
    clear_pending_report(uid)
    if is_new:
        # Новым пользователям нечего убирать; никаких служебных сообщений.
        set_fields(uid, inline_migrated=1)
    else:
        remove_old_reply_keyboard(message.chat.id, uid)
    if ready(user_by_id(uid)):
        show_menu(message.chat.id)
    else:
        ask_registration(message.chat.id, uid)


@bot.message_handler(commands=["help"])
def cmd_help(message):
    if not require_access(message):
        return
    touch(message.from_user)
    bot.send_message(message.chat.id,
        "/start — открыть главное меню\n"
        "/cancel — отменить изменение анкеты\n"
        "/stats — статистика администратора\n"
        "/reports — жалобы (только владельцу бота)\n"
        "/bans — действующие блокировки (только владельцу)\n\n"
        "Все кнопки доступны внутри сообщений.\n"
        "🔎 Поиск — анкеты с лайками и пропусками.\n"
        "⚙️ Настройки анкеты — редактирование и отключение.\n"
        "❤️ Входящий лайк — уведомление с кнопкой просмотра анкеты.",
        reply_markup=main_menu())


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not message.from_user or message.chat.type != "private" or message.from_user.id not in ADMIN_IDS:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db_connection() as db:
        total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        completed = db.execute("""
            SELECT COUNT(*) FROM users WHERE photo_file_id IS NOT NULL AND bio IS NOT NULL
            AND preferred_gender_set=1 AND rules_version=?
        """, (RULES_VERSION,)).fetchone()[0]
        visible = db.execute("SELECT COUNT(*) FROM users WHERE is_visible=1 AND photo_file_id IS NOT NULL").fetchone()[0]
        weekly = db.execute("SELECT COUNT(*) FROM users WHERE last_active_at>=?", (cutoff,)).fetchone()[0]
        likes = db.execute("SELECT COUNT(*) FROM swipes WHERE decision='like'").fetchone()[0]
        pairs = db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        reports = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    bot.send_message(message.chat.id,
        "📊 <b>Статистика ЛГУвинчика</b>\n"
        f"Пользователей: {total}\n"
        f"Заполненных анкет: {completed}\n"
        f"Включённых анкет с фото: {visible}\n"
        f"Активных за 7 дней: {weekly}\n"
        f"Лайков: {likes}\n"
        f"Взаимных симпатий: {pairs}\n"
        f"Жалоб: {reports}")


@bot.message_handler(commands=["reports"])
def cmd_reports(message):
    if not message.from_user or message.chat.type != "private" or message.from_user.id != OWNER_ID:
        return
    with db_connection() as db:
        rows = db.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 20").fetchall()
    if not rows:
        bot.send_message(message.chat.id, "🚩 Жалоб пока нет.")
        return
    bot.send_message(message.chat.id, "🚩 <b>Последние 20 жалоб:</b>")
    for row in rows:
        kb = report_keyboard(row["id"]) if row["status"] == "open" else None
        content = (f"№{row['id']} • {escape(row['status'])}\n"
                   f"На ID: <code>{row['reported_id']}</code>\n"
                   f"Текст: {escape(row['reason'][:500])}")
        bot.send_message(message.chat.id, content, reply_markup=kb)


@bot.message_handler(commands=["bans"])
def cmd_bans(message):
    if not message.from_user or message.chat.type != "private" or message.from_user.id != OWNER_ID:
        return
    with db_connection() as db:
        rows = db.execute("""SELECT * FROM moderation_bans WHERE active=1
             AND (expires_at IS NULL OR expires_at>?) ORDER BY updated_at DESC LIMIT 20""",
             (utc_now(),)).fetchall()
    if not rows:
        bot.send_message(message.chat.id,"✅ Активных блокировок нет.")
        return
    bot.send_message(message.chat.id,"⛔ <b>Активные блокировки:</b>")
    for row in rows:
        kb = types.InlineKeyboardMarkup()
        kb.add(icon_button("Разблокировать",f"mod:unban:{row['user_id']}"))
        bot.send_message(message.chat.id,
                         f"ID: <code>{row['user_id']}</code>\n"
                         f"До: {escape(row['expires_at'] or 'бессрочно')}", reply_markup=kb)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    if not require_access(message):
        return
    touch(message.from_user)
    uid = message.from_user.id
    if ready(user_by_id(uid)):
        set_fields(uid, edit_field=None)
        clear_pending_like(uid)
        clear_pending_report(uid)
        bot.send_message(message.chat.id, "Действие отменено.")
        show_settings(message.chat.id, uid)
    else:
        ask_registration(message.chat.id, uid)


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    if not call.message or call.message.chat.type != "private":
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data or ""

    # Модерация доступна только владельцу бота, независимо от его подписки.
    if data.startswith("mod:"):
        if uid != OWNER_ID:
            bot.answer_callback_query(call.id, "Недостаточно прав", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            action, report_id = parts[1], int(parts[2])
            if action == "unban":
                ok = unban_user(report_id,uid)
                bot.answer_callback_query(call.id,"Разблокирован" if ok else "Блокировка не найдена")
                if ok:
                    clear_old_buttons(call)
                    bot.send_message(chat_id,f"✅ ID {report_id} разблокирован.")
                    try:
                        bot.send_message(report_id,"✅ <b>Блокировка снята.</b> Ты снова можешь пользоваться ботом.")
                    except Exception:
                        logger.warning("Не удалось уведомить ID %s о разблокировке",report_id)
                return
            with db_connection() as db:
                report = db.execute("SELECT * FROM reports WHERE id=?",(report_id,)).fetchone()
            if not report:
                bot.answer_callback_query(call.id,"Жалоба не найдена",show_alert=True)
                return
            if action == "view":
                user = user_by_id(report["reported_id"])
                bot.answer_callback_query(call.id)
                if user:
                    send_profile(chat_id,user,own=False,reply_markup=report_keyboard(report_id))
                else:
                    bot.send_message(chat_id,"Анкета удалена.")
                return
            if action == "back":
                bot.answer_callback_query(call.id)
                bot.send_message(chat_id,f"🚩 Жалоба №{report_id}",reply_markup=report_keyboard(report_id))
                return
            if report["status"] != "open":
                bot.answer_callback_query(call.id,"Жалоба уже рассмотрена",show_alert=True)
                return
            if action == "banmenu":
                bot.answer_callback_query(call.id)
                bot.send_message(chat_id,f"⛔ Выбери срок для ID {report['reported_id']}:",
                                 reply_markup=moderation_ban_keyboard(report_id))
                return
            if action == "dismiss":
                ok = dismiss_report(report_id,uid)
                bot.answer_callback_query(call.id,"Жалоба отклонена" if ok else "Уже обработана")
                if ok:
                    clear_old_buttons(call)
                    bot.send_message(chat_id,f"✅ Жалоба №{report_id} отклонена.")
                return
            if action == "ban" and len(parts)==4 and parts[3] in BAN_PERIODS:
                success = ban_user(report["reported_id"],uid,report_id,parts[3])
                bot.answer_callback_query(call.id,"Блокировка установлена" if success else "Не удалось заблокировать")
                if success:
                    clear_old_buttons(call)
                    bot.send_message(chat_id,f"⛔ ID {report['reported_id']} заблокирован. Срок: {BAN_PERIODS[parts[3]][0]}.")
                    notify_ban(report["reported_id"])
                return
        bot.answer_callback_query(call.id,"Кнопка недействительна",show_alert=True)
        return

    if check_ban(chat_id,uid,callback_id=call.id):
        return

    # Проверяем канал до любых действий, включая старые кнопки под анкетами.
    # Исключение только для «Проверить подписку»: там та же проверка, после
    # которой открывается экран анкеты или текущий шаг регистрации.
    if data == "subscription:check":
        if not ensure_subscription(chat_id, uid, callback_id=call.id):
            return
        touch(call.from_user)
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
        clear_old_buttons(call)
        show_menu(chat_id)
        return
    if not ensure_subscription(chat_id, uid, callback_id=call.id):
        return

    touch(call.from_user)
    u = user_by_id(uid)

    if data == "rules:accept":
        if missing_field(u) != "rules":
            bot.answer_callback_query(call.id, "Правила уже приняты")
            return
        set_fields(uid, accepted_rules=1, rules_version=RULES_VERSION, share_username=1)
        bot.answer_callback_query(call.id, "Правила приняты")
        clear_old_buttons(call)
        ask_registration(chat_id, uid)
        return

    if data.startswith("register_gender:") and missing_field(u) == "gender":
        gender = data.split(":", 1)[1]
        if gender in SELECTABLE_GENDERS:
            set_fields(uid, gender=gender)
            bot.answer_callback_query(call.id, "Сохранено")
            clear_old_buttons(call)
            ask_registration(chat_id, uid)
            return

    if data.startswith("register_looking:") and missing_field(u) == "preferred_gender":
        looking = data.split(":", 1)[1]
        if looking in LOOKING:
            set_fields(uid, preferred_gender=looking, preferred_gender_set=1)
            bot.answer_callback_query(call.id, "Сохранено")
            clear_old_buttons(call)
            ask_registration(chat_id, uid)
            return

    if data.startswith("register_faculty:") and missing_field(u) == "faculty":
        index_text = data.partition(":")[2]
        if index_text.isdecimal() and int(index_text) < len(FACULTIES):
            set_fields(uid, faculty=FACULTIES[int(index_text)])
            bot.answer_callback_query(call.id, "Факультет сохранён")
            clear_old_buttons(call)
            ask_registration(chat_id, uid)
            return

    if data == "registration:skip_bio" and missing_field(u) == "bio":
        set_fields(uid, bio="")
        bot.answer_callback_query(call.id, "Описание пропущено")
        clear_old_buttons(call)
        ask_registration(chat_id, uid)
        return

    if not ready(u):
        bot.answer_callback_query(call.id, "Сначала заверши регистрацию", show_alert=True)
        ask_registration(chat_id, uid)
        return

    # Навигация — все действия исключительно через inline-кнопки.
    if data.startswith("menu:"):
        action = data.partition(":")[2]
        if action in {"home", "search", "profile", "settings"}:
            bot.answer_callback_query(call.id)
            set_fields(uid, edit_field=None)
            clear_pending_like(uid)
            clear_pending_report(uid)
            if action in {"home", "profile"}:
                show_own_profile(chat_id, uid)
            elif action == "search":
                show_next(chat_id, uid)
            else:
                show_settings(chat_id, uid)
            return

    if data == "nav:menu":
        bot.answer_callback_query(call.id)
        set_fields(uid, edit_field=None)
        clear_pending_like(uid)
        clear_pending_report(uid)
        show_own_profile(chat_id, uid)
        return

    if data == "nav:cancel_edit":
        bot.answer_callback_query(call.id, "Изменение отменено")
        clear_old_buttons(call)
        set_fields(uid, edit_field=None)
        clear_pending_like(uid)
        clear_pending_report(uid)
        show_settings(chat_id, uid)
        return

    if data == "settings:faculties":
        bot.answer_callback_query(call.id)
        show_faculty_hiding(chat_id, uid)
        return

    if data.startswith("faculty:page:"):
        page_str = data.split(":", 2)[2]
        if page_str.isdigit():
            bot.answer_callback_query(call.id)
            show_faculty_hiding(chat_id, uid, int(page_str))
            return

    if data.startswith("faculty:toggle:"):
        parts = data.split(":")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            page, index = int(parts[2]), int(parts[3])
            if 0 <= index < len(FACULTIES) and index // FACULTIES_PAGE_SIZE == page:
                label = FACULTIES[index]
                key = normalize_faculty(label)
                with db_connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    old = db.execute("""
                        SELECT 1 FROM hidden_faculties WHERE owner_id=? AND faculty_key=?
                    """, (uid, key)).fetchone()
                    if old:
                        db.execute("DELETE FROM hidden_faculties WHERE owner_id=? AND faculty_key=?", (uid, key))
                    else:
                        db.execute("""
                            INSERT INTO hidden_faculties(owner_id,faculty_key,faculty_label,created_at)
                            VALUES(?,?,?,?)
                        """, (uid, key, label, utc_now()))
                    db.execute("DELETE FROM browse_sessions WHERE viewer_id=? OR target_id=?", (uid, uid))
                    db.commit()
                bot.answer_callback_query(call.id, "Ограничение убрано" if old else "Анкета скрыта")
                show_faculty_hiding(chat_id, uid, page)
                return

    if data == "faculty:reset":
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM hidden_faculties WHERE owner_id=?", (uid,))
            db.execute("DELETE FROM browse_sessions WHERE viewer_id=? OR target_id=?", (uid, uid))
            db.commit()
        bot.answer_callback_query(call.id, "Скрытие от факультетов сброшено")
        show_faculty_hiding(chat_id, uid)
        return

    if data == "settings:reset_confirm":
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id,
            "🔄 <b>Заполнить анкету заново?</b>\n\n"
            "Ты повторно укажешь имя, возраст, пол, кого искать, факультет, "
            "курс, описание и фотографию.\n\n"
            "Пока заполняешь анкету, она будет скрыта из поиска. "
            "Старые лайки, совпадения, настройки скрытия от факультетов "
            "и статус включения анкеты сохранятся.",
            reply_markup=reset_confirmation_keyboard())
        return

    if data == "settings:reset_start":
        if reset_profile(uid):
            bot.answer_callback_query(call.id, "Начинаем заполнение")
            clear_old_buttons(call)
            bot.send_message(chat_id, "🔄 Анкета временно скрыта. Заполни её заново:")
            ask_registration(chat_id, uid)
        else:
            bot.answer_callback_query(call.id, "Повторное заполнение уже началось", show_alert=True)
        return

    if data == "settings:toggle":
        bot.answer_callback_query(call.id)
        next_visibility = 0 if u["is_visible"] else 1
        with db_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE users SET is_visible=?, edit_field=NULL WHERE telegram_id=?",
                       (next_visibility, uid))
            if not next_visibility:
                db.execute("DELETE FROM browse_sessions WHERE viewer_id=? OR target_id=?", (uid, uid))
            db.commit()
        clear_old_buttons(call)
        bot.send_message(chat_id,
            "⛔ Анкета отключена: тебя не увидят в поиске." if not next_visibility
            else "✅ Анкета включена и снова участвует в поиске.")
        show_settings(chat_id, uid)
        return

    if data.startswith("edit:"):
        field = data.partition(":")[2]
        if field in EDITABLE:
            bot.answer_callback_query(call.id)
            clear_pending_like(uid)
            edit_prompt(chat_id, uid, field)
            return

    if data.startswith("edit_gender:") and u["edit_field"] == "gender":
        choice = data.partition(":")[2]
        if choice in SELECTABLE_GENDERS:
            set_fields(uid, gender=choice, edit_field=None)
            bot.answer_callback_query(call.id, "Сохранено")
            clear_old_buttons(call)
            show_settings(chat_id, uid)
            return

    if data.startswith("edit_faculty:") and u["edit_field"] == "faculty":
        index_text = data.partition(":")[2]
        if index_text.isdecimal() and int(index_text) < len(FACULTIES):
            set_fields(uid, faculty=FACULTIES[int(index_text)], edit_field=None)
            with db_connection() as db:
                db.execute("DELETE FROM browse_sessions WHERE viewer_id=? OR target_id=?", (uid, uid))
            bot.answer_callback_query(call.id, "Факультет обновлён")
            clear_old_buttons(call)
            show_settings(chat_id, uid)
            return

    if data.startswith("edit_looking:") and u["edit_field"] == "preferred_gender":
        choice = data.partition(":")[2]
        if choice in LOOKING:
            set_fields(uid, preferred_gender=choice, edit_field=None)
            bot.answer_callback_query(call.id, "Сохранено")
            clear_old_buttons(call)
            show_settings(chat_id, uid)
            return

    if data == "like:cancel":
        clear_pending_like(uid)
        bot.answer_callback_query(call.id, "Отменено")
        clear_old_buttons(call)
        bot.send_message(chat_id, "Сообщение отменено. Ты можешь воспользоваться кнопками у анкеты.")
        return

    if data.startswith("swipe:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[1] in {"like", "skip", "message"} and parts[2].isdigit():
            target = int(parts[2])
            if parts[1] == "message":
                if not begin_like_message(uid, target, "search"):
                    bot.answer_callback_query(call.id, "Анкета больше недоступна", show_alert=True)
                    return
                bot.answer_callback_query(call.id)
                bot.send_message(chat_id,
                    f"💌 Напиши сообщение к симпатии (1–{MAX_LIKE_MESSAGE} символов). "
                    "Сообщение будет показано пользователю при просмотре твоей анкеты.",
                    reply_markup=inline_buttons("like", [("Отменить", "cancel")]))
                return
            clear_pending_like(uid)
            status, created_match, new_like = process_swipe(uid, target, parts[1])
            if status != "ok":
                bot.answer_callback_query(call.id, "Анкета уже просмотрена или недоступна", show_alert=True)
                return
            bot.answer_callback_query(call.id, "❤️ Лайк отправлен" if new_like else "Пропущено")
            clear_old_buttons(call)
            if created_match:
                notify_match(uid, target)
            elif new_like:
                notify_like(uid, target)
            show_next(chat_id, uid)
            return

    if data.startswith("incoming:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[2].isdigit():
            action, source_id = parts[1], int(parts[2])
            if action == "view":
                bot.answer_callback_query(call.id)
                show_incoming_profile(chat_id, uid, source_id)
                return
            if action == "message":
                if not begin_like_message(uid, source_id, "incoming"):
                    bot.answer_callback_query(call.id, "Анкета больше недоступна", show_alert=True)
                    return
                bot.answer_callback_query(call.id)
                bot.send_message(chat_id,
                    f"💌 Напиши сообщение к ответной симпатии (1–{MAX_LIKE_MESSAGE} символов).",
                    reply_markup=inline_buttons("like", [("Отменить", "cancel")]))
                return
            if action in {"like", "skip"}:
                clear_pending_like(uid)
                status, created_match = process_incoming(uid, source_id, action)
                if status != "ok":
                    bot.answer_callback_query(call.id, "Симпатия уже обработана или недоступна", show_alert=True)
                    return
                bot.answer_callback_query(call.id, "❤️ Взаимная симпатия!" if created_match else "Пропущено")
                clear_old_buttons(call)
                if created_match:
                    notify_match(uid, source_id)
                else:
                    show_menu(chat_id)
                return
            if action == "report":
                if not start_report(uid,source_id,"incoming"):
                    bot.answer_callback_query(call.id,"Анкета недоступна",show_alert=True)
                    return
                bot.answer_callback_query(call.id)
                bot.send_message(chat_id,
                    f"🚩 Напиши жалобу обычным сообщением ({MIN_REPORT_LENGTH}–{MAX_REPORT_LENGTH} символов). "
                    "Опиши, что произошло. Жалобу получит только владелец бота.",
                    reply_markup=inline_buttons("report",[("Отменить жалобу","cancel")]))
                return
            if action == "block":
                with db_connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    source = incoming_available(db, uid, source_id)
                    if source is None:
                        db.rollback()
                        bot.answer_callback_query(call.id, "Анкета больше недоступна")
                        return
                    db.execute("""
                        INSERT OR IGNORE INTO blocks(blocker_id, blocked_id, created_at)
                        VALUES(?, ?, ?)
                    """, (uid, source_id, utc_now()))
                    db.commit()
                bot.answer_callback_query(call.id, "Пользователь заблокирован")
                clear_old_buttons(call)
                show_menu(chat_id)
                return

    if data == "report:cancel":
        clear_pending_report(uid)
        bot.answer_callback_query(call.id,"Жалоба отменена")
        clear_old_buttons(call)
        show_menu(chat_id)
        return

    if data.startswith("report:"):
        target_str = data.partition(":")[2]
        if target_str.isdigit() and start_report(uid,int(target_str),"search"):
            bot.answer_callback_query(call.id)
            bot.send_message(chat_id,
                f"🚩 Напиши жалобу обычным сообщением ({MIN_REPORT_LENGTH}–{MAX_REPORT_LENGTH} символов). "
                "Опиши, что произошло. Жалобу получит только владелец бота.",
                reply_markup=inline_buttons("report",[("Отменить жалобу","cancel")]))
            return

    if data.startswith("block:"):
        target_str = data.partition(":")[2]
        if target_str.isdigit() and active_session(uid, int(target_str)):
            with db_connection() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""
                    INSERT OR IGNORE INTO blocks(blocker_id, blocked_id, created_at)
                    VALUES(?, ?, ?)
                """, (uid, int(target_str), utc_now()))
                db.execute("DELETE FROM browse_sessions WHERE viewer_id=?", (uid,))
                db.commit()
            bot.answer_callback_query(call.id, "Пользователь заблокирован")
            clear_old_buttons(call)
            show_next(chat_id, uid)
            return

    bot.answer_callback_query(call.id, "Эта кнопка больше не действует", show_alert=True)


@bot.message_handler(content_types=["photo"])
def photo_handler(message):
    if not require_access(message):
        return
    touch(message.from_user)
    uid = message.from_user.id
    u = user_by_id(uid)
    if missing_field(u) == "photo_file_id":
        set_fields(uid, photo_file_id=message.photo[-1].file_id)
        finish_profile_reset(uid)
        bot.send_message(message.chat.id, "✅ Фото добавлено. Анкета готова!")
        show_own_profile(message.chat.id, uid)
    elif ready(u) and u["edit_field"] == "photo_file_id":
        set_fields(uid, photo_file_id=message.photo[-1].file_id, edit_field=None)
        bot.send_message(message.chat.id, "✅ Фото обновлено.")
        show_settings(message.chat.id, uid)
    else:
        bot.send_message(message.chat.id, "Изменить фото можно через «⚙️ Настройки анкеты».",
                         reply_markup=main_menu())


@bot.message_handler(content_types=["text"])
def text_handler(message):
    if not require_access(message):
        return
    touch(message.from_user)
    uid = message.from_user.id
    u = user_by_id(uid)
    text = (message.text or "").strip()
    if text.startswith("/"):
        bot.send_message(message.chat.id, "Неизвестная команда. Введи /help.", reply_markup=main_menu())
        return
    report = pending_report(uid)
    if report:
        if not MIN_REPORT_LENGTH <= len(text) <= MAX_REPORT_LENGTH:
            bot.send_message(message.chat.id,
                f"Укажи подробности жалобы от {MIN_REPORT_LENGTH} до {MAX_REPORT_LENGTH} символов. "
                "Или нажми «Отменить жалобу».")
            return
        status, report_id = submit_report(uid,text)
        if status == "ok":
            delivered = notify_owner_report(report_id)
            bot.send_message(message.chat.id,
                "✅ Жалоба сохранена и передана владельцу бота." if delivered else
                "✅ Жалоба сохранена. Владелец сможет посмотреть её через /reports.")
        elif status == "duplicate":
            bot.send_message(message.chat.id,"Жалоба на эту анкету уже находится на рассмотрении.")
        else:
            bot.send_message(message.chat.id,"Анкета больше недоступна для жалобы. Открой поиск заново.")
        show_menu(message.chat.id)
        return

    step = missing_field(u)
    if step:
        if step in {"rules", "gender", "preferred_gender", "faculty", "photo_file_id"}:
            ask_registration(message.chat.id, uid)
            return
        value = validate_text(step, text)
        if value is None:
            bot.send_message(message.chat.id, "❌ Некорректное значение. Попробуй ещё раз.")
            return
        if step == "age":
            change_age(uid, value)
        else:
            set_fields(uid, **{step: value})
        ask_registration(message.chat.id, uid)
        return

    pending = get_pending_like(uid)
    if pending:
        if not 1 <= len(text) <= MAX_LIKE_MESSAGE:
            bot.send_message(message.chat.id,
                f"Сообщение должно содержать от 1 до {MAX_LIKE_MESSAGE} символов. "
                "Попробуй ещё раз или нажми «Отменить».")
            return
        # Используем исходную активную сессию, а не произвольную ссылку на анкету.
        if pending["mode"] == "search":
            status, created_match, new_like = process_swipe(uid, pending["target_id"], "like", text)
        else:
            status, created_match = process_incoming(uid, pending["target_id"], "like", text)
            new_like = False
        if status != "ok":
            clear_pending_like(uid)
            bot.send_message(message.chat.id, "Анкета больше недоступна. Открой поиск снова.",
                             reply_markup=menu_navigation("search", "profile"))
            return
        clear_pending_like(uid)
        bot.send_message(message.chat.id, "💌 Симпатия с сообщением отправлена!")
        if created_match:
            notify_match(uid, pending["target_id"])
        elif new_like:
            notify_like(uid, pending["target_id"])
        if pending["mode"] == "search":
            show_next(message.chat.id, uid)
        else:
            show_menu(message.chat.id)
        return

    if u["edit_field"]:
        field = u["edit_field"]
        if field in {"gender", "preferred_gender", "faculty", "photo_file_id"}:
            bot.send_message(message.chat.id,
                "Воспользуйся кнопками выше или отправь фотографию. Для отмены: /cancel.")
            return
        value = validate_text(field, text)
        if value is None:
            bot.send_message(message.chat.id, "❌ Не подходит по формату. Попробуй ещё раз или /cancel.")
            return
        if field == "age":
            change_age(uid, value)
        else:
            set_fields(uid, **{field: value, "edit_field": None})
        bot.send_message(message.chat.id, "✅ Анкета обновлена.")
        show_settings(message.chat.id, uid)
        return

    # Кнопки старых версий теперь не используются: предлагаем inline-меню.
    show_menu(message.chat.id)


def run_bot_forever():
    """Перезапуск polling при сетевой ошибке даже на этапе skip_pending.

    pyTelegramBotAPI выполняет skip_pending до внутреннего блока try/except.
    Поэтому при нестабильном интернете тайм-аут там может завершить процесс.
    """
    from telebot import apihelper

    # Больше времени на TLS-подключение, в том числе через прокси.
    apihelper.CONNECT_TIMEOUT = 35
    apihelper.READ_TIMEOUT = 60

    proxy_names = [name for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY",
                                   "http_proxy", "ALL_PROXY", "all_proxy") if os.getenv(name)]
    if proxy_names:
        # Не выводим адреса и пароли прокси в журнал.
        logger.warning("В окружении обнаружен прокси (%s). При тайм-аутах проверь его доступность.",
                       ", ".join(proxy_names))

    retry_delay = 5
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True,
                timeout=40,
                long_polling_timeout=15,
                logger_level=logging.WARNING,
            )
            logger.warning("Polling завершился. Попытка переподключения через %s с.", retry_delay)
        except (RequestException, TimeoutError, OSError) as exc:
            logger.warning("Telegram API недоступен: %s. Новая попытка через %s с.",
                           type(exc).__name__, retry_delay)
        # Ошибки кода и неверные настройки токена не маскируются бесконечными попытками.
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    init_db()
    logger.info("ЛГУвинчик запущен (Inline UI). База: %s", DB_PATH)
    run_bot_forever()
