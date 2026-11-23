from __future__ import annotations
import json, os, sqlite3, threading, shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.getenv('LAVENDER_DATA_DIR', Path(__file__).resolve().parent / 'data'))
DB_PATH = Path(os.getenv('LAVENDER_DB_PATH', DATA_DIR / 'Lavender.db'))
_lock = threading.RLock()

def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=10000')
    c.execute('PRAGMA journal_mode=WAL')
    return c

@contextmanager
def transaction(immediate=False):
    with _lock:
        c = connect()
        try:
            c.execute('BEGIN IMMEDIATE' if immediate else 'BEGIN')
            yield c
            c.commit()
        except Exception:
            c.rollback(); raise
        finally: c.close()

def execute(sql, params=(), one=False, all=False):
    with _lock:
        c = connect()
        try:
            cur = c.execute(sql, params)
            if one: return cur.fetchone()
            if all: return cur.fetchall()
            c.commit(); return cur.rowcount
        finally: c.close()

def init_db():
    """Create missing tables and add only missing legacy columns; existing data is preserved."""
    schema = [
      '''CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT,user INTEGER NOT NULL UNIQUE,tag TEXT,state TEXT DEFAULT 'created',comment TEXT,answers TEXT DEFAULT '{}',created_at TIMESTAMP,locked_by INTEGER,locked_at TIMESTAMP,reviewed_by INTEGER,reviewed_at TIMESTAMP,invite_link TEXT)''',
      '''CREATE TABLE IF NOT EXISTS application_history (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,tag TEXT,state TEXT,comment TEXT,answers TEXT,reviewed_by INTEGER,reviewed_at TIMESTAMP,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
      '''CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY AUTOINCREMENT,question_key TEXT NOT NULL UNIQUE,question_text TEXT NOT NULL,display_text TEXT NOT NULL,order_num INTEGER NOT NULL DEFAULT 0)''',
      '''CREATE TABLE IF NOT EXISTS texts (id INTEGER PRIMARY KEY AUTOINCREMENT,text_key TEXT NOT NULL UNIQUE,text_content TEXT NOT NULL,parse_mode TEXT DEFAULT 'HTML',description TEXT)''',
      '''CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL UNIQUE,tag TEXT,first_name TEXT,last_name TEXT,display_name TEXT,last_seen TIMESTAMP)''',
      '''CREATE TABLE IF NOT EXISTS bot_users (user_id INTEGER PRIMARY KEY,username TEXT,first_name TEXT,last_name TEXT,display_name TEXT,last_seen TIMESTAMP,legacy_keyboard_removed INTEGER DEFAULT 0)''',
      '''CREATE TABLE IF NOT EXISTS blacklist (id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL UNIQUE,tag TEXT,reason TEXT,added_by INTEGER,added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
      '''CREATE TABLE IF NOT EXISTS decline_templates (id INTEGER PRIMARY KEY AUTOINCREMENT,template_text TEXT NOT NULL,added_by INTEGER,added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
      '''CREATE TABLE IF NOT EXISTS invite_settings (id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT,is_enabled INTEGER DEFAULT 0,last_invite_link TEXT,last_generated_at TIMESTAMP,updated_by INTEGER,auto_approve INTEGER DEFAULT 0)''',
      '''CREATE TABLE IF NOT EXISTS applications_settings (id INTEGER PRIMARY KEY AUTOINCREMENT,is_open INTEGER DEFAULT 1,updated_by INTEGER,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
      '''CREATE TABLE IF NOT EXISTS ui_sessions (chat_id INTEGER NOT NULL,user_id INTEGER NOT NULL,anchor_message_id INTEGER,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(chat_id,user_id))'''
    ]
    with transaction(immediate=True) as c:
        for q in schema: c.execute(q)
        # Older deployments may have tables missing newer columns.
        required = {
          'applications': {'created_at':'TIMESTAMP','locked_by':'INTEGER','locked_at':'TIMESTAMP','reviewed_by':'INTEGER','reviewed_at':'TIMESTAMP','invite_link':'TEXT','answers':"TEXT DEFAULT '{}'",'comment':'TEXT','tag':'TEXT','state':"TEXT DEFAULT 'created'"},
          'admins': {'tag':'TEXT','first_name':'TEXT','last_name':'TEXT','display_name':'TEXT','last_seen':'TIMESTAMP'},
          'bot_users': {'username':'TEXT','first_name':'TEXT','last_name':'TEXT','display_name':'TEXT','last_seen':'TIMESTAMP','legacy_keyboard_removed':'INTEGER DEFAULT 0'},
          'invite_settings': {'chat_id':'TEXT','is_enabled':'INTEGER DEFAULT 0','last_invite_link':'TEXT','last_generated_at':'TIMESTAMP','updated_by':'INTEGER','auto_approve':'INTEGER DEFAULT 0'},
          'application_history': {'user_id':'INTEGER','tag':'TEXT','state':'TEXT','comment':'TEXT','answers':'TEXT','reviewed_by':'INTEGER','reviewed_at':'TIMESTAMP'}
        }
        for table, cols in required.items():
            existing={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
            for name, decl in cols.items():
                if name not in existing: c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
        c.execute("UPDATE applications SET created_at=datetime('now') WHERE created_at IS NULL")
        if not c.execute('SELECT 1 FROM invite_settings LIMIT 1').fetchone(): c.execute('INSERT INTO invite_settings(is_enabled,auto_approve) VALUES(0,0)')
        if not c.execute('SELECT 1 FROM applications_settings LIMIT 1').fetchone(): c.execute('INSERT INTO applications_settings(is_open) VALUES(1)')
        defaults=[
          ('age','Сколько тебе лет?','Возраст: {answer}',1),('plus','Расскажи о своих плюсах','Плюсы: {answer}',2),
          ('time','Количество времени, которое можешь играть','Время игры: {answer}',3),('about','Что-то от себя, если хочешь добавить','О себе: {answer}',4),
          ('rp','Знаешь ли что такое РП и имел ли опыт в этом?','РП опыт: {answer}',5),('nick','Твой ник, через который ты будешь играть','Ник: {answer}',6)]
        if not c.execute('SELECT 1 FROM questions LIMIT 1').fetchone(): c.executemany('INSERT INTO questions(question_key,question_text,display_text,order_num) VALUES(?,?,?,?)',defaults)
        for uid in MAIN_ADMINS:
            c.execute('INSERT OR IGNORE INTO admins(user_id) VALUES(?)',(uid,))
        # These defaults intentionally contain no emoji; existing customized texts are never overwritten.
        texts=[('welcome_text','Здравствуйте, {name}! Добро пожаловать в Lavender Park.','HTML','Приветствие'),
          ('admin_welcome_text','Здравствуйте, {name}! На рассмотрении {count} заявок.','HTML','Приветствие администратора'),
          ('server_info_text','На нашем сервере используются уникальные модификации.\n\nПочему стоит остаться?\n• Дружное сообщество\n• Активная администрация\n• Регулярные обновления','HTML','Информация о сервере'),
          ('applications_closed_text','Приём заявок временно закрыт.','HTML','Закрытые заявки'),
          ('application_sent_text','Заявка отправлена. Статус можно посмотреть в разделе информации о заявке.','HTML','Заявка отправлена')]
        for row in texts: c.execute('INSERT OR IGNORE INTO texts(text_key,text_content,parse_mode,description) VALUES(?,?,?,?)',row)

def backup_database():
    if not DB_PATH.exists(): return None
    folder=DATA_DIR/'Backups'; folder.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    dest=folder/f'Lavender_backup_{stamp}.db'
    with _lock:
        src=connect(); out=sqlite3.connect(dest)
        try: src.backup(out)
        finally: out.close(); src.close()
    backups=sorted(folder.glob('*.db'))
    for old in backups[:-5]: old.unlink(missing_ok=True)
    return dest

MAIN_ADMINS=[]
