import asyncio
import os
import sqlite3
import json
import re
import time
import sys
import fcntl
import signal
import logging
import shutil
from collections import defaultdict
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, ChatJoinRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramNetworkError, TelegramServerError

MAIN_ADMINS = [5479947797, 1301864145]
token = os.environ.get("API_TOKEN")
data_folder_path = os.path.dirname(__file__) + "/data"
LOCK_FILE = "/tmp/lavender_bot.lock"

if not token:
    raise ValueError("API_TOKEN не задан в переменных окружения")

MAX_MESSAGES_PER_SECOND = 2
MAX_MESSAGES_PER_MINUTE = 20
SPAM_BAN_TIME = 300
user_messages = defaultdict(list)
banned_users = {}
auto_unlock_tasks = {}
admins = []

def setup_logging():
    logs_dir = os.path.join(data_folder_path, "Logs")
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(logs_dir, f"bot_{timestamp}.log")
    
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()
logger.info("Bot starting...")

class Form(StatesGroup):
    answering_question = State()
    waiting_for_comment = State()
    waiting_for_question_key = State()
    waiting_for_question_text = State()
    waiting_for_question_display = State()
    waiting_for_display_text = State()
    waiting_for_edit_select = State()
    waiting_for_edit_text = State()
    waiting_for_edit_display = State()
    waiting_for_delete_select = State()
    waiting_for_text_select = State()
    waiting_for_text_edit = State()
    waiting_for_format_select = State()
    waiting_for_decline_choice = State()
    waiting_for_new_admin = State()
    waiting_for_remove_admin = State()
    waiting_for_rejected_search = State()
    waiting_for_applied_search = State()
    waiting_for_blacklist_add = State()
    waiting_for_blacklist_remove = State()
    waiting_for_blacklist_search = State()
    waiting_for_template_add = State()
    waiting_for_template_delete = State()
    waiting_for_invite_chat = State()

def is_main_admin(user_id):
    return user_id in MAIN_ADMINS

def is_admin(user_id):
    return user_id in admins

def load_admins():
    global admins
    try:
        result = db_execute("SELECT user_id, tag FROM admins", fetchall=True)
        if result:
            admins = [row[0] for row in result]
            for row in result:
                user_id, tag = row
                if not tag:
                    app_result = db_execute("SELECT tag FROM applications WHERE user = ?", (user_id,), fetch=True)
                    if app_result and app_result[0]:
                        db_execute("UPDATE admins SET tag = ? WHERE user_id = ?", (app_result[0], user_id))
        for main_admin in MAIN_ADMINS:
            if main_admin not in admins:
                admins.append(main_admin)
                db_execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (main_admin,))
        logger.info(f"Загружены админы: {admins}")
    except Exception as e:
        logger.error(f"Ошибка загрузки админов: {e}")

def add_admin(user_id):
    try:
        if user_id not in admins:
            admins.append(user_id)
            tag = None
            try:
                user = bot.get_chat(user_id)
                tag = user.username
            except:
                pass
            db_execute("INSERT OR IGNORE INTO admins (user_id, tag) VALUES (?, ?)", (user_id, tag))
            logger.info(f"Добавлен админ: {user_id} (@{tag})")
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка добавления админа: {e}")
        return False

def remove_admin(user_id):
    try:
        if user_id in MAIN_ADMINS:
            return False
        if user_id in admins:
            admins.remove(user_id)
            db_execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            logger.info(f"Удален админ: {user_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка удаления админа: {e}")
        return False

def backup_database():
    try:
        db_path = get_db_path()
        if not os.path.exists(db_path):
            return
        
        backup_dir = os.path.join(data_folder_path, "Backups")
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"Lavender_backup_{timestamp}.db")
        shutil.copy2(db_path, backup_path)
        logger.info(f"Создан бэкап базы: {backup_path}")
        
        backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.db')])
        while len(backups) > 2:
            old_backup = os.path.join(backup_dir, backups[0])
            os.remove(old_backup)
            logger.info(f"Удален старый бэкап: {old_backup}")
            backups.pop(0)
            
    except Exception as e:
        logger.error(f"Ошибка бэкапа: {e}")

def escape_html(text):
    if text is None:
        return ""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_user_link(user_id, tag):
    if tag and tag != "no_username":
        return f'<a href="tg://user?id={user_id}">@{escape_html(tag)}</a>'
    else:
        return f'<a href="tg://user?id={user_id}">{user_id}</a>'

def is_user_banned(user_id):
    if user_id in banned_users:
        if datetime.now() < banned_users[user_id]:
            return True
        else:
            del banned_users[user_id]
    return False

def check_spam(user_id):
    now = datetime.now()
    user_messages[user_id] = [t for t in user_messages[user_id] if (now - t).total_seconds() < 60]
    user_messages[user_id].append(now)
    
    last_second = [t for t in user_messages[user_id] if (now - t).total_seconds() < 1]
    if len(last_second) > MAX_MESSAGES_PER_SECOND:
        banned_users[user_id] = now + timedelta(seconds=SPAM_BAN_TIME)
        logger.warning(f"Пользователь {user_id} забанен за флуд")
        return True, "бан на 5 минут за флуд"
    
    last_minute = [t for t in user_messages[user_id] if (now - t).total_seconds() < 60]
    if len(last_minute) > MAX_MESSAGES_PER_MINUTE:
        banned_users[user_id] = now + timedelta(seconds=SPAM_BAN_TIME)
        logger.warning(f"Пользователь {user_id} забанен за флуд")
        return True, "бан на 5 минут за флуд"
    
    return False, ""

def get_db_path():
    return os.path.join(data_folder_path, "Lavender.db")

def ensure_db_directory():
    if not os.path.exists(data_folder_path):
        os.makedirs(data_folder_path, exist_ok=True)

def check_single_instance():
    global lock_file
    try:
        lock_file = open(LOCK_FILE, 'w')
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        return True
    except IOError:
        logger.error("Другой экземпляр бота уже запущен!")
        return False

