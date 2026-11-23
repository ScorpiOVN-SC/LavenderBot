from .db import execute
MAIN_ADMINS: set[int] = set()

def is_main_admin(uid:int)->bool: return uid in MAIN_ADMINS

def is_admin(uid:int)->bool:
    return is_main_admin(uid) or execute('SELECT 1 FROM admins WHERE user_id=?',(uid,),one=True) is not None

def add_admin(uid:int, tag:str|None=None)->bool:
    if is_main_admin(uid): return False
    try:
        profile=execute('SELECT username,first_name,last_name,display_name,last_seen FROM bot_users WHERE user_id=?',(uid,),one=True)
        if profile:
            return execute('INSERT OR IGNORE INTO admins(user_id,tag,first_name,last_name,display_name,last_seen) VALUES(?,?,?,?,?,?)',(uid,tag or profile['username'],profile['first_name'],profile['last_name'],profile['display_name'],profile['last_seen'])) > 0
        return execute('INSERT OR IGNORE INTO admins(user_id,tag) VALUES(?,?)',(uid,tag)) > 0
    except Exception: return False

def remove_admin(uid:int)->bool:
    if is_main_admin(uid): return False
    return execute('DELETE FROM admins WHERE user_id=?',(uid,)) > 0
