from aiogram import Bot
from .db import execute

def settings():
    r=execute('SELECT chat_id,is_enabled,last_invite_link,last_generated_at,auto_approve FROM invite_settings ORDER BY id LIMIT 1',one=True)
    if not r: return {'chat_id':None,'enabled':False,'last_link':None,'last_at':None,'auto':False}
    return {'chat_id':r['chat_id'],'enabled':bool(r['is_enabled']),'last_link':r['last_invite_link'],'last_at':r['last_generated_at'],'auto':bool(r['auto_approve'])}

async def generate(bot:Bot, uid:int|None=None, remember:bool=True)->str|None:
    s=settings()
    if not s['enabled'] or not s['chat_id']: return None
    try:
        chat_id=int(s['chat_id'])
        me=await bot.get_me(); member=await bot.get_chat_member(chat_id,me.id)
        if member.status not in ('administrator','creator'): return None
        link=await bot.create_chat_invite_link(chat_id=chat_id,creates_join_request=s['auto'],name=(f'Lavender user {uid}' if uid else 'Lavender invite'))
        if remember: execute('UPDATE invite_settings SET last_invite_link=?,last_generated_at=CURRENT_TIMESTAMP',(link.invite_link,))
        return link.invite_link
    except Exception: return None