def db_connect():
    max_retries = 2
    retry_delay = 0.5
    for attempt in range(max_retries):
        try:
            ensure_db_directory()
            conn = sqlite3.connect(get_db_path(), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=3000")
            return conn
        except sqlite3.Error as e:
            logger.error(f"Попытка подключения {attempt + 1} к БД не удалась: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                raise

def db_execute(query, params=None, fetch=False, fetchall=False):
    conn = None
    try:
        conn = db_connect()
        cursor = conn.cursor()
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        if fetch:
            result = cursor.fetchone()
        elif fetchall:
            result = cursor.fetchall()
        else:
            result = None
            conn.commit()
        return result
    except sqlite3.Error as e:
        logger.error(f"Ошибка БД: {e}")
        return None
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

def get_moscow_time():
    return datetime.utcnow() + timedelta(hours=3)

def get_text(text_key, **kwargs):
    try:
        result = db_execute("SELECT text_content, parse_mode FROM texts WHERE text_key = ?", (text_key,), fetch=True)
        if result:
            text = result[0]
            parse_mode = result[1] if result[1] else "HTML"
            for key, value in kwargs.items():
                text = text.replace("{" + key + "}", str(value))
            return text, parse_mode
    except Exception as e:
        logger.error(f"Ошибка получения текста {text_key}: {e}")
    return "Текст не задан", "HTML"

def get_questions():
    try:
        result = db_execute("SELECT question_key, question_text, display_text FROM questions ORDER BY order_num", fetchall=True)
        return result if result else []
    except Exception as e:
        logger.error(f"Ошибка получения вопросов: {e}")
        return []

def get_application_answers(user_id):
    try:
        result = db_execute("SELECT answers FROM applications WHERE user = ?", (user_id,), fetch=True)
        if result and result[0]:
            try:
                return json.loads(result[0])
            except:
                return {}
    except Exception as e:
        logger.error(f"Ошибка получения ответов для {user_id}: {e}")
    return {}

def add_to_blacklist(user_id, tag, reason, admin_id):
    try:
        db_execute(
            "INSERT OR IGNORE INTO blacklist (user_id, tag, reason, added_by) VALUES (?, ?, ?, ?)",
            (user_id, tag, reason, admin_id)
        )
        logger.info(f"Пользователь {user_id} добавлен в черный список админом {admin_id}")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления в черный список: {e}")
        return False

def remove_from_blacklist(user_id):
    try:
        db_execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        logger.info(f"Пользователь {user_id} удален из черного списка")
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления из черного списка: {e}")
        return False

def is_in_blacklist(user_id):
    try:
        result = db_execute("SELECT user_id FROM blacklist WHERE user_id = ?", (user_id,), fetch=True)
        return result is not None
    except Exception as e:
        logger.error(f"Ошибка проверки черного списка: {e}")
        return False

def get_blacklist():
    try:
        result = db_execute("SELECT user_id, tag, reason, added_at FROM blacklist ORDER BY added_at DESC", fetchall=True)
        return result if result else []
    except Exception as e:
        logger.error(f"Ошибка получения черного списка: {e}")
        return []

def get_decline_templates():
    try:
        result = db_execute("SELECT id, template_text, added_by, added_at FROM decline_templates ORDER BY added_at DESC", fetchall=True)
        return result if result else []
    except Exception as e:
        logger.error(f"Ошибка получения шаблонов: {e}")
        return []

def add_decline_template(template_text, admin_id):
    try:
        db_execute("INSERT INTO decline_templates (template_text, added_by) VALUES (?, ?)", (template_text, admin_id))
        logger.info(f"Админ {admin_id} добавил шаблон: {template_text}")
        return True
    except Exception as e:
        logger.error(f"Ошибка добавления шаблона: {e}")
        return False

def delete_decline_template(template_id):
    try:
        db_execute("DELETE FROM decline_templates WHERE id = ?", (template_id,))
        logger.info(f"Шаблон {template_id} удален")
        return True
    except Exception as e:
        logger.error(f"Ошибка удаления шаблона: {e}")
        return False

def is_private_chat(message: Message):
    return message.chat.type == "private"

def lock_application(user_id, admin_id):
    now = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
    db_execute("UPDATE applications SET locked_by = ?, locked_at = ? WHERE user = ?", 
               (admin_id, now, user_id))

def unlock_application(user_id):
    db_execute("UPDATE applications SET locked_by = NULL, locked_at = NULL WHERE user = ?", (user_id,))

def is_application_locked(user_id):
    result = db_execute("SELECT locked_by, locked_at FROM applications WHERE user = ?", (user_id,), fetch=True)
    if result and result[0]:
        locked_at_str = str(result[1])
        try:
            if '.' in locked_at_str:
                locked_at = datetime.strptime(locked_at_str, '%Y-%m-%d %H:%M:%S.%f')
            else:
                locked_at = datetime.strptime(locked_at_str, '%Y-%m-%d %H:%M:%S')
        except:
            locked_at = datetime.now() - timedelta(minutes=10)
        
        if (datetime.now() - locked_at).total_seconds() > 300:
            unlock_application(user_id)
            return None
        return result[0]
    return None

async def auto_unlock_application(user_id, admin_id, message, state: FSMContext):
    await asyncio.sleep(300)
    locked_by = is_application_locked(user_id)
    if locked_by == admin_id:
        unlock_application(user_id)
        try:
            await message.answer("Время на рассмотрение заявки истекло. Заявка разблокирована. Возвращаю в главное меню.")
            await start_command(message, state)
        except:
            pass

async def notify_admins_about_review(message: Message, user_id: int, admin_id: int, action: str):
    try:
        result = db_execute("SELECT tag FROM applications WHERE user = ?", (user_id,), fetch=True)
        tag = result[0] if result and result[0] else str(user_id)
        
        result_count = db_execute("SELECT COUNT(*) FROM applications WHERE state = ?", ("sended",), fetch=True)
        count = result_count[0] if result_count else 0
        
        admin_result = db_execute("SELECT tag FROM admins WHERE user_id = ?", (admin_id,), fetch=True)
        admin_tag = admin_result[0] if admin_result else None
        admin_name = f"@{admin_tag}" if admin_tag else str(admin_id)
        
        if tag and tag != "no_username":
            user_display = f"@{tag}"
        else:
            user_display = str(user_id)
        
        for admin in admins:
            if admin != admin_id:
                try:
                    await bot.send_message(
                        admin,
                        f"{admin_name} {action} анкету {user_display}\n\nУ вас {count} анкет",
                        disable_notification=True
                    )
                except:
                    pass
    except Exception as e:
        logger.error(f"Ошибка уведомления о рассмотрении: {e}")

def get_age_from_answers(answers_json):
    try:
        answers = json.loads(answers_json) if answers_json else {}
        return answers.get("age", "Не указан")
    except:
        return "Не указан"

def format_time_diff(diff):
    if diff.days > 365:
        years = diff.days // 365
        months = (diff.days % 365) // 30
        if years == 1:
            time_str = f"{years} год"
        elif 2 <= years <= 4:
            time_str = f"{years} года"
        else:
            time_str = f"{years} лет"
        if months > 0:
            if months == 1:
                time_str += f" {months} месяц"
            elif 2 <= months <= 4:
                time_str += f" {months} месяца"
            else:
                time_str += f" {months} месяцев"
        time_str += " назад"
    elif diff.days > 30:
        months = diff.days // 30
        if months == 1:
            time_str = f"{months} месяц назад"
        elif 2 <= months <= 4:
            time_str = f"{months} месяца назад"
        else:
            time_str = f"{months} месяцев назад"
        days = diff.days % 30
        if days > 0:
            if days == 1:
                time_str += f" {days} день"
            elif 2 <= days <= 4:
                time_str += f" {days} дня"
            else:
                time_str += f" {days} дней"
    elif diff.days > 0:
        if diff.days == 1:
            time_str = f"{diff.days} день назад"
        elif 2 <= diff.days <= 4:
            time_str = f"{diff.days} дня назад"
        else:
            time_str = f"{diff.days} дней назад"
    elif diff.seconds > 3600:
        hours = diff.seconds // 3600
        if hours == 1:
            time_str = f"{hours} час назад"
        elif 2 <= hours <= 4:
            time_str = f"{hours} часа назад"
        else:
            time_str = f"{hours} часов назад"
        minutes = (diff.seconds % 3600) // 60
        if minutes > 0:
            if minutes == 1:
                time_str += f" {minutes} минуту"
            elif 2 <= minutes <= 4:
                time_str += f" {minutes} минуты"
            else:
                time_str += f" {minutes} минут"
    elif diff.seconds > 60:
        minutes = diff.seconds // 60
        if minutes == 1:
            time_str = f"{minutes} минуту назад"
        elif 2 <= minutes <= 4:
            time_str = f"{minutes} минуты назад"
        else:
            time_str = f"{minutes} минут назад"
    else:
        time_str = "только что"
    
    return time_str

def get_reapply_warning(user_id):
    try:
        result = db_execute("""
            SELECT created_at, state, comment, answers 
            FROM application_history 
            WHERE user_id = ? AND state IN ('canceled', 'canceled_final')
            ORDER BY created_at DESC 
            LIMIT 1
        """, (user_id,), fetch=True)
        
        if not result:
            return None
        
        created_at_str, state, comment, answers_json = result
        if not created_at_str:
            return None
            
        try:
            created_at = datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S')
        except:
            try:
                created_at = datetime.strptime(created_at_str, '%Y-%m-%d %H:%M:%S.%f')
            except:
                return None
        
        now = datetime.now()
        diff = now - created_at
        
        time_str = format_time_diff(diff)
        old_age = get_age_from_answers(answers_json)
        
        warning_text = f"⚠️ ПРЕДУПРЕЖДЕНИЕ: пользователь повторно подает заявку (было {time_str})"
        warning_text += f"\nСтарый возраст: {old_age}"
        
        if comment:
            warning_text += f"\nПричина прошлого отказа: {comment[:100]}..."
        
        return warning_text
        
    except Exception as e:
        logger.error(f"Ошибка получения предупреждения: {e}")
        return None

def save_to_history(user_id, tag, state, comment=None, answers=None, reviewed_by=None):
    try:
        db_execute(
            """INSERT INTO application_history 
               (user_id, tag, state, comment, answers, reviewed_by, reviewed_at) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, tag, state, comment, answers, reviewed_by, get_moscow_time().strftime('%Y-%m-%d %H:%M:%S'))
        )
        logger.info(f"Сохранена история для {user_id}: {state}")
        return True
    except Exception as e:
        logger.error(f"Ошибка сохранения истории: {e}")
        return False

def get_invite_settings():
    try:
        result = db_execute(
            "SELECT chat_id, is_enabled, last_invite_link, last_generated_at, auto_approve FROM invite_settings LIMIT 1",
            fetch=True
        )
        if result:
            return {
                "chat_id": result[0],
                "is_enabled": bool(result[1]),
                "last_invite_link": result[2],
                "last_generated_at": result[3],
                "auto_approve": bool(result[4]) if len(result) > 4 else False
            }
    except Exception as e:
        logger.error(f"Ошибка получения настроек: {e}")
    return {"chat_id": None, "is_enabled": False, "last_invite_link": None, "last_generated_at": None, "auto_approve": False}

def set_invite_chat(chat_id, admin_id):
    try:
        db_execute(
            "UPDATE invite_settings SET chat_id = ?, updated_by = ?",
            (str(chat_id), admin_id)
        )
        logger.info(f"Админ {admin_id} установил чат для приглашений: {chat_id}")
        return True
    except Exception as e:
        logger.error(f"Ошибка установки чата: {e}")
        return False

def toggle_invite(enable: bool, admin_id):
    try:
        db_execute(
            "UPDATE invite_settings SET is_enabled = ?, updated_by = ?",
            (1 if enable else 0, admin_id)
        )
        logger.info(f"Админ {admin_id} {'включил' if enable else 'выключил'} генерацию ссылок")
        return True
    except Exception as e:
        logger.error(f"Ошибка переключения: {e}")
        return False

def toggle_auto_approve(enable: bool, admin_id):
    try:
        db_execute(
            "UPDATE invite_settings SET auto_approve = ?, updated_by = ?",
            (1 if enable else 0, admin_id)
        )
        logger.info(f"Админ {admin_id} {'включил' if enable else 'выключил'} авто-одобрение")
        return True
    except Exception as e:
        logger.error(f"Ошибка переключения авто-одобрения: {e}")
        return False

async def generate_invite_link_for_user(user_id: int) -> str:
    settings = get_invite_settings()
    
    if not settings["is_enabled"]:
        logger.warning("Генерация ссылок отключена")
        return None
    
    if not settings["chat_id"]:
        logger.warning("Чат для генерации ссылок не задан")
        return None
    
    try:
        chat_id = int(settings["chat_id"])
        
        invite_link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=settings["auto_approve"],
            name=f"Приглашение для пользователя {user_id}"
        )
        
        link = invite_link.invite_link
        
        db_execute(
            "UPDATE invite_settings SET last_invite_link = ?, last_generated_at = CURRENT_TIMESTAMP",
            (link,)
        )
        
        logger.info(f"Создана ссылка для пользователя {user_id}: {link}")
        return link
        
    except Exception as e:
        logger.error(f"Ошибка создания ссылки для {user_id}: {e}")
        return None

def get_last_invite_link():
    settings = get_invite_settings()
    return settings.get("last_invite_link")

def get_telegram_creation_date(user_id: int):
    points = [
        (100000000, datetime(2015, 9, 25)),
        (602102865, datetime(2018, 7, 18)),
        (623210951, datetime(2018, 8, 12)),
        (1000000000, datetime(2019, 11, 20)),
        (1084352018, datetime(2020, 2, 2)),
        (1301864145, datetime(2020, 7, 1)),
        (1488526742, datetime(2021, 6, 25)),
        (1588757700, datetime(2021, 2, 1)),
        (1812395528, datetime(2021, 6, 1)),
        (1925857232, datetime(2021, 7, 13)),
        (2000000000, datetime(2021, 11, 10)),
        (3000000000, datetime(2022, 2, 15)),
        (5257508402, datetime(2022, 3, 7)),
        (5333638741, datetime(2022, 5, 8)),
        (5421909799, datetime(2022, 7, 4)),
        (5479947797, datetime(2022, 7, 16)),
        (5844141112, datetime(2022, 12, 31)),
        (6000000000, datetime(2023, 1, 10)),
        (6050655492, datetime(2023, 4, 23)),
        (6294361700, datetime(2023, 7, 29)),
        (6332950457, datetime(2023, 7, 22)),
        (6760145635, datetime(2023, 12, 12)),
        (7000000000, datetime(2024, 1, 5)),
        (7194195424, datetime(2024, 7, 18)),
        (7222869205, datetime(2024, 8, 3)),
        (7827876802, datetime(2025, 2, 19)),
        (7967884371, datetime(2025, 3, 30)),
        (8477848337, datetime(2026, 10, 30)),
        (8527024370, datetime(2026, 11, 12)),
    ]
    
    points.sort(key=lambda x: x[0])
    
    if user_id <= points[0][0]:
        id1, date1 = points[0]
        id2, date2 = points[1]
    elif user_id >= points[-1][0]:
        id1, date1 = points[-2]
        id2, date2 = points[-1]
    else:
        for i in range(len(points) - 1):
            if points[i][0] <= user_id <= points[i+1][0]:
                id1, date1 = points[i]
                id2, date2 = points[i+1]
                break
    
    total_ids = id2 - id1
    total_seconds = (date2 - date1).total_seconds()
    
    if total_ids == 0:
        return date1
        
    ids_per_second = total_ids / total_seconds
    seconds_diff = (user_id - id1) / ids_per_second
    
    return date1 + timedelta(seconds=seconds_diff)

def format_months(days):
    months = days // 30
    if months == 0:
        return "менее месяца"
    elif months == 1:
        return "1 месяц"
    elif 2 <= months <= 4:
        return f"{months} месяца"
    else:
        return f"{months} месяцев"

async def get_account_age_and_warning(user_id: int):
    try:
        estimated_date = get_telegram_creation_date(user_id)
        now = datetime.now()
        days = (now - estimated_date).days
        years = days // 365
        
        if days < 0:
            days = 0
            years = 0
        
        if days < 365:
            months = format_months(days)
            warning = f"⚠️ Аккаунт создан ~{months} назад"
        else:
            if years == 1:
                warning = f"✅ Аккаунт создан {years} год назад"
            elif 2 <= years <= 4:
                warning = f"✅ Аккаунт создан {years} года назад"
            else:
                warning = f"✅ Аккаунт создан {years} лет назад"
        
        return warning
        
    except Exception as e:
        logger.error(f"Ошибка получения возраста аккаунта {user_id}: {e}")
        return None

def check_app_configuration():
    try:
        ensure_db_directory()
        conn = db_connect()
        cursor = conn.cursor()
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user INTEGER NOT NULL UNIQUE,
            tag TEXT,
            state TEXT DEFAULT 'created',
            comment TEXT,
            answers TEXT DEFAULT '{}'
        )''')
        
        cursor.execute("PRAGMA table_info(applications)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'created_at' not in columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN created_at TIMESTAMP")
            logger.info("Добавлена колонка created_at")
        
        if 'locked_by' not in columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN locked_by INTEGER DEFAULT NULL")
            logger.info("Добавлена колонка locked_by")
        
        if 'locked_at' not in columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN locked_at TIMESTAMP")
            logger.info("Добавлена колонка locked_at")
        
        if 'reviewed_by' not in columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN reviewed_by INTEGER DEFAULT NULL")
            logger.info("Добавлена колонка reviewed_by")
        
        if 'reviewed_at' not in columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN reviewed_at TIMESTAMP")
            logger.info("Добавлена колонка reviewed_at")
        
        if 'invite_link' not in columns:
            cursor.execute("ALTER TABLE applications ADD COLUMN invite_link TEXT")
            logger.info("Добавлена колонка invite_link")
        
        cursor.execute("UPDATE applications SET created_at = datetime('now') WHERE created_at IS NULL")
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS application_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tag TEXT,
            state TEXT,
            comment TEXT,
            answers TEXT,
            reviewed_by INTEGER,
            reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        cursor.execute("PRAGMA table_info(application_history)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'user_id' not in columns:
            cursor.execute("ALTER TABLE application_history ADD COLUMN user_id INTEGER")
        if 'tag' not in columns:
            cursor.execute("ALTER TABLE application_history ADD COLUMN tag TEXT")
        if 'state' not in columns:
            cursor.execute("ALTER TABLE application_history ADD COLUMN state TEXT")
        if 'comment' not in columns:
            cursor.execute("ALTER TABLE application_history ADD COLUMN comment TEXT")
        if 'answers' not in columns:
            cursor.execute("ALTER TABLE application_history ADD COLUMN answers TEXT")
        if 'reviewed_by' not in columns:
            cursor.execute("ALTER TABLE application_history ADD COLUMN reviewed_by INTEGER")
        if 'reviewed_at' not in columns:
            cursor.execute("ALTER TABLE application_history ADD COLUMN reviewed_at TIMESTAMP")
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_key TEXT NOT NULL UNIQUE,
            question_text TEXT NOT NULL,
            display_text TEXT NOT NULL,
            order_num INTEGER NOT NULL DEFAULT 0
        )''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS texts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text_key TEXT NOT NULL UNIQUE,
            text_content TEXT NOT NULL,
            parse_mode TEXT DEFAULT 'HTML',
            description TEXT
        )''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            tag TEXT
        )''')
        
        cursor.execute("PRAGMA table_info(admins)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'tag' not in columns:
            cursor.execute("ALTER TABLE admins ADD COLUMN tag TEXT")
            logger.info("Добавлена колонка tag в admins")
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            tag TEXT,
            reason TEXT,
            added_by INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS decline_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_text TEXT NOT NULL,
            added_by INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS invite_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            is_enabled INTEGER DEFAULT 0,
            last_invite_link TEXT,
            last_generated_at TIMESTAMP,
            updated_by INTEGER,
            auto_approve INTEGER DEFAULT 0
        )''')
        
        cursor.execute("PRAGMA table_info(invite_settings)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'chat_id' not in columns:
            cursor.execute("ALTER TABLE invite_settings ADD COLUMN chat_id TEXT")
        if 'is_enabled' not in columns:
            cursor.execute("ALTER TABLE invite_settings ADD COLUMN is_enabled INTEGER DEFAULT 0")
        if 'last_invite_link' not in columns:
            cursor.execute("ALTER TABLE invite_settings ADD COLUMN last_invite_link TEXT")
        if 'last_generated_at' not in columns:
            cursor.execute("ALTER TABLE invite_settings ADD COLUMN last_generated_at TIMESTAMP")
        if 'updated_by' not in columns:
            cursor.execute("ALTER TABLE invite_settings ADD COLUMN updated_by INTEGER")
        if 'auto_approve' not in columns:
            cursor.execute("ALTER TABLE invite_settings ADD COLUMN auto_approve INTEGER DEFAULT 0")
            logger.info("Добавлена колонка auto_approve")
        
        cursor.execute("SELECT COUNT(*) FROM invite_settings")
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO invite_settings (is_enabled, auto_approve) VALUES (0, 0)"
            )
            logger.info("Создана запись настроек приглашений")
        
        for main_admin in MAIN_ADMINS:
            cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (main_admin,))
        
        cursor.execute("SELECT COUNT(*) FROM questions")
        if cursor.fetchone()[0] == 0:
            default_questions = [
                ("age", "Сколько тебе лет?", "Возраст: {answer}", 1),
                ("plus", "Расскажи о своих плюсах", "Плюсы: {answer}", 2),
                ("time", "Количество времени, которое можешь играть", "Время игры: {answer}", 3),
                ("about", "Что-то от себя, если хочешь добавить", "О себе: {answer}", 4),
                ("rp", "Знаешь ли что такое РП и имел ли опыт в этом?", "РП опыт: {answer}", 5),
                ("nick", "Твой ник, через который ты будешь играть", "Ник: {answer}", 6)
            ]
            cursor.executemany(
                "INSERT INTO questions (question_key, question_text, display_text, order_num) VALUES (?, ?, ?, ?)",
                default_questions
            )
        
        cursor.execute("SELECT COUNT(*) FROM texts")
        if cursor.fetchone()[0] == 0:
            default_texts = [
                ("apply_text", 
                 "Ваша заявка одобрена!\n\nIP сервера: 5.83.140.206:25714\nСсылка для входа: {invite_link}\nhttps://discord.gg/AuKrCmJAaj\n\nСпасибо, что выбрали нас!",
                 "HTML", "Текст при одобрении заявки"),
                ("cancel_text",
                 "Здравствуйте!\n\nПричина отказа:\n{reason}\n\n{retry_text}\n\nС уважением,\nАдминистрация Lavender Park",
                 "HTML", "Текст при отказе"),
                ("welcome_text",
                 "Добро пожаловать, {name}!\n\nЯ — бот сервера Lavender Park.",
                 "HTML", "Приветственный текст"),
                ("admin_welcome_text",
                 "Здравствуйте, {name}!\n\nЗаявок на проверку: {count}",
                 "HTML", "Приветственный текст для админов"),
                ("server_info_text",
                 "Наши фишки:\n1. Уникальные моды\n2. Голосовой чат\n3. Последняя версия\n4. Дружное сообщество",
                 "HTML", "Информация о сервере"),
                ("already_applied_text",
                 "Вашу заявку уже одобрили",
                 "HTML", "Текст если заявка уже одобрена"),
                ("application_sent_text",
                 "Заявка отправлена!\nТы можешь отслеживать её состояние в 'Информации о заявке'",
                 "HTML", "Текст при отправке заявки"),
                ("not_all_fields_text",
                 "Кажется, ты заполнил не все критерии",
                 "HTML", "Текст если не все поля заполнены"),
                ("already_sent_text",
                 "Ты уже отправил заявку",
                 "HTML", "Текст если заявка уже отправлена"),
                ("final_cancel_text",
                 "Здравствуйте!\n\nПричина отказа:\n{reason}\n\nЭто окончательное решение администрации. Вы не можете подать заявку повторно.\n\nС уважением,\nАдминистрация Lavender Park",
                 "HTML", "Текст при окончательном отказе")
            ]
            cursor.executemany(
                "INSERT INTO texts (text_key, text_content, parse_mode, description) VALUES (?, ?, ?, ?)",
                default_texts
            )
        
        conn.commit()
        conn.close()
        load_admins()
        logger.info("База данных проверена и обновлена")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")

check_app_configuration()
backup_database()
logger.info("Проверка конфигурации бота завершена")

bot = Bot(token=token, session_timeout=30, request_timeout=30)
dp = Dispatcher(storage=MemoryStorage())

async def send_formatted_message(message: Message, text_key: str, reply_markup=None, **kwargs):
    if not is_private_chat(message):
        return
    text, parse_mode = get_text(text_key, **kwargs)
    try:
        if parse_mode and parse_mode.upper() in ["HTML", "MARKDOWN"]:
            await asyncio.wait_for(message.answer(text=text, parse_mode=parse_mode, reply_markup=reply_markup), timeout=10)
        else:
            await asyncio.wait_for(message.answer(text=text, reply_markup=reply_markup), timeout=10)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения {message.from_user.id}: {e}")
        try:
            await message.answer(text=re.sub(r'<[^>]+>', '', text), reply_markup=reply_markup)
        except:
            pass

async def send_formatted_message_by_id(user_id: int, text_key: str, **kwargs):
    text, parse_mode = get_text(text_key, **kwargs)
    try:
        if parse_mode and parse_mode.upper() in ["HTML", "MARKDOWN"]:
            await asyncio.wait_for(bot.send_message(user_id, text=text, parse_mode=parse_mode), timeout=10)
        else:
            await asyncio.wait_for(bot.send_message(user_id, text=text), timeout=10)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения {user_id}: {e}")
        try:
            await bot.send_message(user_id, text=re.sub(r'<[^>]+>', '', text))
        except:
            pass

APPROVED_GROUP_ID = -1003953845640
APPROVED_TOPIC_ID = 122708

async def send_approved_to_group(user_id: int, admin_id: int = None):
    try:
        result = db_execute("""
            SELECT tag, answers 
            FROM applications 
            WHERE user = ?
        """, (user_id,), fetch=True)
        
        if not result:
            logger.error(f"Заявка пользователя {user_id} не найдена")
            return
        
        tag, answers_json = result
        answers = json.loads(answers_json) if answers_json else {}
        questions = get_questions()
        
        user_link = format_user_link(user_id, tag)
        
        text = f"Пользователь: {user_link}\n"
        text += f"ID: {user_id}\n"
        
        account_warning = await get_account_age_and_warning(user_id)
        if account_warning:
            text += f"{account_warning}\n"
        
        if admin_id:
            admin_result = db_execute("SELECT tag FROM admins WHERE user_id = ?", (admin_id,), fetch=True)
            admin_tag = admin_result[0] if admin_result else None
            if admin_tag:
                text += f"Одобрил: @{escape_html(admin_tag)}\n"
            else:
                text += f"Одобрил: <code>{admin_id}</code>\n"
        
        text += f"Время: {get_moscow_time().strftime('%d.%m.%Y %H:%M')}\n\n"
        
        for q_key, q_text, display_text in questions:
            answer = answers.get(q_key, "Не заполнено")
            display = display_text.replace("{answer}", escape_html(str(answer)))
            text += f"{display}\n"
        
        try:
            await bot.send_message(
                chat_id=APPROVED_GROUP_ID,
                text=text,
                message_thread_id=APPROVED_TOPIC_ID,
                parse_mode="HTML",
                disable_notification=True
            )
            logger.info(f"Одобренная заявка {user_id} отправлена в группу")
        except Exception as e:
            logger.error(f"Ошибка отправки в группу: {e}")
        
    except Exception as e:
        logger.error(f"Ошибка отправки в группу: {e}")

@dp.chat_join_request()
async def handle_join_request(request: ChatJoinRequest):
    settings = get_invite_settings()
    
    if not settings["auto_approve"]:
        logger.info(f"Авто-одобрение отключено, запрос от {request.from_user.id} отклонён")
        await request.decline()
        return
    
    user_id = request.from_user.id
    chat_id = request.chat.id
    
    logger.info(f"Запрос на вступление от {user_id} в чат {chat_id}")
    
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in ["member", "administrator", "creator"]:
            await request.decline()
            logger.warning(f"❌ Пользователь {user_id} уже в группе")
            return
    except:
        pass
    
    result = db_execute("SELECT state FROM applications WHERE user = ?", (user_id,), fetch=True)
    
    if result and result[0] == "applied":
        try:
            await request.approve()
            logger.info(f"✅ Запрос пользователя {user_id} одобрен автоматически")
            
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🎉 Добро пожаловать! Пользователь {request.from_user.first_name} присоединился по одноразовой ссылке."
                )
            except:
                pass
        except Exception as e:
            logger.error(f"Ошибка одобрения запроса {user_id}: {e}")
    else:
        try:
            await request.decline()
            logger.warning(f"❌ Запрос пользователя {user_id} отклонён (не в списке одобренных)")
        except Exception as e:
            logger.error(f"Ошибка отклонения запроса {user_id}: {e}")

@dp.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    
    user_id = message.from_user.id
    username = message.from_user.username or "no_username"
    logger.info(f"Пользователь {user_id} (@{username}) использовал /start")
    await state.clear()
    
    if not is_admin(user_id):
        try:
            db_execute("INSERT OR IGNORE INTO applications (user, tag) VALUES (?, ?)", 
                      (user_id, username))
        except Exception as e:
            logger.error(f"Ошибка в start для {user_id}: {e}")

        keyboard = [
            [KeyboardButton(text="Подать заявку")],
            [KeyboardButton(text="О сервере")]
        ]

        try:
            result = db_execute("SELECT state FROM applications WHERE user = ?", (user_id,), fetch=True)
            if result:
                state_db = result[0]
                if state_db in ["sended", "applied", "canceled"]:
                    keyboard.append([KeyboardButton(text="Информация о заявке")])
                
                if state_db == "applied":
                    keyboard.append([KeyboardButton(text="🔗 Получить ссылку")])
        except:
            pass

        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        await send_formatted_message(message, "welcome_text", reply_markup=markup, name=message.from_user.first_name or "Пользователь")
    else:
        keyboard = [
            [KeyboardButton(text="Начать проверку")],
            [KeyboardButton(text="Отказанные заявки")],
            [KeyboardButton(text="Одобренные заявки")],
        ]
        
        if is_main_admin(user_id):
            keyboard.append([KeyboardButton(text="Управление вопросами")])
            keyboard.append([KeyboardButton(text="Управление текстами")])
            keyboard.append([KeyboardButton(text="Управление админами")])
            keyboard.append([KeyboardButton(text="Черный список")])
            keyboard.append([KeyboardButton(text="Шаблоны отказа")])
            keyboard.append([KeyboardButton(text="Настройка приглашений")])
        
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

        try:
            result = db_execute("SELECT COUNT(*) FROM applications WHERE state = ?", ("sended",), fetch=True)
            count = result[0] if result else 0
        except:
            count = 0

        await send_formatted_message(message, "admin_welcome_text", reply_markup=markup, name=message.from_user.first_name or "Админ", count=count)

@dp.message(Command("go"))
async def go_command(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    await start_application(message, state)

@dp.message(Command("info"))
async def info_command_handler(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    await info_command(message, state)

@dp.message(Command("get_chat_id"))
async def get_chat_id(message: Message):
    if not is_private_chat(message):
        return
    if not is_main_admin(message.from_user.id):
        return
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>\nThread ID: <code>{message.message_thread_id}</code>", parse_mode="HTML")

async def show_admin_management(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав для управления админами")
        return
    
    result = db_execute("SELECT user_id, tag FROM admins", fetchall=True)
    admin_list = result if result else []
    
    text = "Управление админами\n\n"
    if admin_list:
        text += "Список админов:\n"
        for uid, tag in admin_list:
            if uid in MAIN_ADMINS:
                text += f"- {uid} (@{tag}) (главный админ)\n"
            else:
                text += f"- {uid} (@{tag})\n"
    else:
        text += "Нет админов\n"
    
    keyboard = [
        [KeyboardButton(text="Добавить админа")],
        [KeyboardButton(text="Удалить админа")],
        [KeyboardButton(text="Назад в меню")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    await message.answer(text, reply_markup=markup)

async def handle_add_admin(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    await state.set_state(Form.waiting_for_new_admin)
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
    await message.answer("Введите ID пользователя, которого хотите добавить в админы:", reply_markup=markup)

async def process_add_admin(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await start_command(message, state)
        return
    
    try:
        user_id = int(message.text.strip())
        if user_id in MAIN_ADMINS:
            await message.answer("Это главный админ, он уже в списке")
            await start_command(message, state)
            return
        
        if add_admin(user_id):
            await message.answer(f"Пользователь {user_id} добавлен в админы")
            try:
                await bot.send_message(user_id, "Вас добавили в админы бота!")
            except:
                pass
        else:
            await message.answer(f"Пользователь {user_id} уже в админах")
    except ValueError:
        await message.answer("Неверный ID. Введите число.")
        return
    
    await state.clear()
    await start_command(message, state)

async def handle_remove_admin(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    result = db_execute("SELECT user_id, tag FROM admins", fetchall=True)
    admin_list = [row[0] for row in result] if result else []
    admin_list = [uid for uid in admin_list if uid not in MAIN_ADMINS]
    
    if not admin_list:
        await message.answer("Нет админов для удаления")
        await start_command(message, state)
        return
    
    await state.set_state(Form.waiting_for_remove_admin)
    
    keyboard = []
    for uid in admin_list:
        keyboard.append([KeyboardButton(text=str(uid))])
    keyboard.append([KeyboardButton(text="Отмена")])
    
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    await message.answer("Выберите ID админа для удаления:", reply_markup=markup)

async def process_remove_admin(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await start_command(message, state)
        return
    
    try:
        user_id = int(message.text.strip())
        if user_id in MAIN_ADMINS:
            await message.answer("Нельзя удалить главного админа")
            await start_command(message, state)
            return
        
        if remove_admin(user_id):
            await message.answer(f"Админ {user_id} удален")
            try:
                await bot.send_message(user_id, "Вас удалили из админов бота.")
            except:
                pass
        else:
            await message.answer(f"Админ {user_id} не найден")
    except ValueError:
        await message.answer("Неверный ID. Введите число.")
        return
    
    await state.clear()
    await start_command(message, state)

async def show_blacklist_menu(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    blacklist = get_blacklist()
    
    text = "Черный список\n\n"
    if blacklist:
        for user_id, tag, reason, added_at in blacklist[:10]:
            user_display = f"@{tag}" if tag else str(user_id)
            text += f"- {user_display} (ID: {user_id})\n"
            if reason:
                text += f"  Причина: {reason[:50]}{'...' if len(reason) > 50 else ''}\n"
            text += f"  Добавлен: {added_at}\n\n"
        if len(blacklist) > 10:
            text += f"и еще {len(blacklist) - 10} пользователей\n"
    else:
        text += "Черный список пуст\n"
    
    keyboard = [
        [KeyboardButton(text="Добавить в черный список")],
        [KeyboardButton(text="Удалить из черного списка")],
        [KeyboardButton(text="Найти в черном списке")],
        [KeyboardButton(text="Назад в меню")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

async def handle_blacklist_add(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    await state.set_state(Form.waiting_for_blacklist_add)
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
    await message.answer(
        "Введите @тег или ID пользователя для добавления в черный список:\n"
        "Можно добавить причину через пробел после ID/тега",
        reply_markup=markup
    )

async def process_blacklist_add(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await start_command(message, state)
        return
    
    admin_id = message.from_user.id
    text = message.text.strip()
    
    parts = text.split(maxsplit=1)
    user_input = parts[0]
    reason = parts[1] if len(parts) > 1 else "Без причины"
    
    try:
        if user_input.startswith("@"):
            tag = user_input[1:]
            result = db_execute("SELECT user, tag FROM applications WHERE tag = ?", (tag,), fetch=True)
            if not result:
                await message.answer(f"Пользователь с тегом @{tag} не найден в системе")
                return
            user_id, user_tag = result
        else:
            user_id = int(user_input)
            result = db_execute("SELECT tag FROM applications WHERE user = ?", (user_id,), fetch=True)
            user_tag = result[0] if result else None
        
        if is_in_blacklist(user_id):
            await message.answer(f"Пользователь уже в черном списке")
            return
        
        if add_to_blacklist(user_id, user_tag, reason, admin_id):
            user_display = f"@{user_tag}" if user_tag else str(user_id)
            await message.answer(f"Пользователь {user_display} добавлен в черный список\nПричина: {reason}")
            
            app_result = db_execute("SELECT state FROM applications WHERE user = ?", (user_id,), fetch=True)
            if app_result and app_result[0] == "sended":
                db_execute("UPDATE applications SET state = ?, comment = ? WHERE user = ?", 
                          ("canceled", f"Черный список: {reason}", user_id))
                try:
                    await bot.send_message(
                        user_id,
                        f"Ваша заявка отклонена. Причина: вы в черном списке.\n{reason}"
                    )
                except:
                    pass
                await message.answer(f"Заявка пользователя {user_display} автоматически отклонена")
        else:
            await message.answer("Ошибка при добавлении в черный список")
            
    except ValueError:
        await message.answer("Неверный формат. Введите @тег или ID пользователя")
    
    await state.clear()
    await show_blacklist_menu(message, state)

async def handle_blacklist_remove(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    blacklist = get_blacklist()
    if not blacklist:
        await message.answer("Черный список пуст")
        await show_blacklist_menu(message, state)
        return
    
    await state.set_state(Form.waiting_for_blacklist_remove)
    
    keyboard = []
    for user_id, tag, reason, added_at in blacklist:
        user_display = f"@{tag}" if tag else str(user_id)
        keyboard.append([KeyboardButton(text=f"{user_display} ({user_id})")])
    keyboard.append([KeyboardButton(text="Отмена")])
    
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    await message.answer("Выберите пользователя для удаления из черного списка:", reply_markup=markup)

async def process_blacklist_remove(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await start_command(message, state)
        return
    
    import re
    match = re.search(r'\((\d+)\)', message.text)
    if not match:
        await message.answer("Не удалось определить пользователя")
        return
    
    user_id = int(match.group(1))
    
    if remove_from_blacklist(user_id):
        await message.answer(f"Пользователь {user_id} удален из черного списка")
    else:
        await message.answer("Ошибка при удалении из черного списка")
    
    await state.clear()
    await show_blacklist_menu(message, state)

async def search_blacklist(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    await state.set_state(Form.waiting_for_blacklist_search)
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Назад в меню")]], resize_keyboard=True)
    await message.answer(
        "Введите @тег или ID пользователя для поиска в черном списке:",
        reply_markup=markup
    )

async def process_blacklist_search(message: Message, state: FSMContext):
    if message.text == "Назад в меню":
        await start_command(message, state)
        return
    
    search = message.text.strip()
    
    try:
        if search.startswith("@"):
            tag = search[1:]
            result = db_execute("""
                SELECT user_id, tag, reason, added_by, added_at
                FROM blacklist 
                WHERE tag = ?
                ORDER BY added_at DESC
            """, (tag,), fetch=True)
        else:
            try:
                user_id = int(search)
                result = db_execute("""
                    SELECT user_id, tag, reason, added_by, added_at
                    FROM blacklist 
                    WHERE user_id = ?
                """, (user_id,), fetch=True)
            except ValueError:
                await message.answer("Неверный формат. Введите @тег или ID пользователя.")
                return
        
        if not result:
            await message.answer(
                f"Пользователь {search} не найден в черном списке",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="Назад в меню")]],
                    resize_keyboard=True
                )
            )
            return
        
        user_id, tag, reason, added_by, added_at = result
        user_display = f"@{tag}" if tag else str(user_id)
        
        text = f"Пользователь в черном списке\n\n"
        text += f"Пользователь: {user_display}\n"
        text += f"ID: <code>{user_id}</code>\n"
        if reason:
            text += f"Причина: {escape_html(reason)}\n"
        if added_by:
            admin_result = db_execute("SELECT tag FROM admins WHERE user_id = ?", (added_by,), fetch=True)
            admin_tag = admin_result[0] if admin_result else None
            if admin_tag:
                text += f"Добавил: @{escape_html(admin_tag)}\n"
            else:
                text += f"Добавил: <code>{added_by}</code>\n"
        if added_at:
            text += f"Дата: {added_at}\n"
        
        keyboard = [
            [KeyboardButton(text=f"Удалить из черного списка {user_id}")],
            [KeyboardButton(text="Назад в меню")]
        ]
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка поиска в черном списке: {e}")
        await message.answer("Произошла ошибка при поиске")

async def show_templates_menu(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    templates = get_decline_templates()
    
    text = "Шаблоны для отказа\n\n"
    if templates:
        for tid, template_text, added_by, added_at in templates:
            admin_result = db_execute("SELECT tag FROM admins WHERE user_id = ?", (added_by,), fetch=True)
            admin_tag = admin_result[0] if admin_result else None
            admin_display = f"@{admin_tag}" if admin_tag else str(added_by)
            text += f"ID: {tid}\n"
            text += f"Текст: {template_text}\n"
            text += f"Добавил: {admin_display}\n"
            text += f"Дата: {added_at}\n\n"
    else:
        text += "Шаблонов нет\n"
    
    keyboard = [
        [KeyboardButton(text="Добавить шаблон")],
        [KeyboardButton(text="Удалить шаблон")],
        [KeyboardButton(text="Назад в меню")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

async def handle_template_add(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    await state.set_state(Form.waiting_for_template_add)
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
    await message.answer(
        "Введите текст шаблона для отказа:",
        reply_markup=markup
    )

async def process_template_add(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await start_command(message, state)
        return
    
    admin_id = message.from_user.id
    template_text = message.text.strip()
    
    if add_decline_template(template_text, admin_id):
        await message.answer(f"Шаблон добавлен:\n{template_text}")
    else:
        await message.answer("Ошибка при добавлении шаблона")
    
    await state.clear()
    await show_templates_menu(message, state)

async def handle_template_delete(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    templates = get_decline_templates()
    if not templates:
        await message.answer("Нет шаблонов для удаления")
        await show_templates_menu(message, state)
        return
    
    await state.set_state(Form.waiting_for_template_delete)
    
    keyboard = []
    for tid, template_text, added_by, added_at in templates:
        preview = template_text[:30] + "..." if len(template_text) > 30 else template_text
        keyboard.append([KeyboardButton(text=f"{tid}: {preview}")])
    keyboard.append([KeyboardButton(text="Отмена")])
    
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    await message.answer(
        "Выберите шаблон для удаления (нажмите на ID):",
        reply_markup=markup
    )

async def process_template_delete(message: Message, state: FSMContext):
    if message.text == "Отмена":
        await start_command(message, state)
        return
    
    try:
        tid = int(message.text.split(":")[0])
        if delete_decline_template(tid):
            await message.answer(f"Шаблон {tid} удален")
        else:
            await message.answer("Ошибка при удалении шаблона")
    except:
        await message.answer("Неверный формат. Нажмите на кнопку с ID шаблона")
    
    await state.clear()
    await show_templates_menu(message, state)

async def show_rejected_applications(message: Message, state: FSMContext):
    try:
        user_id = message.from_user.id
        logger.info(f"Админ {user_id} запросил поиск отклоненных заявок")
        
        await state.set_state(Form.waiting_for_rejected_search)
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Назад в меню")]], resize_keyboard=True)
        await message.answer(
            "Введите тег или ID пользователя для поиска отклоненной заявки:",
            reply_markup=markup
        )
        
    except Exception as e:
        logger.error(f"Ошибка поиска отклоненных заявок: {e}")
        await message.answer("Произошла ошибка")

async def show_applied_applications(message: Message, state: FSMContext):
    try:
        user_id = message.from_user.id
        logger.info(f"Админ {user_id} запросил поиск одобренных заявок")
        
        await state.set_state(Form.waiting_for_applied_search)
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Назад в меню")]], resize_keyboard=True)
        await message.answer(
            "Введите тег, ID пользователя или ник для поиска одобренной заявки:",
            reply_markup=markup
        )
        
    except Exception as e:
        logger.error(f"Ошибка поиска одобренных заявок: {e}")
        await message.answer("Произошла ошибка")

async def process_rejected_search(message: Message, state: FSMContext):
    if message.text == "Назад в меню":
        await start_command(message, state)
        return
    
    search = message.text.strip()
    
    try:
        if search.startswith("@"):
            tag = search[1:]
            result = db_execute("""
                SELECT user, tag, comment, answers, reviewed_by, reviewed_at
                FROM applications 
                WHERE state = 'canceled' AND tag = ?
                ORDER BY id DESC
            """, (tag,), fetch=True)
        else:
            try:
                user_id = int(search)
                result = db_execute("""
                    SELECT user, tag, comment, answers, reviewed_by, reviewed_at
                    FROM applications 
                    WHERE state = 'canceled' AND user = ?
                """, (user_id,), fetch=True)
            except ValueError:
                await message.answer("Неверный формат. Введите @тег или ID пользователя.")
                return
        
        if not result:
            await message.answer(
                f"Заявок не найдено для {search}",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="Назад в меню")]],
                    resize_keyboard=True
                )
            )
            return
        
        uid, tag, comment, answers_json, reviewed_by, reviewed_at = result
        user_display = format_user_link(uid, tag)
        
        text = f"Отказанная заявка\n\n"
        text += f"Пользователь: {user_display}\n"
        text += f"ID: <code>{uid}</code>\n"
        if comment:
            text += f"Причина: {escape_html(comment)}\n"
        if reviewed_by:
            admin_result = db_execute("SELECT tag FROM admins WHERE user_id = ?", (reviewed_by,), fetch=True)
            admin_tag = admin_result[0] if admin_result else None
            if admin_tag:
                text += f"Отклонил: @{escape_html(admin_tag)}\n"
            else:
                text += f"Отклонил: <code>{reviewed_by}</code>\n"
        if reviewed_at:
            text += f"Время: {reviewed_at}\n"
        
        text += "\n"
        try:
            answers = json.loads(answers_json) if answers_json else {}
            questions = get_questions()
            for q_key, q_text, display_text in questions:
                answer = answers.get(q_key, "Не заполнено")
                display = display_text.replace("{answer}", escape_html(str(answer)))
                text += f"{display}\n"
        except:
            text += "Нет данных\n"
        
        keyboard = [
            [KeyboardButton(text=f"+ Дать попытку {uid}")],
            [KeyboardButton(text="Назад в меню")]
        ]
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        await message.answer("Произошла ошибка при поиске")

async def process_applied_search(message: Message, state: FSMContext):
    if message.text == "Назад в меню":
        await start_command(message, state)
        return
    
    search = message.text.strip()
    
    try:
        if search.startswith("@"):
            tag = search[1:]
            result = db_execute("""
                SELECT user, tag, answers, reviewed_by, reviewed_at
                FROM applications 
                WHERE state = 'applied' AND tag = ?
                ORDER BY id DESC
            """, (tag,), fetch=True)
        else:
            try:
                user_id = int(search)
                result = db_execute("""
                    SELECT user, tag, answers, reviewed_by, reviewed_at
                    FROM applications 
                    WHERE state = 'applied' AND user = ?
                """, (user_id,), fetch=True)
            except ValueError:
                result = db_execute("""
                    SELECT user, tag, answers, reviewed_by, reviewed_at
                    FROM applications 
                    WHERE state = 'applied' AND answers LIKE ?
                    ORDER BY id DESC
                """, (f'%{search}%',), fetch=True)
        
        if not result:
            await message.answer(
                f"Заявок не найдено для {search}",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[[KeyboardButton(text="Назад в меню")]],
                    resize_keyboard=True
                )
            )
            return
        
        uid, tag, answers_json, reviewed_by, reviewed_at = result
        user_display = format_user_link(uid, tag)
        
        text = f"Одобренная заявка\n\n"
        text += f"Пользователь: {user_display}\n"
        text += f"ID: <code>{uid}</code>\n"
        if reviewed_by:
            admin_result = db_execute("SELECT tag FROM admins WHERE user_id = ?", (reviewed_by,), fetch=True)
            admin_tag = admin_result[0] if admin_result else None
            if admin_tag:
                text += f"Одобрил: @{escape_html(admin_tag)}\n"
            else:
                text += f"Одобрил: <code>{reviewed_by}</code>\n"
        if reviewed_at:
            text += f"Время: {reviewed_at}\n"
        
        text += "\n"
        try:
            answers = json.loads(answers_json) if answers_json else {}
            questions = get_questions()
            for q_key, q_text, display_text in questions:
                answer = answers.get(q_key, "Не заполнено")
                display = display_text.replace("{answer}", escape_html(str(answer)))
                text += f"{display}\n"
        except:
            text += "Нет данных\n"
        
        keyboard = [
            [KeyboardButton(text="Назад в меню")]
        ]
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        
        await message.answer(text, reply_markup=markup, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Ошибка поиска одобренных: {e}")
        await message.answer("Произошла ошибка при поиске")

async def add_extra_attempt_from_button(message: Message, button_text: str):
    try:
        parts = button_text.split()
        if len(parts) < 3:
            await message.answer("Не удалось определить пользователя")
            return
        
        uid = int(parts[2])
        admin_id = message.from_user.id
        
        result = db_execute("SELECT state, tag, comment, answers FROM applications WHERE user = ?", (uid,), fetch=True)
        if not result:
            await message.answer(f"Пользователь с ID {uid} не найден")
            return
        
        state, tag, comment, answers = result
        
        if state != "canceled":
            await message.answer(f"Заявка пользователя {uid} не отклонена")
            return
        
        save_to_history(uid, tag, "canceled", comment, answers, admin_id)
        
        db_execute("UPDATE applications SET state = ? WHERE user = ?", ("created", uid))
        
        logger.info(f"Админ {admin_id} добавил попытку пользователю {uid}")
        await message.answer(f"Пользователю {uid} добавлена дополнительная попытка\nСтатус заявки сброшен на 'created'")
        
        try:
            await bot.send_message(
                uid,
                f"Администратор добавил вам дополнительную попытку подачи заявки!\n"
                f"Вы можете подать заявку заново, нажав 'Подать заявку' в главном меню."
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {uid}: {e}")
        
        await start_command(message, state)
        
    except ValueError:
        await message.answer("Неверный формат ID пользователя")
    except Exception as e:
        logger.error(f"Ошибка добавления попытки: {e}")
        await message.answer("Произошла ошибка при добавлении попытки")

async def start_application(message: Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"Пользователь {user_id} начал заполнение заявки")
    
    if is_in_blacklist(user_id):
        await message.answer(
            "Вы находитесь в черном списке и не можете подать заявку.\n"
            "Если считаете это ошибкой, обратитесь к администратору."
        )
        return
    
    try:
        db_execute("INSERT OR IGNORE INTO applications (user, tag) VALUES (?, ?)", 
                  (user_id, message.from_user.username or None))
    except Exception as e:
        logger.error(f"Ошибка вставки заявки: {e}")

    try:
        result = db_execute("SELECT state FROM applications WHERE user = ?", (user_id,), fetch=True)
        if result:
            state_db = result[0]
            if state_db == "applied":
                await send_formatted_message(message, "already_applied_text")
                await start_command(message, state)
                return
            elif state_db == "canceled":
                await message.answer(
                    "Ваша заявка была окончательно отклонена. Вы не можете подать заявку повторно.",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[[KeyboardButton(text="Назад в меню")]],
                        resize_keyboard=True
                    )
                )
                return
    except Exception as e:
        logger.error(f"Ошибка проверки состояния заявки: {e}")

    questions = get_questions()
    answers = get_application_answers(user_id)
    
    if not questions:
        await message.answer("Нет доступных вопросов для заявки")
        return
    
    start_index = 0
    for i, (q_key, _, _) in enumerate(questions):
        if q_key not in answers or not answers[q_key]:
            start_index = i
            break
    
    await state.update_data(application_mode=True, current_question_index=start_index, total_questions=len(questions))
    await state.set_state(Form.answering_question)
    
    await show_question(message, state, "current")

async def show_preview(message: Message, state: FSMContext):
    user_id = message.from_user.id
    answers = get_application_answers(user_id)
    questions = get_questions()
    
    data = await state.get_data()
    current_index = data.get("current_question_index", 0)
    await state.update_data(preview_from_index=current_index)
    
    preview_parts = ["Предпросмотр заявки\n"]
    
    for i, (q_key, q_text, display_text) in enumerate(questions, 1):
        answer = answers.get(q_key, "Не заполнено")
        display = display_text.replace("{answer}", escape_html(str(answer)))
        preview_parts.append(f"{i}. {display}")
    
    preview_parts.append("\nПроверьте данные перед отправкой")
    
    keyboard = [
        [KeyboardButton(text="Назад к вопросам")],
        [KeyboardButton(text="Отправить заявку")],
        [KeyboardButton(text="Отменить заявку")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    await message.answer(text="\n".join(preview_parts), reply_markup=markup, parse_mode="HTML")

async def show_question(message: Message, state: FSMContext, direction: str = "next"):
    user_id = message.from_user.id
    data = await state.get_data()
    questions = get_questions()
    answers = get_application_answers(user_id)
    
    current_index = data.get("current_question_index", 0)
    total = len(questions)
    
    if direction == "next":
        current_index += 1
    elif direction == "prev":
        current_index -= 1
    elif direction == "current":
        pass
    elif direction == "restore":
        current_index = data.get("preview_from_index", 0)
    
    if current_index < 0:
        current_index = 0
    if current_index >= total:
        await show_preview(message, state)
        return
    
    await state.update_data(current_question_index=current_index)
    
    q_key, q_text, _ = questions[current_index]
    current_answer = answers.get(q_key, "")
    
    keyboard = []
    nav_row = []
    
    if current_index > 0:
        nav_row.append(KeyboardButton(text="Назад"))
    if current_answer:
        nav_row.append(KeyboardButton(text="Пропустить"))
    if nav_row:
        keyboard.append(nav_row)
    
    keyboard.append([KeyboardButton(text="Предпросмотр")])
    keyboard.append([KeyboardButton(text="Отменить заявку")])
    
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    question_text = f"Вопрос {current_index + 1} из {total}\n\n{q_text}"
    if current_answer:
        question_text += f"\n\nТекущий ответ: {escape_html(str(current_answer))}"
    
    await message.answer(text=question_text, reply_markup=markup, parse_mode="HTML")

async def save_answer_and_continue(message: Message, state: FSMContext, answer: str):
    user_id = message.from_user.id
    data = await state.get_data()
    questions = get_questions()
    current_index = data.get("current_question_index", 0)
    
    q_key, _, _ = questions[current_index]
    answers = get_application_answers(user_id)
    answers[q_key] = answer
    
    try:
        db_execute("UPDATE applications SET answers = ? WHERE user = ?", (json.dumps(answers, ensure_ascii=False), user_id))
        logger.info(f"Пользователь {user_id} сохранил ответ на вопрос {q_key}")
    except Exception as e:
        logger.error(f"Ошибка сохранения ответа: {e}")
        await message.answer("Ошибка при сохранении ответа")
        return
    
    await message.answer(text="Ответ сохранен")
    await show_question(message, state, "next")

async def submit_application(message: Message, state: FSMContext):
    user_id = message.from_user.id
    try:
        answers = get_application_answers(user_id)
        questions = get_questions()
        
        all_answered = True
        for q_key, _, _ in questions:
            if q_key not in answers or not answers[q_key]:
                all_answered = False
                break
        
        if not all_answered:
            await send_formatted_message(message, "not_all_fields_text")
            return
        
        result = db_execute("SELECT state FROM applications WHERE user = ?", (user_id,), fetch=True)
        if result and result[0] == "sended":
            await send_formatted_message(message, "already_sent_text")
            return
        elif result and result[0] == "canceled":
            await message.answer("Ваша заявка была окончательно отклонена")
            return
        
        db_execute("UPDATE applications SET state = ? WHERE user = ?", ("sended", user_id))
        
        logger.info(f"Пользователь {user_id} отправил заявку")
        await send_formatted_message(message, "application_sent_text")
        await start_command(message, state)
        
        for admin in admins:
            try:
                await bot.send_message(admin, text="У вас новая заявка!")
            except:
                pass
    except Exception as e:
        logger.error(f"Ошибка отправки заявки: {e}")
        await message.answer(text="Произошла ошибка при отправке заявки")

async def check_applications(message: Message, state: FSMContext):
    admin_id = message.from_user.id
    try:
        result = db_execute("SELECT * FROM applications WHERE state = 'sended' ORDER BY id", fetchall=True)
        if not result or len(result) < 1:
            await message.answer(text="У вас нет заявок")
            await start_command(message, state)
            return

        app_data = None
        for app in result:
            user_id = app[1]
            locked_by = is_application_locked(user_id)
            if locked_by is None:
                lock_application(user_id, admin_id)
                app_data = app
                break
            elif locked_by == admin_id:
                app_data = app
                break
        
        if app_data is None:
            await message.answer("Все заявки уже просматривают другие админы")
            await start_command(message, state)
            return

        app_id = app_data[0]
        user_id = app_data[1]
        tag = app_data[2] or "Нет тега"
        answers_json = app_data[5] if len(app_data) > 5 else "{}"
        
        await state.update_data(check_user_id=user_id)
        
        if user_id in auto_unlock_tasks:
            auto_unlock_tasks[user_id].cancel()
        task = asyncio.create_task(auto_unlock_application(user_id, admin_id, message, state))
        auto_unlock_tasks[user_id] = task
        
        keyboard = [
            [KeyboardButton(text="Одобрить")],
            [KeyboardButton(text="Отклонить")],
            [KeyboardButton(text="Следующая")],
            [KeyboardButton(text="Отмена")]
        ]
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        
        try:
            answers = json.loads(answers_json) if answers_json else {}
        except:
            answers = {}
            
        questions = get_questions()
        
        user_link = format_user_link(user_id, tag)
        
        account_warning = await get_account_age_and_warning(user_id)
        
        check_text_parts = [
            f"Заявка",
            f"ID: <code>{app_id}</code>",
            f"Пользователь: {user_link}",
            f"User ID: <code>{user_id}</code>",
        ]
        
        if account_warning:
            check_text_parts.append(f"{account_warning}")
        
        warning = get_reapply_warning(user_id)
        if warning:
            check_text_parts.append(f"\n{warning}")
        
        for i, (q_key, q_text, display_text) in enumerate(questions, 1):
            answer = answers.get(q_key, "Не заполнено")
            safe_answer = escape_html(str(answer))
            display = display_text.replace("{answer}", safe_answer)
            check_text_parts.append(display)
        
        check_text_parts.append("\nУ вас есть 5 минут на рассмотрение")
        
        await message.answer(text="\n\n".join(check_text_parts), reply_markup=markup, parse_mode="HTML")
        logger.info(f"Админ {admin_id} начал проверку заявки {user_id}")
    except Exception as e:
        logger.error(f"Ошибка проверки: {e}")
        await message.answer(text="Произошла ошибка при проверке заявок")

async def show_next_application(message: Message, state: FSMContext):
    admin_id = message.from_user.id
    try:
        current_user_id = (await state.get_data()).get("check_user_id")
        if current_user_id:
            if current_user_id in auto_unlock_tasks:
                auto_unlock_tasks[current_user_id].cancel()
                del auto_unlock_tasks[current_user_id]
            unlock_application(current_user_id)
        
        result = db_execute("SELECT * FROM applications WHERE state = 'sended' ORDER BY id", fetchall=True)
        if not result:
            await message.answer(text="У вас нет заявок")
            await start_command(message, state)
            return

        app_data = None
        for app in result:
            user_id = app[1]
            locked_by = is_application_locked(user_id)
            if locked_by is None:
                lock_application(user_id, admin_id)
                app_data = app
                break
            elif locked_by == admin_id:
                app_data = app
                break
        
        if app_data is None:
            await message.answer("Все заявки уже просматривают другие админы")
            await start_command(message, state)
            return

        app_id = app_data[0]
        user_id = app_data[1]
        tag = app_data[2] or "Нет тега"
        answers_json = app_data[5] if len(app_data) > 5 else "{}"
        
        await state.update_data(check_user_id=user_id)
        
        if user_id in auto_unlock_tasks:
            auto_unlock_tasks[user_id].cancel()
        task = asyncio.create_task(auto_unlock_application(user_id, admin_id, message, state))
        auto_unlock_tasks[user_id] = task
        
        keyboard = [
            [KeyboardButton(text="Одобрить")],
            [KeyboardButton(text="Отклонить")],
            [KeyboardButton(text="Следующая")],
            [KeyboardButton(text="Отмена")]
        ]
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        
        try:
            answers = json.loads(answers_json) if answers_json else {}
        except:
            answers = {}
            
        questions = get_questions()
        
        user_link = format_user_link(user_id, tag)
        
        account_warning = await get_account_age_and_warning(user_id)
        
        app_index = 1
        for i, app in enumerate(result):
            if app[0] == app_id:
                app_index = i + 1
                break
        
        check_text_parts = [
            f"Заявка {app_index} из {len(result)}",
            f"ID: <code>{app_id}</code>",
            f"Пользователь: {user_link}",
            f"User ID: <code>{user_id}</code>",
        ]
        
        if account_warning:
            check_text_parts.append(f"{account_warning}")
        
        warning = get_reapply_warning(user_id)
        if warning:
            check_text_parts.append(f"\n{warning}")
        
        for i, (q_key, q_text, display_text) in enumerate(questions, 1):
            answer = answers.get(q_key, "Не заполнено")
            safe_answer = escape_html(str(answer))
            display = display_text.replace("{answer}", safe_answer)
            check_text_parts.append(display)
        
        check_text_parts.append("\nУ вас есть 5 минут на рассмотрение")
        
        await message.answer(text="\n\n".join(check_text_parts), reply_markup=markup, parse_mode="HTML")
        logger.info(f"Админ {admin_id} перешел к следующей заявке")
    except Exception as e:
        logger.error(f"Ошибка показа следующей заявки: {e}")
        await message.answer(text="Произошла ошибка")

async def decline_application_with_choice(message: Message, state: FSMContext, user_id: int):
    admin_id = message.from_user.id
    try:
        await state.update_data(decline_user_id=user_id)
        await state.set_state(Form.waiting_for_decline_choice)
        
        keyboard = [
            [KeyboardButton(text="Дать еще попытку")],
            [KeyboardButton(text="Окончательный отказ")],
            [KeyboardButton(text="Отмена")]
        ]
        
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        
        await message.answer(
            "Выберите действие после отказа:",
            reply_markup=markup
        )
        logger.info(f"Админ {admin_id} начал отклонение заявки {user_id}")
        
    except Exception as e:
        logger.error(f"Ошибка выбора после отказа: {e}")
        await message.answer("Произошла ошибка")

async def process_decline_choice(message: Message, state: FSMContext):
    admin_id = message.from_user.id
    data = await state.get_data()
    user_id = data.get("decline_user_id")
    
    if not user_id:
        await message.answer("Ошибка: пользователь не найден")
        await start_command(message, state)
        return
    
    result = db_execute("SELECT comment, tag, answers FROM applications WHERE user = ?", (user_id,), fetch=True)
    comment = result[0] if result else "Без причины"
    tag = result[1] if result and result[1] else None
    answers = result[2] if result and result[2] else "{}"
    
    if user_id in auto_unlock_tasks:
        auto_unlock_tasks[user_id].cancel()
        del auto_unlock_tasks[user_id]
    
    if message.text == "Дать еще попытку":
        save_to_history(user_id, tag, "canceled", comment, answers, admin_id)
        
        db_execute("UPDATE applications SET state = ? WHERE user = ?", ("created", user_id))
        unlock_application(user_id)
        
        try:
            await send_formatted_message_by_id(
                user_id, 
                "cancel_text", 
                reason=escape_html(comment),
                retry_text="Вы можете подать заявку повторно, нажав 'Подать заявку' в главном меню."
            )
        except:
            pass
        
        user_link = format_user_link(user_id, tag)
        await message.answer(f"Заявка {user_link} отклонена. Пользователь может подать заявку повторно.", parse_mode="HTML")
        logger.info(f"Админ {admin_id} отклонил заявку {user_id} с возможностью повторной подачи")
        
        await notify_admins_about_review(message, user_id, admin_id, "отклонил")
        
        result = db_execute("SELECT * FROM applications WHERE state = 'sended' ORDER BY id", fetchall=True)
        if result and len(result) > 0:
            await check_applications(message, state)
        else:
            await start_command(message, state)
        
    elif message.text == "Окончательный отказ":
        save_to_history(user_id, tag, "canceled_final", comment, answers, admin_id)
        
        db_execute("UPDATE applications SET state = ? WHERE user = ?", ("canceled", user_id))
        unlock_application(user_id)
        
        now = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
        db_execute("UPDATE applications SET reviewed_by = ?, reviewed_at = ? WHERE user = ?", 
                   (admin_id, now, user_id))
        
        try:
            await send_formatted_message_by_id(
                user_id, 
                "final_cancel_text", 
                reason=escape_html(comment)
            )
        except:
            pass
        
        user_link = format_user_link(user_id, tag)
        await message.answer(f"Заявка {user_link} окончательно отклонена. Пользователь не может подать заявку повторно.", parse_mode="HTML")
        logger.info(f"Админ {admin_id} окончательно отклонил заявку {user_id}")
        
        await notify_admins_about_review(message, user_id, admin_id, "окончательно отклонил")
        
        result = db_execute("SELECT * FROM applications WHERE state = 'sended' ORDER BY id", fetchall=True)
        if result and len(result) > 0:
            await check_applications(message, state)
        else:
            await start_command(message, state)
        
    elif message.text == "Отмена":
        unlock_application(user_id)
        await start_command(message, state)
    else:
        await message.answer("Пожалуйста, выберите один из вариантов")

async def approve_application(message: Message, state: FSMContext, user_id: int):
    admin_id = message.from_user.id
    try:
        locked_by = is_application_locked(user_id)
        if locked_by is not None and locked_by != admin_id:
            await message.answer("Эта заявка уже обрабатывается другим админом")
            return
        
        if user_id in auto_unlock_tasks:
            auto_unlock_tasks[user_id].cancel()
            del auto_unlock_tasks[user_id]
        
        db_execute("UPDATE applications SET state = ? WHERE user = ?", ("applied", user_id))
        unlock_application(user_id)
        
        now = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
        db_execute("UPDATE applications SET reviewed_by = ?, reviewed_at = ? WHERE user = ?", 
                   (admin_id, now, user_id))
        
        result = db_execute("SELECT tag FROM applications WHERE user = ?", (user_id,), fetch=True)
        tag = result[0] if result and result[0] else None
        user_link = format_user_link(user_id, tag)
        
        await message.answer(text=f"Вы одобрили заявку {user_link}", parse_mode="HTML")
        logger.info(f"Админ {admin_id} одобрил заявку {user_id}")
        
        invite_link = await generate_invite_link_for_user(user_id)
        
        if invite_link:
            db_execute("UPDATE applications SET invite_link = ? WHERE user = ?", (invite_link, user_id))
            try:
                await send_formatted_message_by_id(
                    user_id, 
                    "apply_text", 
                    invite_link=invite_link
                )
                logger.info(f"Пользователю {user_id} отправлена ссылка: {invite_link}")
            except Exception as e:
                logger.error(f"Ошибка отправки с ссылкой: {e}")
                await send_formatted_message_by_id(user_id, "apply_text")
        else:
            await send_formatted_message_by_id(user_id, "apply_text")
        
        await send_approved_to_group(user_id, admin_id)
        await notify_admins_about_review(message, user_id, admin_id, "одобрил")
        
        result = db_execute("SELECT * FROM applications WHERE state = 'sended' ORDER BY id", fetchall=True)
        if result and len(result) > 0:
            await check_applications(message, state)
        else:
            await start_command(message, state)
    except Exception as e:
        logger.error(f"Ошибка одобрения заявки: {e}")

async def decline_application(message: Message, state: FSMContext, user_id: int, reason: str):
    admin_id = message.from_user.id
    try:
        locked_by = is_application_locked(user_id)
        if locked_by is not None and locked_by != admin_id:
            await message.answer("Эта заявка уже обрабатывается другим админом")
            return
        
        db_execute("UPDATE applications SET state = ?, comment = ? WHERE user = ?", 
                  ("canceled", reason, user_id))
        
        result = db_execute("SELECT tag FROM applications WHERE user = ?", (user_id,), fetch=True)
        tag = result[0] if result and result[0] else None
        user_link = format_user_link(user_id, tag)
        
        await message.answer(text=f"Вы отклонили заявку {user_link} по причине:\n{reason}", parse_mode="HTML")
        logger.info(f"Админ {admin_id} отклонил заявку {user_id}, причина: {reason}")
        
        await decline_application_with_choice(message, state, user_id)
        
    except Exception as e:
        logger.error(f"Ошибка отклонения заявки: {e}")

async def manage_questions_menu(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
    
    admin_id = message.from_user.id
    logger.info(f"Главный админ {admin_id} открыл меню управления вопросами")
    await state.clear()
    await state.set_state(Form.waiting_for_question_key)
    
    keyboard = [
        [KeyboardButton(text="Добавить вопрос")],
        [KeyboardButton(text="Редактировать вопрос")],
        [KeyboardButton(text="Удалить вопрос")],
        [KeyboardButton(text="Список вопросов")],
        [KeyboardButton(text="Назад в меню")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    try:
        result = db_execute("SELECT COUNT(*) FROM questions", fetch=True)
        count = result[0] if result else 0
    except:
        count = 0
    
    await message.answer(text=f"Управление вопросами\n\nВсего вопросов: {count}", reply_markup=markup, parse_mode="HTML")

async def manage_texts_menu(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять текстами")
        return
    
    admin_id = message.from_user.id
    logger.info(f"Главный админ {admin_id} открыл меню управления текстами")
    await state.clear()
    await state.set_state(Form.waiting_for_text_select)
    
    keyboard = [
        [KeyboardButton(text="Редактировать текст")],
        [KeyboardButton(text="Изменить формат текста")],
        [KeyboardButton(text="Список текстов")],
        [KeyboardButton(text="Справка по форматам")],
        [KeyboardButton(text="Назад в меню")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    await message.answer(text="Управление текстами\n\nВыберите действие:", reply_markup=markup, parse_mode="HTML")

async def handle_question_menu(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    if msg_text == "Назад в меню":
        await start_command(message, state)
        return
    elif msg_text == "Список вопросов":
        await show_questions_list(message)
        return
    elif msg_text == "Добавить вопрос":
        await state.set_state(Form.waiting_for_question_text)
        await state.update_data(question_action="add", question_key=None, question_text=None)
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
        await message.answer(text="Введите ключ вопроса (например: experience):", reply_markup=markup)
        return
    elif msg_text == "Редактировать вопрос":
        await state.set_state(Form.waiting_for_edit_select)
        await state.update_data(question_action="edit")
        await show_questions_for_selection(message, "редактирования")
        return
    elif msg_text == "Удалить вопрос":
        await state.set_state(Form.waiting_for_delete_select)
        await state.update_data(question_action="delete")
        await show_questions_for_selection(message, "удаления")
        return
    elif msg_text == "Отмена":
        await manage_questions_menu(message, state)
        return

async def handle_question_text_input(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    data = await state.get_data()
    
    if msg_text == "Отмена":
        await manage_questions_menu(message, state)
        return
    
    if data.get("question_action") == "add":
        await state.update_data(question_key=msg_text)
        await state.set_state(Form.waiting_for_question_display)
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
        await message.answer(text="Введите текст вопроса:", reply_markup=markup)
    else:
        await manage_questions_menu(message, state)

async def handle_question_display_input(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    data = await state.get_data()
    
    if msg_text == "Отмена":
        await manage_questions_menu(message, state)
        return
    
    if data.get("question_action") == "add":
        await state.update_data(question_text=msg_text)
        await state.set_state(Form.waiting_for_display_text)
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
        await message.answer(
            text="Введите отображаемый текст (используйте {answer} для ответа):", 
            reply_markup=markup
        )
    else:
        await manage_questions_menu(message, state)

async def handle_display_text_input(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    admin_id = message.from_user.id
    data = await state.get_data()
    
    if msg_text == "Отмена":
        await manage_questions_menu(message, state)
        return
    
    q_key = data.get("question_key")
    q_text = data.get("question_text")
    
    if not q_key or not q_text:
        await message.answer("Ошибка: не найдены данные вопроса")
        await manage_questions_menu(message, state)
        return
    
    try:
        result = db_execute("SELECT MAX(order_num) FROM questions", fetch=True)
        max_order = result[0] if result and result[0] is not None else 0
        db_execute(
            "INSERT INTO questions (question_key, question_text, display_text, order_num) VALUES (?, ?, ?, ?)", 
            (q_key, q_text, msg_text, max_order + 1)
        )
        await message.answer(text="Вопрос успешно добавлен!")
        logger.info(f"Главный админ {admin_id} добавил вопрос: {q_key}")
    except Exception as e:
        logger.error(f"Ошибка добавления вопроса: {e}")
        await message.answer(text="Ошибка при добавлении вопроса")
    
    await manage_questions_menu(message, state)

async def handle_edit_select(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    if msg_text == "Отмена":
        await manage_questions_menu(message, state)
        return
    
    await state.update_data(edit_question_key=msg_text)
    keyboard = [
        [KeyboardButton(text="Текст вопроса"), KeyboardButton(text="Отображаемый текст")],
        [KeyboardButton(text="Отмена")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    await message.answer(text=f"Выберите что редактировать для вопроса {msg_text}:", reply_markup=markup, parse_mode="HTML")
    await state.set_state(Form.waiting_for_edit_text)

async def handle_edit_text_choice(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    if msg_text == "Отмена":
        await manage_questions_menu(message, state)
        return
    
    data = await state.get_data()
    q_key = data.get("edit_question_key")
    
    if msg_text == "Текст вопроса":
        await state.update_data(edit_field="question_text")
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
        await message.answer(text="Введите новый текст вопроса:", reply_markup=markup)
        await state.set_state(Form.waiting_for_edit_display)
    elif msg_text == "Отображаемый текст":
        await state.update_data(edit_field="display_text")
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
        await message.answer(text="Введите новый отображаемый текст:", reply_markup=markup)
        await state.set_state(Form.waiting_for_edit_display)

async def handle_edit_display_input(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    admin_id = message.from_user.id
    if msg_text == "Отмена":
        await manage_questions_menu(message, state)
        return
    
    data = await state.get_data()
    q_key = data.get("edit_question_key")
    field = data.get("edit_field")
    
    try:
        if field == "question_text":
            db_execute("UPDATE questions SET question_text = ? WHERE question_key = ?", (msg_text, q_key))
            await message.answer(text="Текст вопроса обновлен!")
            logger.info(f"Главный админ {admin_id} обновил текст вопроса {q_key}")
        elif field == "display_text":
            db_execute("UPDATE questions SET display_text = ? WHERE question_key = ?", (msg_text, q_key))
            await message.answer(text="Отображаемый текст обновлен!")
            logger.info(f"Главный админ {admin_id} обновил отображаемый текст {q_key}")
    except Exception as e:
        logger.error(f"Ошибка обновления вопроса: {e}")
    
    await manage_questions_menu(message, state)

async def handle_delete_question(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    admin_id = message.from_user.id
    if msg_text == "Отмена":
        await manage_questions_menu(message, state)
        return
    
    try:
        db_execute("DELETE FROM questions WHERE question_key = ?", (msg_text,))
        await message.answer(text=f"Вопрос '{msg_text}' удален!")
        logger.info(f"Главный админ {admin_id} удалил вопрос: {msg_text}")
    except Exception as e:
        logger.error(f"Ошибка удаления вопроса: {e}")
    
    await manage_questions_menu(message, state)

async def show_questions_list(message: Message):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут просматривать вопросы")
        return
        
    try:
        result = db_execute("SELECT question_key, question_text, display_text, order_num FROM questions ORDER BY order_num", fetchall=True)
        if not result:
            await message.answer(text="Список вопросов пуст")
            return
        
        text_parts = ["Список вопросов:\n"]
        for q in result:
            text_parts.append(f"Ключ: <code>{escape_html(q[0])}</code> (порядок: {q[3]})")
            text_parts.append(f"Текст: {escape_html(q[1])}")
            text_parts.append(f"Отображение: {escape_html(q[2])}")
            text_parts.append("")
        
        full_text = "\n".join(text_parts)
        if len(full_text) > 4000:
            for i in range(0, len(full_text), 4000):
                await message.answer(text=full_text[i:i+4000], parse_mode="HTML")
        else:
            await message.answer(text=full_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка показа вопросов: {e}")
        await message.answer(text="Ошибка при отображении списка вопросов")

async def show_questions_for_selection(message: Message, action: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять вопросами")
        return
        
    try:
        result = db_execute("SELECT question_key, question_text FROM questions ORDER BY order_num", fetchall=True)
        if not result:
            await message.answer(text="Нет доступных вопросов")
            return
        
        keyboard = []
        for q in result:
            keyboard.append([KeyboardButton(text=q[0])])
        keyboard.append([KeyboardButton(text="Отмена")])
        
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        await message.answer(text=f"Выберите вопрос для {action}:", reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка показа вопросов для выбора: {e}")

async def handle_text_menu(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять текстами")
        return
        
    if msg_text == "Назад в меню":
        await start_command(message, state)
        return
    elif msg_text == "Список текстов":
        await show_texts_list(message)
        return
    elif msg_text == "Редактировать текст":
        await show_texts_for_selection(message)
        await state.set_state(Form.waiting_for_text_edit)
        return
    elif msg_text == "Изменить формат текста":
        await show_texts_for_format_change(message)
        await state.set_state(Form.waiting_for_format_select)
        return
    elif msg_text == "Справка по форматам":
        await show_format_help(message)
        return
    elif msg_text == "Отмена":
        await manage_texts_menu(message, state)
        return

async def show_texts_list(message: Message):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут просматривать тексты")
        return
        
    try:
        result = db_execute("SELECT text_key, description, parse_mode, text_content FROM texts", fetchall=True)
        if not result:
            await message.answer(text="Список текстов пуст")
            return
        
        text_parts = ["Список текстов:\n"]
        for t in result:
            text_parts.append(f"Ключ: <code>{t[0]}</code>")
            text_parts.append(f"Описание: {escape_html(t[1] or 'Нет описания')}")
            text_parts.append(f"Формат: {escape_html(t[2] or 'Нет')}")
            preview = t[3][:100].replace('<', '&lt;').replace('>', '&gt;')
            text_parts.append(f"Текст: {preview}...")
            text_parts.append("")
        
        full_text = "\n".join(text_parts)
        if len(full_text) > 4000:
            for i in range(0, len(full_text), 4000):
                await message.answer(text=full_text[i:i+4000], parse_mode="HTML")
        else:
            await message.answer(text=full_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка показа текстов: {e}")
        await message.answer(text="Ошибка при отображении списка текстов")

async def show_texts_for_selection(message: Message):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять текстами")
        return
        
    try:
        result = db_execute("SELECT text_key, description FROM texts", fetchall=True)
        if not result:
            await message.answer(text="Нет доступных текстов")
            return
        
        keyboard = []
        for t in result:
            keyboard.append([KeyboardButton(text=t[0])])
        keyboard.append([KeyboardButton(text="Отмена")])
        
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        await message.answer(text="Выберите текст для редактирования:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Ошибка показа текстов для выбора: {e}")

async def show_texts_for_format_change(message: Message):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять текстами")
        return
        
    try:
        result = db_execute("SELECT text_key, description, parse_mode FROM texts", fetchall=True)
        if not result:
            await message.answer(text="Нет доступных текстов")
            return
        
        keyboard = []
        for t in result:
            keyboard.append([KeyboardButton(text=f"{t[0]} [{t[2]}]")])
        keyboard.append([KeyboardButton(text="Отмена")])
        
        markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
        await message.answer(text="Выберите текст для изменения формата:", reply_markup=markup)
    except Exception as e:
        logger.error(f"Ошибка показа текстов для формата: {e}")

async def show_format_help(message: Message):
    help_text = """Справка по форматам текста

HTML формат (рекомендуется):
• <code><b>жирный</b></code> - жирный текст
• <code><i>курсив</i></code> - курсив
• <code><code>моноширинный</code></code> - моноширинный

Markdown формат:
• <code>*жирный*</code> - жирный текст
• <code>_курсив_</code> - курсив
• <code>`код`</code> - моноширинный

Переменные:
• <code>{name}</code> - имя пользователя
• <code>{count}</code> - количество заявок
• <code>{reason}</code> - причина отказа
• <code>{answer}</code> - ответ пользователя
• <code>{invite_link}</code> - одноразовая ссылка-приглашение"""
    
    await message.answer(text=help_text, parse_mode="HTML")

async def handle_text_edit(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять текстами")
        return
        
    admin_id = message.from_user.id
    if msg_text == "Отмена":
        await manage_texts_menu(message, state)
        return
    
    data = await state.get_data()
    if not data.get("edit_text_key"):
        await state.update_data(edit_text_key=msg_text)
        try:
            result = db_execute("SELECT text_content, parse_mode FROM texts WHERE text_key = ?", (msg_text,), fetch=True)
            if result:
                current_text = result[0]
                parse_mode = result[1]
                
                format_hint = ""
                if parse_mode == "HTML":
                    format_hint = "Используйте HTML теги: <b>жирный</b>, <i>курсив</i>, <code>код</code>"
                elif parse_mode == "Markdown":
                    format_hint = "Используйте Markdown: *жирный*, _курсив_, `код`"
                
                markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
                await message.answer(
                    text=f"Редактирование текста: <code>{msg_text}</code>\nФормат: {parse_mode}\nПодсказка: {format_hint}\n\nТекущий текст:\n{current_text}\n\nВведите новый текст:",
                    reply_markup=markup, parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Ошибка получения текста: {e}")
            await message.answer(text="Ошибка при получении текста")
    else:
        text_key = data.get("edit_text_key")
        try:
            db_execute("UPDATE texts SET text_content = ? WHERE text_key = ?", (msg_text, text_key))
            result = db_execute("SELECT parse_mode FROM texts WHERE text_key = ?", (text_key,), fetch=True)
            parse_mode = result[0] if result else "HTML"
            await message.answer(text=f"Текст {text_key} обновлен!\nФормат: {parse_mode}", parse_mode="HTML")
            logger.info(f"Главный админ {admin_id} обновил текст {text_key}")
        except Exception as e:
            logger.error(f"Ошибка обновления текста: {e}")
            await message.answer(text="Ошибка при обновлении текста")
        await manage_texts_menu(message, state)

async def handle_format_change(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять текстами")
        return
        
    if msg_text == "Отмена":
        await manage_texts_menu(message, state)
        return
    
    text_key = msg_text.split(" [")[0] if " [" in msg_text else msg_text
    result = db_execute("SELECT text_key, parse_mode FROM texts WHERE text_key = ?", (text_key,), fetch=True)
    
    if not result:
        await message.answer(text="Текст не найден")
        return
    
    await state.update_data(format_text_key=text_key)
    
    keyboard = [
        [KeyboardButton(text="HTML"), KeyboardButton(text="Markdown")],
        [KeyboardButton(text="Отмена")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    await message.answer(
        text=f"Текущий формат текста {text_key}: {result[1]}\n\nВыберите новый формат:\n• HTML - использует HTML теги\n• Markdown - использует Markdown разметку",
        reply_markup=markup, parse_mode="HTML"
    )
    await state.set_state("waiting_for_format_choice")

async def apply_format_change(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("Только главные админы могут управлять текстами")
        return
        
    admin_id = message.from_user.id
    if msg_text == "Отмена":
        await manage_texts_menu(message, state)
        return
    
    if msg_text not in ["HTML", "Markdown"]:
        await message.answer(text="Пожалуйста, выберите HTML или Markdown")
        return
    
    data = await state.get_data()
    text_key = data.get("format_text_key")
    
    if not text_key:
        await message.answer(text="Ошибка: текст не выбран")
        await manage_texts_menu(message, state)
        return
    
    try:
        db_execute("UPDATE texts SET parse_mode = ? WHERE text_key = ?", (msg_text, text_key))
        await message.answer(text=f"Формат текста {text_key} изменен на {msg_text}", parse_mode="HTML")
        logger.info(f"Главный админ {admin_id} изменил формат {text_key} на {msg_text}")
    except Exception as e:
        logger.error(f"Ошибка изменения формата: {e}")
        await message.answer(text="Ошибка при изменении формата")
    
    await manage_texts_menu(message, state)

async def show_invite_settings_menu(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    settings = get_invite_settings()
    
    status = "Включена" if settings["is_enabled"] else "Выключена"
    auto_status = "Включено" if settings["auto_approve"] else "Выключено"
    chat = settings["chat_id"] or "Не задан"
    last_link = settings["last_invite_link"] or "Не генерировалась"
    
    text = f"Настройка одноразовых приглашений\n\n"
    text += f"Генерация ссылок: {status}\n"
    text += f"Авто-одобрение: {auto_status}\n"
    text += f"Чат: <code>{chat}</code>\n"
    text += f"Последняя ссылка: {last_link}\n\n"
    text += "Ссылка будет вставляться в текст одобрения через переменную {invite_link}\n"
    text += "Авто-одобрение: пользователь подаёт заявку на вступление, бот проверяет статус и автоматически одобряет/отклоняет"
    
    keyboard = [
        [KeyboardButton(text="Включить ссылки"), KeyboardButton(text="Выключить ссылки")],
        [KeyboardButton(text="Включить авто-одобрение"), KeyboardButton(text="Выключить авто-одобрение")],
        [KeyboardButton(text="Задать ID чата")],
        [KeyboardButton(text="Сгенерировать новую ссылку")],
        [KeyboardButton(text="Показать текущую ссылку")],
        [KeyboardButton(text="Назад в меню")]
    ]
    markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
    
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

async def handle_invite_settings(message: Message, state: FSMContext, msg_text: str):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    admin_id = message.from_user.id
    
    if msg_text == "Назад в меню":
        await start_command(message, state)
        return
    elif msg_text == "Включить ссылки":
        if toggle_invite(True, admin_id):
            await message.answer("Генерация ссылок включена")
        else:
            await message.answer("Ошибка включения")
        await show_invite_settings_menu(message, state)
        return
    elif msg_text == "Выключить ссылки":
        if toggle_invite(False, admin_id):
            await message.answer("Генерация ссылок выключена")
        else:
            await message.answer("Ошибка выключения")
        await show_invite_settings_menu(message, state)
        return
    elif msg_text == "Включить авто-одобрение":
        if toggle_auto_approve(True, admin_id):
            await message.answer("Авто-одобрение включено")
        else:
            await message.answer("Ошибка включения")
        await show_invite_settings_menu(message, state)
        return
    elif msg_text == "Выключить авто-одобрение":
        if toggle_auto_approve(False, admin_id):
            await message.answer("Авто-одобрение выключено")
        else:
            await message.answer("Ошибка выключения")
        await show_invite_settings_menu(message, state)
        return
    elif msg_text == "Задать ID чата":
        await state.set_state(Form.waiting_for_invite_chat)
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True)
        await message.answer(
            "Введите ID чата (группы), для которого будут генерироваться ссылки:\n\n"
            "Как получить ID:\n"
            "1. Добавьте бота в группу\n"
            "2. Напишите /get_chat_id в группе\n"
            "3. Скопируйте ID (отрицательное число)",
            reply_markup=markup
        )
        return
    elif msg_text == "Сгенерировать новую ссылку":
        link = await generate_invite_link_for_user(0)
        if link:
            await message.answer(f"Новая ссылка создана:\n{link}\n\nОна будет использоваться при следующем одобрении заявки")
        else:
            settings = get_invite_settings()
            if not settings["is_enabled"]:
                await message.answer("Генерация ссылок отключена. Включите в меню.")
            elif not settings["chat_id"]:
                await message.answer("Чат не задан. Настройте ID чата.")
            else:
                await message.answer("Ошибка генерации ссылки")
        await show_invite_settings_menu(message, state)
        return
    elif msg_text == "Показать текущую ссылку":
        link = get_last_invite_link()
        if link:
            await message.answer(f"Текущая ссылка:\n{link}")
        else:
            await message.answer("Ссылка еще не генерировалась")
        await show_invite_settings_menu(message, state)
        return
    else:
        await show_invite_settings_menu(message, state)

async def process_invite_chat_input(message: Message, state: FSMContext):
    if not is_main_admin(message.from_user.id):
        await message.answer("У вас нет прав")
        return
    
    if message.text == "Отмена":
        await start_command(message, state)
        return
    
    chat_id = message.text.strip()
    
    try:
        int(chat_id)
    except ValueError:
        await message.answer("ID чата должен быть числом. Попробуйте снова:")
        return
    
    if set_invite_chat(chat_id, message.from_user.id):
        await message.answer(f"Чат для приглашений установлен: <code>{chat_id}</code>", parse_mode="HTML")
    else:
        await message.answer("Ошибка установки чата")
    
    await state.clear()
    await show_invite_settings_menu(message, state)

async def get_invite_link_for_user(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    result = db_execute("SELECT state FROM applications WHERE user = ?", (user_id,), fetch=True)
    if not result:
        await message.answer("Вы ещё не подавали заявку.")
        return
    
    state_db = result[0]
    
    if state_db != "applied":
        await message.answer("Ваша заявка ещё не одобрена.")
        return
    
    new_link = await generate_invite_link_for_user(user_id)
    
    if new_link:
        db_execute("UPDATE applications SET invite_link = ? WHERE user = ?", (new_link, user_id))
        await message.answer(
            f"🔗 Ваша ссылка для входа:\n{new_link}\n\n"
            "⚠️ После перехода по ссылке бот автоматически проверит вашу заявку и одобрит вход.\n"
            "Если у вас возникнут проблемы, обратитесь к администратору."
        )
        logger.info(f"Пользователю {user_id} выдана новая ссылка-приглашение")
    else:
        settings = get_invite_settings()
        if not settings["is_enabled"]:
            await message.answer("❌ Генерация ссылок отключена администратором.")
        elif not settings["chat_id"]:
            await message.answer("❌ Чат для ссылок не настроен. Обратитесь к администратору.")
        else:
            await message.answer("❌ Не удалось сгенерировать ссылку. Попробуйте позже.")

async def info_command(message: Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"Пользователь {user_id} запросил информацию о заявке")
    try:
        result = db_execute("SELECT state, comment, invite_link FROM applications WHERE user = ?", (user_id,), fetch=True)
        if result:
            app_state = result[0]
            comment = result[1]
            invite_link = result[2]
            
            if app_state == "applied":
                if invite_link:
                    await send_formatted_message(message, "apply_text", invite_link=invite_link)
                else:
                    await send_formatted_message(message, "apply_text")
                return
            elif app_state == "canceled":
                await send_formatted_message(message, "final_cancel_text", reason=escape_html(comment or ""))
                return
            elif app_state == "sended":
                answers = get_application_answers(user_id)
                questions = get_questions()
                go_text = ["Информация о заявке\nСтатус: Отправлено\n"]
                for i, (q_key, q_text, display_text) in enumerate(questions, 1):
                    answer = answers.get(q_key, "Не заполнено")
                    safe_answer = escape_html(str(answer))
                    display = display_text.replace("{answer}", safe_answer)
                    go_text.append(display)
                try:
                    await message.answer(text="\n".join(go_text), parse_mode="HTML")
                except:
                    await message.answer(text="\n".join(go_text))
                return
            elif app_state == "created":
                await message.answer(text="Вы начали заполнять заявку, но не отправили её. Нажмите 'Подать заявку' чтобы продолжить.")
                return
        await message.answer(text="Ты ещё не отправлял заявку")
    except Exception as e:
        logger.error(f"Ошибка info: {e}")
        await message.answer(text="Произошла ошибка при получении информации")

@dp.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    if not is_private_chat(message):
        return
    
    user_id = message.from_user.id
    msg_text = message.text
    current_state = await state.get_state()
    data = await state.get_data()
    
    logger.info(f"Пользователь {user_id} (@{message.from_user.username or 'no_username'}) отправил: {msg_text}")
    
    if current_state == Form.waiting_for_invite_chat:
        await process_invite_chat_input(message, state)
        return
    
    if not is_admin(user_id) and is_user_banned(user_id):
        remaining = (banned_users[user_id] - datetime.now()).seconds
        await message.answer(f"Вы заблокированы. Осталось: {remaining} сек.")
        return
    
    if not is_admin(user_id):
        is_spam, reason = check_spam(user_id)
        if is_spam:
            logger.warning(f"Пользователь {user_id} забанен за флуд: {reason}")
            await message.answer(f"Вы заблокированы: {reason}")
            return
    
    if msg_text == "Отменить заявку" and data.get("application_mode"):
        await state.clear()
        await message.answer("Заявка отменена", reply_markup=ReplyKeyboardRemove())
        await start_command(message, state)
        return
    
    if data.get("application_mode"):
        if msg_text == "Назад":
            await show_question(message, state, "prev")
            return
        elif msg_text == "Пропустить":
            await show_question(message, state, "next")
            return
        elif msg_text == "Предпросмотр":
            await show_preview(message, state)
            return
        elif msg_text == "Отправить заявку":
            await submit_application(message, state)
            return
        elif msg_text == "Назад к вопросам":
            await show_question(message, state, "restore")
            return
        else:
            await save_answer_and_continue(message, state, msg_text)
            return
    
    if is_admin(user_id):
        if msg_text == "Начать проверку":
            await check_applications(message, state)
            return
        elif msg_text == "Отказанные заявки":
            await show_rejected_applications(message, state)
            return
        elif msg_text == "Одобренные заявки":
            await show_applied_applications(message, state)
            return
        elif msg_text == "Назад в меню":
            await start_command(message, state)
            return
        elif msg_text.startswith("+"):
            await add_extra_attempt_from_button(message, msg_text)
            return
        
        if is_main_admin(user_id):
            if msg_text == "Управление вопросами":
                await manage_questions_menu(message, state)
                return
            elif msg_text == "Управление текстами":
                await manage_texts_menu(message, state)
                return
            elif msg_text == "Управление админами":
                await show_admin_management(message, state)
                return
            elif msg_text == "Добавить админа":
                await handle_add_admin(message, state)
                return
            elif msg_text == "Удалить админа":
                await handle_remove_admin(message, state)
                return
            elif msg_text == "Черный список":
                await show_blacklist_menu(message, state)
                return
            elif msg_text == "Добавить в черный список":
                await handle_blacklist_add(message, state)
                return
            elif msg_text == "Удалить из черного списка":
                await handle_blacklist_remove(message, state)
                return
            elif msg_text == "Найти в черном списке":
                await search_blacklist(message, state)
                return
            elif msg_text == "Шаблоны отказа":
                await show_templates_menu(message, state)
                return
            elif msg_text == "Добавить шаблон":
                await handle_template_add(message, state)
                return
            elif msg_text == "Удалить шаблон":
                await handle_template_delete(message, state)
                return
            elif msg_text == "Настройка приглашений":
                await show_invite_settings_menu(message, state)
                return
            elif msg_text in ["Включить ссылки", "Выключить ссылки", "Включить авто-одобрение", "Выключить авто-одобрение", "Задать ID чата", "Сгенерировать новую ссылку", "Показать текущую ссылку"]:
                await handle_invite_settings(message, state, msg_text)
                return
        
        if data.get("check_user_id"):
            if msg_text == "Одобрить":
                await approve_application(message, state, data["check_user_id"])
                return
            elif msg_text == "Отклонить":
                await state.set_state(Form.waiting_for_comment)
                await state.update_data(decline_user_id=data["check_user_id"])
                
                templates = get_decline_templates()
                keyboard = [
                    [KeyboardButton(text="Отмена")]
                ]
                for tid, template_text, added_by, added_at in templates:
                    preview = template_text[:40] + "..." if len(template_text) > 40 else template_text
                    keyboard.append([KeyboardButton(text=preview)])
                
                markup = ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)
                await message.answer(text="Опишите причину отклонения или выберите шаблон:", reply_markup=markup)
                return
            elif msg_text == "Следующая":
                await show_next_application(message, state)
                return
            elif msg_text == "Отмена":
                current_user_id = data.get("check_user_id")
                if current_user_id:
                    if current_user_id in auto_unlock_tasks:
                        auto_unlock_tasks[current_user_id].cancel()
                        del auto_unlock_tasks[current_user_id]
                    unlock_application(current_user_id)
                await start_command(message, state)
                return
    
    if current_state == Form.waiting_for_decline_choice:
        await process_decline_choice(message, state)
        return
    
    if current_state == Form.waiting_for_comment:
        user_id_comment = data.get("decline_user_id")
        if user_id_comment:
            templates = get_decline_templates()
            template_found = False
            for tid, template_text, added_by, added_at in templates:
                preview = template_text[:40] + "..." if len(template_text) > 40 else template_text
                if msg_text == preview or msg_text == template_text:
                    await decline_application(message, state, user_id_comment, template_text)
                    template_found = True
                    break
            
            if not template_found:
                await decline_application(message, state, user_id_comment, msg_text)
        return
    
    if current_state == Form.waiting_for_new_admin:
        await process_add_admin(message, state)
        return
    
    if current_state == Form.waiting_for_remove_admin:
        await process_remove_admin(message, state)
        return
    
    if current_state == Form.waiting_for_rejected_search:
        await process_rejected_search(message, state)
        return
    
    if current_state == Form.waiting_for_applied_search:
        await process_applied_search(message, state)
        return
    
    if current_state == Form.waiting_for_blacklist_add:
        await process_blacklist_add(message, state)
        return
    
    if current_state == Form.waiting_for_blacklist_remove:
        await process_blacklist_remove(message, state)
        return
    
    if current_state == Form.waiting_for_blacklist_search:
        await process_blacklist_search(message, state)
        return
    
    if current_state == Form.waiting_for_template_add:
        await process_template_add(message, state)
        return
    
    if current_state == Form.waiting_for_template_delete:
        await process_template_delete(message, state)
        return
    
    if current_state == Form.waiting_for_question_key:
        await handle_question_menu(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_question_text:
        await handle_question_text_input(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_question_display:
        await handle_question_display_input(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_display_text:
        await handle_display_text_input(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_edit_select:
        await handle_edit_select(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_edit_text:
        await handle_edit_text_choice(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_edit_display:
        await handle_edit_display_input(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_delete_select:
        await handle_delete_question(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_text_select:
        await handle_text_menu(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_text_edit:
        await handle_text_edit(message, state, msg_text)
        return
    elif current_state == Form.waiting_for_format_select:
        await handle_format_change(message, state, msg_text)
        return
    elif current_state == "waiting_for_format_choice":
        await apply_format_change(message, state, msg_text)
        return
    
    if msg_text == "🔗 Получить ссылку":
        await get_invite_link_for_user(message, state)
        return
    elif msg_text == "О сервере":
        await send_formatted_message(message, "server_info_text")
        return
    elif msg_text == "Подать заявку":
        await start_application(message, state)
        return
    elif msg_text == "Информация о заявке":
        await info_command(message, state)
        return
    elif msg_text == "Назад в меню":
        await start_command(message, state)
        return
    elif msg_text == "Отмена":
        if current_state in [Form.waiting_for_question_text, Form.waiting_for_question_display, 
                           Form.waiting_for_display_text, Form.waiting_for_edit_select,
                           Form.waiting_for_edit_text, Form.waiting_for_edit_display,
                           Form.waiting_for_delete_select, Form.waiting_for_text_edit,
                           Form.waiting_for_format_select, "waiting_for_format_choice"]:
            await manage_questions_menu(message, state)
            return
        elif current_state == Form.waiting_for_text_select:
            await manage_texts_menu(message, state)
            return
        elif current_state == Form.waiting_for_comment:
            await start_command(message, state)
            return
        elif current_state == Form.waiting_for_rejected_search:
            await start_command(message, state)
            return
        elif current_state == Form.waiting_for_applied_search:
            await start_command(message, state)
            return
        elif current_state == Form.waiting_for_blacklist_add:
            await show_blacklist_menu(message, state)
            return
        elif current_state == Form.waiting_for_blacklist_remove:
            await show_blacklist_menu(message, state)
            return
        elif current_state == Form.waiting_for_blacklist_search:
            await show_blacklist_menu(message, state)
            return
        elif current_state == Form.waiting_for_template_add:
            await show_templates_menu(message, state)
            return
        elif current_state == Form.waiting_for_template_delete:
            await show_templates_menu(message, state)
            return
        elif current_state == Form.waiting_for_invite_chat:
            await show_invite_settings_menu(message, state)
            return
        else:
            await start_command(message, state)
        return

async def main():
    if not check_single_instance():
        print("Другой экземпляр бота уже запущен. Выход...")
        sys.exit(1)
    
    print("Бот готов к работе...")
    logger.info("Бот готов к работе...")
    
    def signal_handler(sig, frame):
        logger.info(f"Получен сигнал {sig}, завершение работы...")
        print(f"Получен сигнал {sig}, завершение работы...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    while True:
        try:
            await dp.start_polling(bot, timeout=30, relax=0.5, allowed_updates=["message", "callback_query", "chat_join_request"])
        except TelegramNetworkError as e:
            logger.error(f"Ошибка сети: {e}. Перезапуск через 5 секунд...")
            await asyncio.sleep(5)
        except TelegramServerError as e:
            logger.error(f"Ошибка сервера: {e}. Перезапуск через 10 секунд...")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Ошибка polling: {e}. Перезапуск через 5 секунд...")
            await asyncio.sleep(5)
        finally:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()
                os.remove(LOCK_FILE)
            except:
                pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
        logger.info("Бот остановлен")
    except SystemExit:
        print("Бот остановлен")
        logger.info("Бот остановлен")