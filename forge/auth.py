"""Remembered browser authorization. Browser secrets are not stored in JS storage."""
from __future__ import annotations
import hashlib
import secrets
import threading
import time

class BrowserSessions:
    lifetime = 90 * 86400

    def __init__(self, store, port):
        self.store=store
        self.cookie=f'forge_browser_{port}'
        self.lock=threading.RLock()
        self.tickets={}

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def ticket(self):
        with self.lock:
            now=time.time()
            self.tickets={k:v for k,v in self.tickets.items() if v>now}
            value=secrets.token_urlsafe(32)
            self.tickets[self.digest(value)]=now+120
            return value

    def consume(self,value):
        if not isinstance(value,str) or len(value)>256:
            return False
        with self.lock:
            expires=self.tickets.pop(self.digest(value),0)
        return expires>time.time()

    def lookup(self,value):
        if not value or len(value)>256:
            return None
        with self.lock:
            item=self.store.get('browser_sessions',{}).get(self.digest(value))
            return item if item and item['expires']>time.time() else None

    def create(self,response,previous=''):
        with self.lock:
            now=time.time()
            sessions={k:v for k,v in self.store.get('browser_sessions',{}).items() if v['expires']>now}
            if previous:
                sessions.pop(self.digest(previous),None)
            # Keep the last 20 authorized browser profiles; no fingerprinting/UA logging.
            if len(sessions)>=20:
                oldest=min(sessions,key=lambda k:sessions[k]['expires'])
                sessions.pop(oldest)
            value=secrets.token_urlsafe(48)
            item={'csrf':secrets.token_urlsafe(32),'expires':now+self.lifetime}
            sessions[self.digest(value)]=item
            self.store.set('browser_sessions',sessions)
        # HTTP only on exact IPv4 loopback. Secure is not usable portably over this HTTP origin.
        response.set_cookie(self.cookie,value,max_age=self.lifetime,httponly=True,samesite='strict',path='/',secure=False)
        return item

    def forget(self,value,response):
        with self.lock:
            sessions=self.store.get('browser_sessions',{})
            sessions.pop(self.digest(value or ''),None)
            self.store.set('browser_sessions',sessions)
        response.delete_cookie(self.cookie,path='/',httponly=True,samesite='strict')
