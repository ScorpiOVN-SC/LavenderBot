from collections import defaultdict, deque
from time import monotonic
from .db import execute

MAX_PER_SECOND=3
MAX_PER_MINUTE=25
BAN_SECONDS=300
_hits=defaultdict(deque)
_bans={}

def check_spam(uid:int)->tuple[bool,int]:
    now=monotonic()
    if _bans.get(uid,0)>now: return True,int(_bans[uid]-now)
    q=_hits[uid]
    while q and now-q[0]>60: q.popleft()
    q.append(now)
    per_sec=sum(now-t<=1 for t in q)
    if per_sec>MAX_PER_SECOND or len(q)>MAX_PER_MINUTE:
        _bans[uid]=now+BAN_SECONDS
        q.clear()
        return True,BAN_SECONDS
    return False,0
