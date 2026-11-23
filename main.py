from __future__ import annotations
import asyncio, html, json, logging, os, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ChatJoinRequest, ReplyKeyboardRemove
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram import Router
from . import db
from .db import execute, transaction
from . import auth, invites
from .spam import check_spam

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN=os.getenv('API_TOKEN','').strip()
MAIN_ADMINS={int(x) for x in os.getenv('MAIN_ADMINS','5479947797,1301864145').split(',') if x.strip().isdigit()}
APPROVED_GROUP_ID=int(os.getenv('APPROVED_GROUP_ID','-1003953845640'))
APPROVED_TOPIC_ID=int(os.getenv('APPROVED_TOPIC_ID','122708'))
auth.MAIN_ADMINS=MAIN_ADMINS
db.MAIN_ADMINS=sorted(MAIN_ADMINS)
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log=logging.getLogger('lavender')

class Form(StatesGroup): waiting_for_input=State()
router=Router()

# ---------- safe rendering and persistent one-message window ----------
def strip_emoji(value:Any)->str:
    text='' if value is None else str(value)
    return re.sub(r'[\U0001F1E0-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]', '', text)
def esc(v:Any)->str: return html.escape(strip_emoji(v),quote=True)
def user_mention(uid:int, name:str|None=None)->str:
    """Every user reference is a hidden tg://user link, using a safe visible label."""
    label=(name or '').strip() or str(uid)
    return f'<a href="tg://user?id={int(uid)}">{esc(label)}</a>'
def username_label(tag:str|None,uid:int)->str: return f'@{esc(tag)}' if tag and tag!='no_username' else str(uid)

def admin_label(uid:int)->str:
    """Best available identity for the administrator who handled an application."""
    row=execute('SELECT tag,first_name,last_name,display_name FROM admins WHERE user_id=?',(uid,),one=True)
    if row and row['display_name']:
        name=row['display_name']
        return f'{name} (@{row["tag"]})' if row['tag'] else name
    profile=execute('SELECT username,first_name,last_name,display_name FROM bot_users WHERE user_id=?',(uid,),one=True)
    if profile:
        name=profile['display_name'] or ' '.join(x for x in (profile['first_name'],profile['last_name']) if x) or str(uid)
        return f'{name} (@{profile["username"]})' if profile['username'] else name
    return str(uid)

def reviewer_for_application(row):
    """Use current application fields, falling back to the most recent history event."""
    reviewer=row['reviewed_by'] if 'reviewed_by' in row.keys() else None
    reviewed_at=row['reviewed_at'] if 'reviewed_at' in row.keys() else None
    if not reviewer and row['state'] in ('applied','canceled','canceled_final'):
        hist=execute('SELECT reviewed_by,reviewed_at FROM application_history WHERE user_id=? AND reviewed_by IS NOT NULL ORDER BY id DESC LIMIT 1',(row['user'] if 'user' in row.keys() else row['user_id'],),one=True)
        if hist: reviewer=hist['reviewed_by']; reviewed_at=reviewed_at or hist['reviewed_at']
    return (int(reviewer),reviewed_at) if reviewer else (None,reviewed_at)
def kb(rows:list[list[tuple[str,str]]])->InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=strip_emoji(t),callback_data=d) for t,d in row] for row in rows])
def nav(back:str|None=None)->list[list[tuple[str,str]]]:
    # Explicit destinations are used for detail screens; otherwise use the navigation stack.
    return [[('← Назад',back or 'nav:back')]]

def pager(page:int,total_items:int,size:int,namespace:str)->list[tuple[str,str]]:
    pages=max(1,(total_items+size-1)//size)
    page=max(0,min(int(page),pages-1))
    if pages<=1: return []
    prev_cb=f'{namespace}:page:{page-1}' if page>0 else 'noop'
    next_cb=f'{namespace}:page:{page+1}' if page+1<pages else 'noop'
    return [('<' if page>0 else '·',prev_cb),(f'{page+1}/{pages}','noop'),('>' if page+1<pages else '·',next_cb)]

def touch_user(user):
    if not user: return
    uid=int(user.id); username=getattr(user,'username',None); first=getattr(user,'first_name',None); last=getattr(user,'last_name',None)
    display=' '.join(x for x in (first,last) if x) or (('@'+username) if username else str(uid))
    execute('INSERT INTO bot_users(user_id,username,first_name,last_name,display_name,last_seen) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name,last_name=excluded.last_name,display_name=excluded.display_name,last_seen=CURRENT_TIMESTAMP',(uid,username,first,last,display))
    if auth.is_admin(uid):
        execute('UPDATE admins SET tag=?,first_name=?,last_name=?,display_name=?,last_seen=CURRENT_TIMESTAMP WHERE user_id=?',(username,first,last,display,uid))

def get_session(chat_id:int,uid:int):
    return execute('SELECT anchor_message_id FROM ui_sessions WHERE chat_id=? AND user_id=?',(chat_id,uid),one=True)
def set_anchor(chat_id:int,uid:int,mid:int):
    execute('INSERT INTO ui_sessions(chat_id,user_id,anchor_message_id,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(chat_id,user_id) DO UPDATE SET anchor_message_id=excluded.anchor_message_id,updated_at=CURRENT_TIMESTAMP',(chat_id,uid,mid))
async def safe_edit(bot:Bot,chat_id:int,uid:int,text:str,markup:InlineKeyboardMarkup|None,state:FSMContext|None=None):
    text=strip_emoji(text)
    row=get_session(chat_id,uid); mid=row['anchor_message_id'] if row else None
    if mid:
        try:
            await bot.edit_message_text(chat_id=chat_id,message_id=mid,text=text,reply_markup=markup,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
            return mid
        except TelegramBadRequest as e:
            msg=str(e).lower()
            if 'message is not modified' in msg: return mid
            if 'message to edit not found' not in msg and 'message_id_invalid' not in msg: raise
    sent=await bot.send_message(chat_id,text,reply_markup=markup,parse_mode=ParseMode.HTML,disable_web_page_preview=True)
    set_anchor(chat_id,uid,sent.message_id)
    if state: await state.update_data(anchor_message_id=sent.message_id)
    return sent.message_id
async def render(target:Message|CallbackQuery,uid:int,text:str,markup,state:FSMContext|None=None):
    chat_id=target.chat.id if isinstance(target,Message) else target.message.chat.id
    return await safe_edit(target.bot,chat_id,uid,text,markup,state)
async def delete_input(message:Message):
    try: await message.delete()
    except (TelegramBadRequest,TelegramForbiddenError): pass

async def remove_legacy_keyboard(bot:Bot,chat_id:int,uid:int):
    """One-time removal of a ReplyKeyboard left by the previous bot version."""
    row=execute('SELECT legacy_keyboard_removed FROM bot_users WHERE user_id=?',(uid,),one=True)
    if row and row['legacy_keyboard_removed']:
        return
    try:
        msg=await bot.send_message(chat_id,'\u2063',reply_markup=ReplyKeyboardRemove(remove_keyboard=True))
        execute('UPDATE bot_users SET legacy_keyboard_removed=1 WHERE user_id=?',(uid,))
        try: await msg.delete()
        except (TelegramBadRequest,TelegramForbiddenError): pass
    except Exception:
        log.exception('Could not remove legacy reply keyboard for %s',uid)
async def push_screen(state:FSMContext,screen:str,payload:dict|None=None):
    d=await state.get_data(); stack=d.get('navigation_stack',[])
    stack=stack[-19:]+[{'screen':screen,'payload':payload or {}}]
    await state.update_data(navigation_stack=stack)
async def back_screen(state:FSMContext):
    d=await state.get_data(); stack=d.get('navigation_stack',[])
    if len(stack)>1: stack.pop()
    await state.update_data(navigation_stack=stack)
    return stack[-1] if stack else {'screen':'home','payload':{}}
async def set_input(state:FSMContext,action:str,field:str,input_kind:str,return_to:str,payload:dict|None=None):
    await state.set_state(Form.waiting_for_input)
    await state.update_data(input_context={'action':action,'field':field,'input_kind':input_kind,'return_to':return_to,'payload':payload or {}})

# ---------- database-backed business helpers ----------
def app_row(uid:int): return execute('SELECT * FROM applications WHERE user=?',(uid,),one=True)
def questions(): return execute('SELECT question_key,question_text,display_text FROM questions ORDER BY order_num,id',all=True) or []
def get_text(key:str,**values):
    r=execute('SELECT text_content,parse_mode FROM texts WHERE text_key=?',(key,),one=True)
    val=r['text_content'] if r else 'Текст не задан'
    for k,v in values.items(): val=val.replace('{'+k+'}',str(v))
    return val

def app_status(uid:int)->str:
    r=app_row(uid); return r['state'] if r else 'created'
def save_history(uid:int,tag:str|None,status:str,comment:str|None,answers:str|None,admin:int|None):
    execute('INSERT INTO application_history(user_id,tag,state,comment,answers,reviewed_by,reviewed_at) VALUES(?,?,?,?,?,?,?)',(uid,tag,status,comment,answers,admin,datetime.now(timezone.utc).isoformat()))

def lock_app(uid:int,admin_id:int)->tuple[bool,int|None]:
    now=datetime.now(timezone.utc); stale=(now-timedelta(minutes=5)).isoformat()
    with transaction(immediate=True) as c:
        row=c.execute('SELECT locked_by,locked_at FROM applications WHERE user=?',(uid,)).fetchone()
        if not row: return False,None
        if row['locked_by'] and row['locked_at'] and row['locked_at']>stale and row['locked_by']!=admin_id: return False,row['locked_by']
        c.execute('UPDATE applications SET locked_by=?,locked_at=? WHERE user=?',(admin_id,now.isoformat(),uid))
        return True,admin_id

def unlock_app(uid:int,admin_id:int|None=None):
    if admin_id is None: execute('UPDATE applications SET locked_by=NULL,locked_at=NULL WHERE user=?',(uid,))
    else: execute('UPDATE applications SET locked_by=NULL,locked_at=NULL WHERE user=? AND locked_by=?',(uid,admin_id))

# ---------- screen renderers ----------
async def home(bot,chat_id,uid,state):
    await state.clear(); await state.update_data(navigation_stack=[{'screen':'home','payload':{}}])
    if auth.is_admin(uid):
        count=execute("SELECT COUNT(*) n FROM applications WHERE state='sended'",one=True)['n']
        rows=[[('Начать проверку','app:review:0'),('Поиск заявок','app:search')]]
        if auth.is_main_admin(uid): rows += [[('Вопросы','q:list:0'),('Тексты','txt:list:0')],[('Админы','adm:list:0'),('Чёрный список','bl:list:0')],[('Шаблоны отказа','tpl:list:0'),('Приглашения','inv:menu')],[('Настройка заявок','app:settings')]]
        text=f'<b>Lavender Park / Панель управления</b>\n\nЗаявок на рассмотрении: <b>{count}</b>'
    else:
        r=app_row(uid); status=r['state'] if r else 'created'; settings=execute('SELECT is_open FROM applications_settings LIMIT 1',one=True); opened=bool(settings['is_open']) if settings else True
        rows=[[('О сервере','menu:server')]]
        if status=='applied': rows.insert(0,[('Получить ссылку','inv:get')]); rows.append([('Информация о заявке','menu:status')])
        elif status in ('sended','canceled','canceled_final'): rows.append([('Информация о заявке','menu:status')])
        elif opened: rows.insert(0,[('Подать заявку','app:start')])
        else: rows.insert(0,[('Приём заявок закрыт','menu:closed')])
        text=f'<b>Lavender Park</b>\n\nЗдравствуйте, {esc((await bot.get_chat(uid)).first_name or "пользователь")}!'
    await safe_edit(bot,chat_id,uid,text,kb(rows),state)

async def show_questions_menu(bot,chat,uid,state,page=0):
    if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
    items=questions(); size=6; pages=max(1,(len(items)+size-1)//size); page=max(0,min(int(page),pages-1))
    rows=[[(qtext[:32],f'q:item:{key}')] for key,qtext,_ in items[page*size:(page+1)*size]]
    rows.append([('Добавить','q:add')])
    pg=pager(page,len(items),size,'q')
    if pg: rows.append(pg)
    rows+=nav()
    await safe_edit(bot,chat,uid,f'<b>Вопросы</b>\nСтраница {page+1}/{pages}\n\nВыберите вопрос:',kb(rows),state)
async def show_texts_menu(bot,chat,uid,state,page=0):
    if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
    items=execute('SELECT text_key,description FROM texts ORDER BY text_key',all=True) or []; size=8; pages=max(1,(len(items)+size-1)//size); page=max(0,min(int(page),pages-1))
    rows=[[(f"{r['description'] or r['text_key']} · {r['text_key']}"[:42],f'txt:item:{r["text_key"]}')] for r in items[page*size:(page+1)*size]]
    rows.append([('Изменить формат','txt:format')])
    pg=pager(page,len(items),size,'txt')
    if pg: rows.append(pg)
    rows+=nav()
    await safe_edit(bot,chat,uid,f'<b>Тексты</b>\nСтраница {page+1}/{pages}',kb(rows),state)
async def show_admins(bot,chat,uid,state,page=0):
    if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
    rows=execute('SELECT user_id,tag,first_name,last_name,display_name FROM admins ORDER BY user_id',all=True) or []; size=8; pages=max(1,(len(rows)+size-1)//size); page=max(0,min(int(page),pages-1))
    buttons=[]
    for r in rows[page*size:(page+1)*size]:
        label=r['display_name'] or ((('@'+r['tag']) if r['tag'] else str(r['user_id'])))
        if r['tag']: label=f'{label} (@{r["tag"]})' if not str(label).startswith('@') else label
        if auth.is_main_admin(int(r['user_id'])): label=f'{label} [главный]'
        buttons.append([(str(label)[:48],f'adm:item:{r["user_id"]}')])
    buttons.append([('Добавить','adm:add'),('Удалить','adm:remove')])
    pg=pager(page,len(rows),size,'adm')
    if pg: buttons.append(pg)
    buttons+=nav()
    await safe_edit(bot,chat,uid,f'<b>Администраторы</b>\nВсего: {len(rows)}\nСтраница {page+1}/{pages}',kb(buttons),state)
async def show_blacklist(bot,chat,uid,state,page=0):
    if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
    items=execute('SELECT user_id,tag,reason FROM blacklist ORDER BY added_at DESC',all=True) or []; size=8; pages=max(1,(len(items)+size-1)//size); page=max(0,min(int(page),pages-1))
    rows=[[(username_label(r['tag'],r['user_id']),f'bl:item:{r["user_id"]}')] for r in items[page*size:(page+1)*size]]
    rows += [[('Добавить','bl:add'),('Найти','bl:search')]]
    pg=pager(page,len(items),size,'bl')
    if pg: rows.append(pg)
    rows+=nav()
    await safe_edit(bot,chat,uid,f'<b>Чёрный список</b>\nВсего: {len(items)}\nСтраница {page+1}/{pages}',kb(rows),state)
async def show_templates(bot,chat,uid,state,page=0):
    if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
    items=execute('SELECT id,template_text FROM decline_templates ORDER BY id DESC',all=True) or []; size=6; pages=max(1,(len(items)+size-1)//size); page=max(0,min(int(page),pages-1))
    rows=[[(f"#{r['id']} · {r['template_text'][:32]}",f'tpl:item:{r["id"]}')] for r in items[page*size:(page+1)*size]]
    rows.append([('Добавить','tpl:add')])
    pg=pager(page,len(items),size,'tpl')
    if pg: rows.append(pg)
    rows+=nav()
    await safe_edit(bot,chat,uid,f'<b>Шаблоны отказа</b>\nСтраница {page+1}/{pages}',kb(rows),state)
async def show_invites(bot,chat,uid,state):
    if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
    s=invites.settings(); rows=[[('Выкл' if s['enabled'] else 'Вкл','inv:toggle'),('Автоодобрение: '+('Вкл' if s['auto'] else 'Выкл'),'inv:auto')],[('Задать ID чата','inv:chat')],[('Сгенерировать ссылку','inv:generate'),('Показать ссылку','inv:show')]]+nav()
    await safe_edit(bot,chat,uid,f"<b>Приглашения</b>\n\nЧат: <code>{esc(s['chat_id'] or 'не задан')}</code>\nСсылки: {'включены' if s['enabled'] else 'выключены'}\nЗаявки на вступление: {'автоодобрение' if s['auto'] else 'обычная ссылка'}\nПоследняя ссылка: {esc(s['last_link'] or 'нет')}",kb(rows),state)
async def denied(bot,chat,uid,state): await safe_edit(bot,chat,uid,'Недостаточно прав.',kb(nav()),state)

async def show_app_question(bot,chat,uid,state):
    d=await state.get_data(); idx=int(d.get('app_index',0)); qs=questions()
    if idx>=len(qs): return await show_app_preview(bot,chat,uid,state)
    prev=d.get('app_answers',{}); q=qs[idx]
    rows=[[('Пропустить','app:skip')],[('Отменить заявку','app:cancel')]]
    await set_input(state,'application_answer',q[0],'text','application_question',{'index':idx})
    await safe_edit(bot,chat,uid,f'<b>Вопрос {idx+1}/{len(qs)}</b>\n\n{esc(q[1])}\n\nВведите ответ отдельным сообщением. Оно будет удалено после обработки.',kb(rows),state)
async def show_app_preview(bot,chat,uid,state):
    d=await state.get_data(); answers=d.get('app_answers',{}); qs=questions()
    lines=['<b>Предпросмотр заявки</b>']
    for key,_,display in qs:
        lines.append(esc(display.replace('{answer}',str(answers.get(key,'Не заполнено')))))
    await state.set_state(None)
    await safe_edit(bot,chat,uid,'\n\n'.join(lines),kb([[('Отправить заявку','app:submit'),('Назад','app:prev')],[('Отменить заявку','app:cancel')]]),state)

# ---------- universal input processor ----------
def valid_input(kind:str,value:str)->tuple[bool,Any,str]:
    v=value.strip()
    if not v: return False,None,'Значение не должно быть пустым.'
    if len(v)>3500: return False,None,'Слишком длинное значение. Максимум 3500 символов.'
    if kind in ('int','chat_id'):
        try: n=int(v)
        except ValueError: return False,None,'Введите целое число.'
        if kind=='int' and n<=0: return False,None,'ID должен быть положительным числом.'
        if kind=='chat_id' and (n==0 or abs(n)<10000): return False,None,'Укажите корректный числовой ID чата.'
        return True,n,''
    if kind=='tag':
        tag=v.removeprefix('@')
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}',tag): return False,None,'Некорректный username. Укажите @username или числовой ID.'
        return True,'@'+tag,''
    return True,v,''

async def handle_input(message:Message,state:FSMContext):
    uid=message.from_user.id; touch_user(message.from_user); data=await state.get_data(); ctx=data.get('input_context') or {}
    if not ctx: await state.clear(); await delete_input(message); return
    action=ctx.get('action'); payload=ctx.get('payload') or {}; kind=ctx.get('input_kind')
    # Authorization is checked before parsing or mutating anything.
    admin_actions={'question_add_key','question_add_text','question_add_display','question_edit_text','question_edit_display','text_edit','text_format','admin_add','admin_remove','blacklist_add_user','blacklist_add_reason','blacklist_search','template_add','invite_chat','application_search','decline_reason'}
    if action in admin_actions and not auth.is_admin(uid):
        await state.clear(); await delete_input(message); return await denied(message.bot,message.chat.id,uid,state)
    if action in {'question_add_key','question_add_text','question_add_display','question_edit_text','question_edit_display','text_edit','text_format','admin_add','admin_remove','blacklist_add_user','blacklist_add_reason','blacklist_search','template_add','invite_chat','application_search','decline_reason'} and not auth.is_main_admin(uid) and action not in {'application_search','decline_reason'}:
        await state.clear(); await delete_input(message); return await denied(message.bot,message.chat.id,uid,state)
    ok,val,error=valid_input(kind,message.text or '')
    if not ok:
        await delete_input(message)
        await safe_edit(message.bot,message.chat.id,uid,f'<b>Некорректный ввод</b>\n{esc(error)}\n\nВведите значение ещё раз.',kb([[('← Отмена','input:cancel')]]),state)
        return
    try:
        if action=='application_answer':
            d=await state.get_data(); qs=questions()
            try:
                index=int(payload.get('index',d.get('app_index',0)))
                field=payload.get('field')
                if not field and 0 <= index < len(qs): field=qs[index][0]
                if not field and 0 <= int(d.get('app_index',0)) < len(qs):
                    index=int(d.get('app_index',0)); field=qs[index][0]
                if not field or index < 0 or index >= len(qs): raise ValueError('Не удалось определить текущий вопрос. Нажмите «Назад» и начните ответ заново.')
                # Use the current question key from DB, not stale FSM payload.
                field=qs[index][0]
                answers=d.get('app_answers',{}) or {}; answers[field]=val
                await state.update_data(app_answers=answers,app_index=index+1)
            except (TypeError,ValueError,IndexError) as exc:
                await delete_input(message)
                await state.update_data(input_context=None)
                return await safe_edit(message.bot,message.chat.id,uid,f'<b>Не удалось сохранить ответ</b>\n{esc(exc)}',kb([[('Продолжить','app:resume')],[('Отменить заявку','app:cancel')]]),state)
            await delete_input(message); await show_app_question(message.bot,message.chat.id,uid,state); return
        if action=='question_add_key':
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,39}',str(val)): raise ValueError('Ключ должен начинаться с латинской буквы и содержать только латиницу, цифры и _.')
            if execute('SELECT 1 FROM questions WHERE question_key=?',(val,),one=True): raise ValueError('Такой ключ уже существует.')
            await state.update_data(draft_question={'key':val}); await set_input(state,'question_add_text','question_text','text','questions',{})
            await delete_input(message); return await safe_edit(message.bot,message.chat.id,uid,'Введите текст вопроса:',kb([[('← Отмена','input:cancel')]]),state)
        if action=='question_add_text':
            d=await state.get_data(); draft=d.get('draft_question',{}); draft['text']=val; await state.update_data(draft_question=draft)
            await set_input(state,'question_add_display','display_text','text','questions',{})
            await delete_input(message); return await safe_edit(message.bot,message.chat.id,uid,'Введите шаблон отображения. Используйте {answer}:',kb([[('← Отмена','input:cancel')]]),state)
        if action=='question_add_display':
            d=await state.get_data(); draft=d.get('draft_question',{}); draft['display']=val
            execute('INSERT INTO questions(question_key,question_text,display_text,order_num) VALUES(?,?,?,COALESCE((SELECT MAX(order_num)+1 FROM questions),1))',(draft['key'],draft['text'],val))
            await state.clear(); await delete_input(message); return await show_questions_menu(message.bot,message.chat.id,uid,state)
        if action in ('question_edit_text','question_edit_display'):
            key=payload['key']; col='question_text' if action=='question_edit_text' else 'display_text'
            if not execute(f'UPDATE questions SET {col}=? WHERE question_key=?',(val,key)): raise ValueError('Вопрос не найден.')
            await state.clear(); await delete_input(message); return await show_question_item(message.bot,message.chat.id,uid,state,key)
        if action=='text_edit':
            execute('UPDATE texts SET text_content=? WHERE text_key=?',(val,payload['key']))
            await state.clear(); await delete_input(message); return await show_text_item(message.bot,message.chat.id,uid,state,payload['key'])
        if action=='text_format':
            if val not in ('HTML','Markdown','MarkdownV2'): raise ValueError('Допустимые форматы: HTML, Markdown, MarkdownV2.')
            execute('UPDATE texts SET parse_mode=? WHERE text_key=?',(val,payload['key']))
            await state.clear(); await delete_input(message); return await show_text_item(message.bot,message.chat.id,uid,state,payload['key'])
        if action=='admin_add':
            try: chat=await message.bot.get_chat(val); tag=chat.username
            except Exception: tag=None
            if not auth.add_admin(val,tag): raise ValueError('Администратор уже существует или не добавлен.')
            await state.clear(); await delete_input(message); return await show_admins(message.bot,message.chat.id,uid,state)
        if action=='admin_remove':
            if not auth.remove_admin(val): raise ValueError('Администратор не найден или является главным администратором.')
            await state.clear(); await delete_input(message); return await show_admins(message.bot,message.chat.id,uid,state)
        if action=='blacklist_add_user':
            if isinstance(val,str) and val.startswith('@'):
                tag=val[1:]; found=execute('SELECT user,tag FROM applications WHERE tag=?',(tag,),one=True)
                if not found: raise ValueError('Пользователь с таким username не найден в заявках. Используйте числовой ID.')
                target=found['user']; tag=found['tag']
            else:
                target=int(val); found=execute('SELECT tag FROM applications WHERE user=?',(target,),one=True); tag=found['tag'] if found else None
            if execute('SELECT 1 FROM blacklist WHERE user_id=?',(target,),one=True): raise ValueError('Пользователь уже в чёрном списке.')
            await state.update_data(blacklist_target=target,blacklist_tag=tag)
            await set_input(state,'blacklist_add_reason','reason','text','blacklist',{})
            await delete_input(message); return await safe_edit(message.bot,message.chat.id,uid,f'Укажите причину для {user_mention(target,username_label(tag,target))}:',kb([[('← Отмена','input:cancel')]]),state)
        if action=='blacklist_add_reason':
            d=await state.get_data(); target=d['blacklist_target']; tag=d.get('blacklist_tag')
            execute('INSERT INTO blacklist(user_id,tag,reason,added_by) VALUES(?,?,?,?)',(target,tag,val,uid))
            execute("UPDATE applications SET state='canceled',comment=? WHERE user=? AND state='sended'",('Чёрный список: '+val,target))
            await state.clear(); await delete_input(message); return await show_blacklist(message.bot,message.chat.id,uid,state)
        if action=='blacklist_search':
            term=val
            if str(term).isdigit(): found=execute('SELECT user_id,tag,reason FROM blacklist WHERE user_id=?',(int(term),),one=True)
            else: found=execute("SELECT user_id,tag,reason FROM blacklist WHERE tag LIKE ? ESCAPE '\\'",('%'+term.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%',),one=True)
            await state.clear(); await delete_input(message)
            if not found: return await safe_edit(message.bot,message.chat.id,uid,'Ничего не найдено.',kb([[('← Назад','bl:page:0')]]),state)
            return await show_blacklist_item(message.bot,message.chat.id,uid,state,found['user_id'])
        if action=='template_add':
            execute('INSERT INTO decline_templates(template_text,added_by) VALUES(?,?)',(val,uid))
            await state.clear(); await delete_input(message); return await show_templates(message.bot,message.chat.id,uid,state)
        if action=='invite_chat':
            me=await message.bot.get_me(); member=await message.bot.get_chat_member(val,me.id)
            if member.status not in ('administrator','creator'): raise ValueError('Бот должен быть администратором указанного чата.')
            execute('UPDATE invite_settings SET chat_id=?,updated_by=?',(str(val),uid))
            await state.clear(); await delete_input(message); return await show_invites(message.bot,message.chat.id,uid,state)
        if action=='application_search':
            await state.clear(); await delete_input(message); return await show_search_results(message.bot,message.chat.id,uid,state,str(val),0)
        if action=='decline_reason':
            await state.clear(); await delete_input(message); return await finish_decline(message.bot,message.chat.id,uid,state,int(payload['target']),str(val),bool(payload.get('final')))
    except Exception as e:
        log.exception('input action failed: %s',action)
        await delete_input(message)
        await safe_edit(message.bot,message.chat.id,uid,f'<b>Не удалось выполнить действие</b>\n{esc(e)}\n\nПопробуйте ещё раз.',kb([[('← Отмена','input:cancel')]]),state)

# ---------- item and list screens ----------
async def show_question_item(bot,chat,uid,state,key):
    r=execute('SELECT question_key,question_text,display_text FROM questions WHERE question_key=?',(key,),one=True)
    if not r: return await show_questions_menu(bot,chat,uid,state)
    rows=[[('Изменить вопрос','q:edittext:'+key),('Изменить вывод','q:editdisplay:'+key)],[('Удалить','q:delete:'+key)]]+nav('q:list:0')
    await safe_edit(bot,chat,uid,f'<b>{esc(r["question_key"])}</b>\n\nВопрос: {esc(r["question_text"])}\n\nОтображение: {esc(r["display_text"])}',kb(rows),state)
async def show_text_item(bot,chat,uid,state,key):
    r=execute('SELECT text_key,text_content,parse_mode,description FROM texts WHERE text_key=?',(key,),one=True)
    if not r: return await show_texts_menu(bot,chat,uid,state)
    rows=[[('Изменить текст','txt:edit:'+key),('Формат','txt:editformat:'+key)]]+nav('txt:list:0')
    await safe_edit(bot,chat,uid,f'<b>{esc(r["description"] or key)}</b>\nКлюч: <code>{esc(key)}</code>\nФормат: {esc(r["parse_mode"])}\n\n{esc(r["text_content"][:2500])}',kb(rows),state)
async def show_blacklist_item(bot,chat,uid,state,target):
    r=execute('SELECT user_id,tag,reason,added_by,added_at FROM blacklist WHERE user_id=?',(target,),one=True)
    if not r: return await show_blacklist(bot,chat,uid,state)
    await safe_edit(bot,chat,uid,f'<b>Пользователь в чёрном списке</b>\n{user_mention(target,username_label(r["tag"],target))}\nID: <code>{target}</code>\nПричина: {esc(r["reason"])}\nДобавил: {r["added_by"]}\nДата: {esc(r["added_at"])}',kb([[('Удалить из списка','bl:delete:'+str(target))]]+nav('bl:list:0')),state)
async def show_template_item(bot,chat,uid,state,tid):
    r=execute('SELECT id,template_text FROM decline_templates WHERE id=?',(tid,),one=True)
    if not r: return await show_templates(bot,chat,uid,state)
    await safe_edit(bot,chat,uid,f'<b>Шаблон #{tid}</b>\n\n{esc(r["template_text"])}',kb([[('Удалить','tpl:delete:'+str(tid))]]+nav('tpl:list:0')),state)
async def show_search_results(bot,chat,uid,state,query,page=0):
    q=query.strip(); safe=q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
    if q.isdigit(): items=execute('SELECT id,user,tag,state,reviewed_by,reviewed_at FROM applications WHERE user=? OR id=? ORDER BY id DESC',(int(q),int(q)),all=True) or []
    elif q.startswith('@'): items=execute('SELECT id,user,tag,state,reviewed_by,reviewed_at FROM applications WHERE tag=? ORDER BY id DESC',(q[1:],),all=True) or []
    else: items=execute("SELECT id,user,tag,state,reviewed_by,reviewed_at FROM applications WHERE tag LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT 50",('%'+safe+'%',),all=True) or []
    size=7; pages=max(1,(len(items)+size-1)//size); page=max(0,min(int(page),pages-1))
    await state.update_data(search_query=q,search_page=page)
    visible=items[page*size:(page+1)*size]
    rows=[]
    for r in visible:
        reviewer,reviewed_at=reviewer_for_application(r)
        label=f'#{r["id"]} · {username_label(r["tag"],r["user"])} · {r["state"]}'
        if reviewer: label+=f' · {admin_label(reviewer)}'
        rows.append([(label[:60],f'app:open:{r["user"]}')])
    pg=pager(page,len(items),size,'appsearch')
    if pg: rows.append(pg)
    rows+=nav()
    body='\n'.join(user_mention(r['user'],username_label(r['tag'],r['user'])) for r in visible)
    await safe_edit(bot,chat,uid,f'<b>Результаты поиска</b>\nЗапрос: {esc(q)}\nНайдено: {len(items)}\nСтраница {page+1}/{pages}\n\n{body}',kb(rows),state)
async def show_application_card(bot,chat,uid,state,target):
    r=app_row(target)
    if not r: return await safe_edit(bot,chat,uid,'Заявка не найдена.',kb(nav('app:review:0')),state)
    answers=json.loads(r['answers'] or '{}')
    if r['state']=='sended': await state.update_data(current_review_uid=target)
    qs=questions(); lines=[f'<b>Заявка #{r["id"]}</b>',f'Пользователь: {user_mention(target,username_label(r["tag"],target))}',f'ID: <code>{target}</code>',f'Статус: {esc(r["state"])}']
    reviewer,reviewed_at=reviewer_for_application(r)
    if reviewer:
        lines.append(f'Рассмотрел: {user_mention(reviewer,admin_label(reviewer))}')
        if reviewed_at: lines.append(f'Дата решения: {esc(reviewed_at)}')
    for key,q,display in qs:
        lines.append(esc(display.replace('{answer}',str(answers.get(key,'Не заполнено')))))
    lock=r['locked_by']; rows=[]
    if lock and lock!=uid: lines.append(f'\n[!] Заблокировано администратором <code>{lock}</code>')
    elif r['state']=='sended': rows=[[('Одобрить','app:approve:'+str(target)),('Отклонить','app:decline:'+str(target))],[('Заблокировать','app:lock:'+str(target)),('Пропустить','app:skipreview')]]
    elif r['state']=='applied': rows=[[('Повторно отправить ссылку','app:resend:'+str(target))]]
    elif r['state']=='canceled': rows=[[('Дать ещё одну попытку','app:retry:'+str(target))]]
    rows+=nav('app:search')
    await safe_edit(bot,chat,uid,'\n\n'.join(lines),kb(rows),state)
async def review_next(bot,chat,uid,state):
    d=await state.get_data(); previous=d.get('current_review_uid')
    r=execute("SELECT user FROM applications WHERE state='sended' AND user<>COALESCE(?, -1) ORDER BY created_at,id LIMIT 1",(previous,),one=True)
    if not r: return await safe_edit(bot,chat,uid,'[ok] Заявок на рассмотрении нет.',kb(nav()),state)
    target=r['user']; ok,owner=lock_app(target,uid)
    if not ok: return await safe_edit(bot,chat,uid,f'[!] Заявка занята администратором <code>{owner}</code>.',kb([[('Следующая','app:review:0')]]+nav()),state)
    await show_application_card(bot,chat,uid,state,target)
async def finish_decline(bot,chat,uid,state,target,reason,final=False):
    r=app_row(target)
    if not r or r['state']!='sended': return await safe_edit(bot,chat,uid,'Заявка уже обработана или не найдена.',kb(nav()),state)
    if r['locked_by'] not in (None,uid): return await safe_edit(bot,chat,uid,'[!] Заявку удерживает другой администратор.',kb(nav()),state)
    save_status='canceled_final' if final else 'canceled'
    with transaction(immediate=True) as c:
        c.execute('UPDATE applications SET state=?,comment=?,reviewed_by=?,reviewed_at=?,locked_by=NULL,locked_at=NULL WHERE user=? AND state=\'sended\' AND (locked_by IS NULL OR locked_by=?)',(save_status,reason,uid,datetime.now(timezone.utc).isoformat(),target,uid))
        if c.total_changes==0: raise ValueError('Заявка уже была обработана.')
    save_history(target,r['tag'],save_status,reason,r['answers'],uid)
    await notify_admins(bot,target,uid,'отклонил')
    try: await bot.send_message(target,strip_emoji(('Окончательный отказ: ' if final else 'Заявка отклонена: ')+esc(reason)),parse_mode=ParseMode.HTML)
    except Exception: pass
    await review_next(bot,chat,uid,state)

async def send_approved_to_group(bot:Bot,target:int,admin_id:int):
    if not APPROVED_GROUP_ID: return
    r=app_row(target)
    if not r: return
    answers=json.loads(r['answers'] or '{}')
    lines=[f'Пользователь: {user_mention(target,username_label(r["tag"],target))}',f'ID: <code>{target}</code>',f'Одобрил: {user_mention(admin_id,admin_label(admin_id))}',f'Время: {datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")}']
    for key,_,display in questions(): lines.append(esc(display.replace('{answer}',str(answers.get(key,'Не заполнено')))))
    try: await bot.send_message(APPROVED_GROUP_ID,strip_emoji('\n\n'.join(lines)),message_thread_id=APPROVED_TOPIC_ID or None,parse_mode=ParseMode.HTML,disable_notification=True)
    except Exception: log.exception('approved group notification failed')

async def admin_recipients()->list[int]:
    rows=execute('SELECT user_id FROM admins',all=True) or []
    return sorted({int(r['user_id']) for r in rows}|set(MAIN_ADMINS))

async def notify_new_application(bot:Bot,target:int):
    """Notify every configured admin when a completed application is submitted."""
    r=app_row(target)
    if not r: return
    count=execute("SELECT COUNT(*) n FROM applications WHERE state='sended'",one=True)['n']
    answers=json.loads(r['answers'] or '{}')
    lines=[f'<b>Новая заявка #{r["id"]}</b>',f'Пользователь: {user_mention(target,username_label(r["tag"],target))}',f'ID: <code>{target}</code>',f'На рассмотрении: <b>{count}</b>']
    for key,_,display in questions():
        value=str(answers.get(key,'Не заполнено'))
        if len(value)>100: value=value[:100]+'…'
        lines.append(esc(display.replace('{answer}',value)))
    markup=kb([[('Открыть заявку',f'app:open:{target}')]])
    for admin_id in await admin_recipients():
        try:
            await bot.send_message(admin_id,strip_emoji('\n\n'.join(lines)),parse_mode=ParseMode.HTML,reply_markup=markup,disable_web_page_preview=True)
        except TelegramForbiddenError:
            log.warning('New-application notification not delivered: admin %s has not started the bot or blocked it',admin_id)
        except Exception:
            log.exception('New-application notification failed for admin %s',admin_id)
        await asyncio.sleep(0.04)

async def notify_admins(bot:Bot,target:int,admin_id:int,action:str):
    r=app_row(target)
    app_id=r['id'] if r else '?'
    actor=admin_label(admin_id)
    text=f'<b>Заявка #{app_id}</b>\nАдминистратор {user_mention(admin_id,actor)} {esc(action)} заявку пользователя {user_mention(target,username_label(r["tag"] if r else None,target))}.'
    for other in await admin_recipients():
        if other==admin_id: continue
        try: await bot.send_message(other,strip_emoji(text),parse_mode=ParseMode.HTML,disable_web_page_preview=True)
        except TelegramForbiddenError: log.warning('Review notification not delivered to admin %s',other)
        except Exception: log.exception('Review notification failed for admin %s',other)
        await asyncio.sleep(0.04)

# ---------- callbacks: one dispatcher with namespace routing ----------
@router.callback_query()
async def callback_router(call:CallbackQuery,state:FSMContext):
    await call.answer()
    uid=call.from_user.id; chat=call.message.chat.id; bot=call.bot; raw=call.data or ''; p=raw.split(':'); ns=p[0] if p else ''
    if chat!=uid: return
    touch_user(call.from_user)
    await remove_legacy_keyboard(bot,chat,uid)
    # Reject callbacks while input is expected, except cancel.
    if ns=='input' and len(p)>1 and p[1]=='cancel':
        d=await state.get_data(); ctx=d.get('input_context') or {}; ret=ctx.get('return_to')
        await state.set_state(None); await state.update_data(input_context=None)
        if ret=='questions': return await show_questions_menu(bot,chat,uid,state)
        if ret=='texts': return await show_texts_menu(bot,chat,uid,state)
        if ret=='admins': return await show_admins(bot,chat,uid,state)
        if ret=='blacklist': return await show_blacklist(bot,chat,uid,state)
        if ret=='templates': return await show_templates(bot,chat,uid,state)
        if ret=='invites': return await show_invites(bot,chat,uid,state)
        if ret=='review': return await review_next(bot,chat,uid,state)
        if ret=='application_question': return await show_app_question(bot,chat,uid,state)
        return await home(bot,chat,uid,state)
    if ns=='nav' and len(p)>1 and p[1]=='back':
        d=await back_screen(state); scr=d.get('screen')
        if scr=='home': return await home(bot,chat,uid,state)
        if scr=='questions': return await show_questions_menu(bot,chat,uid,state)
        if scr=='question_item': return await show_question_item(bot,chat,uid,state,d.get('payload',{}).get('key'))
        if scr=='texts': return await show_texts_menu(bot,chat,uid,state)
        if scr=='text_item': return await show_text_item(bot,chat,uid,state,d.get('payload',{}).get('key'))
        if scr=='admins': return await show_admins(bot,chat,uid,state)
        if scr=='admin_item': return await show_admins(bot,chat,uid,state)
        if scr=='blacklist': return await show_blacklist(bot,chat,uid,state)
        if scr=='blacklist_item': return await show_blacklist_item(bot,chat,uid,state,d.get('payload',{}).get('target'))
        if scr=='templates': return await show_templates(bot,chat,uid,state)
        if scr=='template_item': return await show_templates(bot,chat,uid,state)
        if scr=='invites': return await show_invites(bot,chat,uid,state)
        if scr=='search_results':
            d=await state.get_data(); return await show_search_results(bot,chat,uid,state,d.get('search_query',''),int(d.get('search_page',0)))
        return await home(bot,chat,uid,state)
    if ns=='noop': return
    if ns=='menu':
        if p[1]=='home': return await home(bot,chat,uid,state)
        if p[1]=='server': return await safe_edit(bot,chat,uid,esc(get_text('server_info_text')),kb(nav()),state)
        if p[1]=='closed': return await safe_edit(bot,chat,uid,esc(get_text('applications_closed_text')),kb(nav()),state)
        if p[1]=='status':
            r=app_row(uid); status=r['state'] if r else 'created'; reason=r['comment'] if r else None
            label={'created':'Заявка не отправлена','sended':'Заявка рассматривается','applied':'Заявка одобрена','canceled':'Заявка отклонена. Можно обратиться к администрации.','canceled_final':'Получен окончательный отказ.'}.get(status,status)
            details=[f'<b>Информация о заявке</b>',esc(label)]
            if r:
                reviewer,reviewed_at=reviewer_for_application(r)
                if reviewer:
                    details.append(f'Рассмотрел: {user_mention(reviewer,admin_label(reviewer))}')
                    if reviewed_at: details.append(f'Дата решения: {esc(reviewed_at)}')
            if reason: details.append(esc(reason))
            return await safe_edit(bot,chat,uid,'\n'.join(details),kb(nav()),state)
    if ns=='appsearch':
        if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
        d=await state.get_data(); return await show_search_results(bot,chat,uid,state,d.get('search_query',''),int(p[2]))
    if ns=='app':
        action=p[1]
        if action=='resume': return await show_app_question(bot,chat,uid,state)
        if action=='start':
            if auth.is_admin(uid): return await denied(bot,chat,uid,state)
            if execute('SELECT 1 FROM blacklist WHERE user_id=?',(uid,),one=True): return await safe_edit(bot,chat,uid,'[x] Подача заявки недоступна. Обратитесь к администрации.',kb(nav()),state)
            opened=execute('SELECT is_open FROM applications_settings LIMIT 1',one=True)
            if opened and not opened['is_open']: return await safe_edit(bot,chat,uid,esc(get_text('applications_closed_text')),kb(nav()),state)
            r=app_row(uid)
            if r and r['state'] in ('sended','applied','canceled_final'): return await safe_edit(bot,chat,uid,'Заявка уже существует. Откройте информацию о заявке.',kb(nav()),state)
            execute('INSERT OR IGNORE INTO applications(user,tag,state,answers) VALUES(?,?,\'created\',\'{}\')',(uid,call.from_user.username))
            await state.update_data(app_index=0,app_answers={}); await push_screen(state,'application',{})
            return await show_app_question(bot,chat,uid,state)
        if action in ('skip','prev'):
            d=await state.get_data(); idx=int(d.get('app_index',0)); answers=d.get('app_answers',{})
            if action=='skip' and idx < len(questions()): answers[questions()[idx][0]]='Не заполнено'; idx+=1
            elif action=='prev': idx=max(0,idx-1)
            await state.update_data(app_index=idx,app_answers=answers); return await show_app_question(bot,chat,uid,state)
        if action=='cancel': await state.clear(); return await home(bot,chat,uid,state)
        if action=='submit':
            d=await state.get_data(); answers=d.get('app_answers',{}); qs=questions()
            if len(answers)<len(qs): return await show_app_question(bot,chat,uid,state)
            execute("UPDATE applications SET state='sended',answers=?,tag=?,created_at=CURRENT_TIMESTAMP,comment=NULL,reviewed_by=NULL,reviewed_at=NULL WHERE user=?",(json.dumps(answers,ensure_ascii=False),call.from_user.username,uid))
            await state.clear()
            await safe_edit(bot,chat,uid,esc(get_text('application_sent_text')),kb([[('Информация о заявке','menu:status')]]+nav()),state)
            await notify_new_application(bot,uid)
            return
        if action=='review':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            await state.update_data(current_review_uid=None)
            return await review_next(bot,chat,uid,state)
        if action=='skipreview':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            d=await state.get_data(); previous=d.get('current_review_uid')
            if previous: unlock_app(int(previous),uid)
            return await review_next(bot,chat,uid,state)
        if action=='search':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            await set_input(state,'application_search','query','text','search',{})
            return await safe_edit(bot,chat,uid,'Введите ID пользователя, @username или часть username:',kb([[('← Отмена','input:cancel')]]),state)
        if action=='searchpage':
            d=await state.get_data(); return await show_search_results(bot,chat,uid,state,d.get('search_query',''),max(0,int(p[2])))
        if action=='open':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            target=int(p[2]); d=await state.get_data(); await push_screen(state,'search_results',{'page':int(d.get('search_page',0))}); return await show_application_card(bot,chat,uid,state,target)
        if action=='lock':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            target=int(p[2]); ok,owner=lock_app(target,uid)
            if not ok: return await safe_edit(bot,chat,uid,f'[!] Заявка заблокирована администратором <code>{owner}</code>.',kb(nav()),state)
            return await show_application_card(bot,chat,uid,state,target)
        if action=='approve':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            target=int(p[2]); r=app_row(target)
            if not r or r['state']!='sended' or r['locked_by'] not in (None,uid): return await safe_edit(bot,chat,uid,'[!] Заявка уже обработана или занята.',kb(nav()),state)
            with transaction(immediate=True) as c:
                cur=c.execute("UPDATE applications SET state='applied',reviewed_by=?,reviewed_at=?,locked_by=NULL,locked_at=NULL WHERE user=? AND state='sended' AND (locked_by IS NULL OR locked_by=?)",(uid,datetime.now(timezone.utc).isoformat(),target,uid))
                if cur.rowcount!=1: raise ValueError('Заявка уже обработана.')
            save_history(target,r['tag'],'applied',None,r['answers'],uid)
            link=await invites.generate(bot,target,remember=True)
            if link:
                execute('UPDATE applications SET invite_link=? WHERE user=?',(link,target))
                try: await bot.send_message(target,strip_emoji(f'Ваша заявка одобрена. Ссылка для вступления: {esc(link)}'),parse_mode=ParseMode.HTML)
                except Exception: pass
            else:
                try: await bot.send_message(target,'Ваша заявка одобрена. Администрация сообщит ссылку отдельно.')
                except Exception: pass
            await send_approved_to_group(bot,target,uid)
            await notify_admins(bot,target,uid,'одобрил')
            return await review_next(bot,chat,uid,state)
        if action=='decline':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            target=int(p[2]); r=execute('SELECT id,template_text FROM decline_templates ORDER BY id',all=True) or []
            rows=[[(f'#{x["id"]} · {x["template_text"][:24]}',f'app:declinetpl:{target}:{x["id"]}')] for x in r]
            rows += [[('Свой текст','app:declinetext:'+str(target)),('Окончательный отказ','app:declinefinal:'+str(target))]]+nav('app:open:'+str(target))
            return await safe_edit(bot,chat,uid,'<b>Выберите причину отказа</b>',kb(rows),state)
        if action=='declinetpl':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            target,tid=int(p[2]),int(p[3]); r=execute('SELECT template_text FROM decline_templates WHERE id=?',(tid,),one=True)
            if not r: return await safe_edit(bot,chat,uid,'Шаблон не найден.',kb(nav()),state)
            return await finish_decline(bot,chat,uid,state,target,r['template_text'])
        if action in ('declinetext','declinefinal'):
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            target=int(p[2]); await set_input(state,'decline_reason','reason','text','review',{'target':target,'final':action=='declinefinal'})
            return await safe_edit(bot,chat,uid,'Введите причину отказа:',kb([[('← Отмена','input:cancel')]]),state)
        if action=='resend':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            target=int(p[2]); r=app_row(target)
            if not r or r['state']!='applied': return await safe_edit(bot,chat,uid,'Заявка не одобрена.',kb(nav()),state)
            link=r['invite_link'] or await invites.generate(bot,target,remember=True)
            if link: execute('UPDATE applications SET invite_link=? WHERE user=?',(link,target))
            try: await bot.send_message(target,strip_emoji(f'Ваша ссылка для вступления: {esc(link)}'),parse_mode=ParseMode.HTML) if link else None
            except Exception: pass
            return await show_application_card(bot,chat,uid,state,target)
        if action=='retry':
            if not auth.is_admin(uid): return await denied(bot,chat,uid,state)
            target=int(p[2]); r=app_row(target)
            if not r or r['state']!='canceled': return await safe_edit(bot,chat,uid,'Повторная попытка недоступна.',kb(nav()),state)
            save_history(target,r['tag'],'canceled',r['comment'],r['answers'],uid)
            execute("UPDATE applications SET state='created',comment=NULL,answers='{}',locked_by=NULL,locked_at=NULL WHERE user=? AND state='canceled'",(target,))
            try: await bot.send_message(target,'Администрация разрешила подать заявку повторно.')
            except Exception: pass
            return await show_application_card(bot,chat,uid,state,target)
        if action=='settings':
            if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
            r=execute('SELECT is_open FROM applications_settings LIMIT 1',one=True); opened=bool(r['is_open']) if r else True
            return await safe_edit(bot,chat,uid,f'<b>Подача заявок</b>\nСейчас: {"открыта" if opened else "закрыта"}',kb([[('Закрыть' if opened else 'Открыть','app:toggle')]]+nav()),state)
        if action=='toggle':
            if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
            execute('UPDATE applications_settings SET is_open=CASE WHEN is_open=1 THEN 0 ELSE 1 END,updated_by=?',(uid,)); return await callback_router_refresh_settings(bot,chat,uid,state)
    if ns=='q':
        if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
        a=p[1]
        if a=='list': await push_screen(state,'questions',{}); return await show_questions_menu(bot,chat,uid,state,int(p[2]))
        if a=='page': return await show_questions_menu(bot,chat,uid,state,int(p[2]))
        if a=='item': await push_screen(state,'question_item',{'key':p[2]}); return await show_question_item(bot,chat,uid,state,p[2])
        if a=='add': await set_input(state,'question_add_key','key','text','questions',{}); return await safe_edit(bot,chat,uid,'Введите уникальный ключ вопроса (латиница, цифры, _):',kb([[('← Отмена','input:cancel')]]),state)
        if a in ('edittext','editdisplay'):
            key=p[2]; col='question_text' if a=='edittext' else 'display_text'
            if not execute('SELECT 1 FROM questions WHERE question_key=?',(key,),one=True): return await show_questions_menu(bot,chat,uid,state)
            await set_input(state,'question_edit_text' if col=='question_text' else 'question_edit_display','value','text','questions',{'key':key})
            return await safe_edit(bot,chat,uid,'Введите новое значение:',kb([[('← Отмена','input:cancel')]]),state)
        if a=='delete':
            key=p[2]; return await safe_edit(bot,chat,uid,f'Подтвердите удаление вопроса <code>{esc(key)}</code>.',kb([[('Да','q:confirmdelete:'+key),('Нет','q:item:'+key)]]),state)
        if a=='confirmdelete': execute('DELETE FROM questions WHERE question_key=?',(p[2],)); return await show_questions_menu(bot,chat,uid,state)
    if ns=='txt':
        if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
        a=p[1]
        if a=='list': await push_screen(state,'texts',{}); return await show_texts_menu(bot,chat,uid,state,int(p[2]))
        if a=='page': return await show_texts_menu(bot,chat,uid,state,int(p[2]))
        if a=='item': await push_screen(state,'text_item',{'key':p[2]}); return await show_text_item(bot,chat,uid,state,p[2])
        if a in ('edit','editformat'):
            key=p[2]
            if not execute('SELECT 1 FROM texts WHERE text_key=?',(key,),one=True): return await show_texts_menu(bot,chat,uid,state)
            await set_input(state,'text_edit' if a=='edit' else 'text_format','value','text','texts',{'key':key})
            return await safe_edit(bot,chat,uid,'Введите новый текст:' if a=='edit' else 'Введите формат: HTML, Markdown или MarkdownV2',kb([[('← Отмена','input:cancel')]]),state)
        if a=='format': return await show_texts_menu(bot,chat,uid,state)
    if ns=='adm':
        if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
        a=p[1]
        if a=='list': await push_screen(state,'admins',{}); return await show_admins(bot,chat,uid,state,int(p[2]))
        if a=='page': return await show_admins(bot,chat,uid,state,int(p[2]))
        if a=='item':
            target=int(p[2]); await push_screen(state,'admin_item',{'target':target})
            info=execute('SELECT * FROM admins WHERE user_id=?',(target,),one=True)
            label=(info['display_name'] if info and info['display_name'] else (('@'+info['tag']) if info and info['tag'] else str(target)))
            details=f'\nUsername: @{esc(info["tag"])}' if info and info['tag'] else ''
            if auth.is_main_admin(target):
                return await safe_edit(bot,chat,uid,f'<b>Главный администратор</b>\n{user_mention(target,label)}\nID: <code>{target}</code>{details}\n\nГлавных администраторов нельзя удалить.',kb(nav()),state)
            return await safe_edit(bot,chat,uid,f'<b>Администратор</b>\n{user_mention(target,label)}\nID: <code>{target}</code>{details}',kb([[('Удалить','adm:removeconfirm:'+str(target))]]+nav()),state)
        if a in ('add','remove'):
            await set_input(state,'admin_add' if a=='add' else 'admin_remove','id','int','admins',{})
            return await safe_edit(bot,chat,uid,'Введите Telegram ID пользователя:',kb([[('← Отмена','input:cancel')]]),state)
        if a=='removeconfirm':
            target=int(p[2])
            if auth.is_main_admin(target): return await safe_edit(bot,chat,uid,'Главного администратора удалить нельзя.',kb(nav()),state)
            return await safe_edit(bot,chat,uid,f'Удалить администратора {target}?',kb([[('Да','adm:delete:'+str(target)),('Нет','adm:list:0')]]+nav()),state)
        if a=='delete':
            target=int(p[2])
            if auth.is_main_admin(target): return await safe_edit(bot,chat,uid,'Главного администратора удалить нельзя.',kb(nav()),state)
            auth.remove_admin(target); return await show_admins(bot,chat,uid,state)
    if ns=='bl':
        if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
        a=p[1]
        if a=='list': await push_screen(state,'blacklist',{}); return await show_blacklist(bot,chat,uid,state,int(p[2]))
        if a=='page': return await show_blacklist(bot,chat,uid,state,int(p[2]))
        if a=='item': await push_screen(state,'blacklist_item',{'target':int(p[2])}); return await show_blacklist_item(bot,chat,uid,state,int(p[2]))
        if a=='add': await set_input(state,'blacklist_add_user','user','text','blacklist',{}); return await safe_edit(bot,chat,uid,'Введите ID пользователя или @username:',kb([[('← Отмена','input:cancel')]]),state)
        if a=='search': await set_input(state,'blacklist_search','query','text','blacklist',{}); return await safe_edit(bot,chat,uid,'Введите ID или часть username:',kb([[('← Отмена','input:cancel')]]),state)
        if a=='delete': return await safe_edit(bot,chat,uid,f'Удалить пользователя <code>{p[2]}</code> из чёрного списка?',kb([[('Да','bl:confirmdelete:'+p[2]),('Нет','bl:item:'+p[2])]]),state)
        if a=='confirmdelete': execute('DELETE FROM blacklist WHERE user_id=?',(int(p[2]),)); return await show_blacklist(bot,chat,uid,state)
    if ns=='tpl':
        if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
        a=p[1]
        if a=='list': await push_screen(state,'templates',{}); return await show_templates(bot,chat,uid,state,int(p[2]))
        if a=='page': return await show_templates(bot,chat,uid,state,int(p[2]))
        if a=='item': await push_screen(state,'template_item',{'target':int(p[2])}); return await show_template_item(bot,chat,uid,state,int(p[2]))
        if a=='add': await set_input(state,'template_add','text','text','templates',{}); return await safe_edit(bot,chat,uid,'Введите текст шаблона:',kb([[('← Отмена','input:cancel')]]),state)
        if a=='delete': return await safe_edit(bot,chat,uid,f'Удалить шаблон #{p[2]}?',kb([[('Да','tpl:confirmdelete:'+p[2]),('Нет','tpl:list:0')]]),state)
        if a=='confirmdelete': execute('DELETE FROM decline_templates WHERE id=?',(int(p[2]),)); return await show_templates(bot,chat,uid,state)
    if ns=='inv':
        if p[1]=='get':
            if app_status(uid)!='applied': return await safe_edit(bot,chat,uid,'Ссылка доступна только после одобрения заявки.',kb(nav()),state)
            r=app_row(uid); link=r['invite_link'] if r else None
            if not link: link=await invites.generate(bot,uid); execute('UPDATE applications SET invite_link=? WHERE user=?',(link,uid))
            return await safe_edit(bot,chat,uid,f'Ваша ссылка: {esc(link or "Ссылка пока недоступна. Обратитесь к администрации.")}',kb(nav()),state)
        if not auth.is_main_admin(uid): return await denied(bot,chat,uid,state)
        a=p[1]
        if a=='menu': await push_screen(state,'invites',{}); return await show_invites(bot,chat,uid,state)
        if a=='toggle': execute('UPDATE invite_settings SET is_enabled=CASE WHEN is_enabled=1 THEN 0 ELSE 1 END,updated_by=?',(uid,)); return await show_invites(bot,chat,uid,state)
        if a=='auto': execute('UPDATE invite_settings SET auto_approve=CASE WHEN auto_approve=1 THEN 0 ELSE 1 END,updated_by=?',(uid,)); return await show_invites(bot,chat,uid,state)
        if a=='chat': await set_input(state,'invite_chat','chat','chat_id','invites',{}); return await safe_edit(bot,chat,uid,'Введите числовой ID чата. Бот должен быть администратором:',kb([[('← Отмена','input:cancel')]]),state)
        if a=='generate':
            link=await invites.generate(bot,None,remember=True); return await safe_edit(bot,chat,uid,f'Ссылка: {esc(link or "Не удалось создать ссылку. Проверьте настройки и права бота.")}',kb(nav('inv:menu')),state)
        if a=='show': return await safe_edit(bot,chat,uid,f'Последняя ссылка: {esc(invites.settings()["last_link"] or "нет")}',kb(nav('inv:menu')),state)

async def callback_router_refresh_settings(bot,chat,uid,state):
    r=execute('SELECT is_open FROM applications_settings LIMIT 1',one=True); opened=bool(r['is_open']) if r else True
    return await safe_edit(bot,chat,uid,f'<b>Подача заявок</b>\nСейчас: {"открыта" if opened else "закрыта"}',kb([[('Закрыть' if opened else 'Открыть','app:toggle')]]+nav()),state)

@router.message(CommandStart())
async def start(message:Message,state:FSMContext):
    if message.chat.type!='private': return
    await state.clear()
    uid=message.from_user.id; touch_user(message.from_user)
    await remove_legacy_keyboard(message.bot,message.chat.id,uid)
    if not auth.is_admin(uid):
        execute('INSERT OR IGNORE INTO applications(user,tag,state,answers) VALUES(?,?,\'created\',\'{}\')',(uid,message.from_user.username))
    row=get_session(message.chat.id,uid)
    if row and row['anchor_message_id']:
        try: await message.bot.delete_message(message.chat.id,row['anchor_message_id'])
        except Exception: pass
    await home(message.bot,message.chat.id,uid,state)
    # Delete the /start command so the private chat stays visually clean.
    await delete_input(message)

@router.message(Command('approve'))
async def manual_approve(message:Message):
    if not message.from_user or not auth.is_admin(message.from_user.id): return
    touch_user(message.from_user)
    parts=(message.text or '').split()
    if len(parts)!=2 or not parts[1].isdigit():
        await safe_edit(message.bot,message.chat.id,message.from_user.id,'Использование: /approve user_id',kb(nav()))
        return
    target=int(parts[1]); r=app_row(target); s=invites.settings()
    if not r or r['state']!='applied' or not s['chat_id']:
        await safe_edit(message.bot,message.chat.id,message.from_user.id,'Пользователь не найден, заявка не одобрена или чат приглашений не настроен.',kb(nav()))
        return
    try:
        await message.bot.approve_chat_join_request(chat_id=int(s['chat_id']),user_id=target)
        await safe_edit(message.bot,message.chat.id,message.from_user.id,f'[ok] Вступление одобрено для {user_mention(target,username_label(r["tag"],target))}.',kb(nav()))
    except Exception as e:
        await safe_edit(message.bot,message.chat.id,message.from_user.id,f'[!] Не удалось одобрить вступление: {esc(e)}',kb(nav()))
    await delete_input(message)

@router.message(Command('get_chat_id'))
async def get_chat_id(message:Message):
    if message.from_user: touch_user(message.from_user)
    if message.chat.type=='private':
        await safe_edit(message.bot,message.chat.id,message.from_user.id,'Команда предназначена для группы.',kb(nav()))
    else:
        try: await message.answer(f'<code>{message.chat.id}</code>',parse_mode=ParseMode.HTML)
        except Exception: pass

@router.message(Form.waiting_for_input)
async def input_handler(message:Message,state:FSMContext):
    if message.chat.type!='private' or not message.from_user: return
    touch_user(message.from_user); await remove_legacy_keyboard(message.bot,message.chat.id,message.from_user.id)
    banned,remaining=check_spam(message.from_user.id)
    if banned:
        await delete_input(message)
        row=get_session(message.chat.id,message.from_user.id)
        if row: await safe_edit(message.bot,message.chat.id,message.from_user.id,f'[!] Слишком много запросов. Повторите через {remaining} сек.',kb(nav()),state)
        return
    await handle_input(message,state)

@router.message(F.text)
async def stray_text(message:Message,state:FSMContext):
    if message.chat.type!='private' or not message.from_user: return
    touch_user(message.from_user); await remove_legacy_keyboard(message.bot,message.chat.id,message.from_user.id)
    # Ignore unrelated chat text. The only supported free-text flow is the universal input state.
    if auth.is_admin(message.from_user.id): return
    banned,remaining=check_spam(message.from_user.id)
    if banned:
        await delete_input(message); return

@router.chat_join_request()
async def join_request(req:ChatJoinRequest,bot:Bot):
    touch_user(req.from_user)
    s=invites.settings()
    if not s['chat_id'] or str(req.chat.id)!=str(s['chat_id']): return
    try:
        member=await bot.get_chat_member(req.chat.id,req.from_user.id)
        if member.status in ('member','administrator','creator'):
            await req.decline(); return
    except Exception: log.exception('could not verify join-request membership')
    row=execute('SELECT state FROM applications WHERE user=?',(req.from_user.id,),one=True)
    if not row or row['state']!='applied':
        try: await req.decline()
        except Exception: log.exception('join request decline failed')
        return
    if s['auto']:
        try: await req.approve()
        except Exception: log.exception('join request approval failed')
        else:
            try: await bot.send_message(req.from_user.id,'Вы приняты в Lavender Park.')
            except Exception: pass

async def main():
    if not TOKEN: raise RuntimeError('Не задан API_TOKEN')
    db.init_db(); db.backup_database()
    bot=Bot(TOKEN,session=AiohttpSession(timeout=30))
    dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router)
    try:
        await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types(),close_bot_session=False)
    finally:
        await bot.session.close()

if __name__=='__main__': asyncio.run(main())
