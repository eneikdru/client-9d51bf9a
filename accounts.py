"""Local identities, revocable sessions, recovery and first-master onboarding."""
import hashlib
import hmac
import secrets
import sqlite3
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

import scheduling


class Credentials(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=3,max_length=32,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$')
    password:SecretStr

    @field_validator('username')
    @classmethod
    def normalized(cls,value):
        return value.lower()

    @field_validator('password')
    @classmethod
    def password_size(cls,value):
        if not 12<=len(value.get_secret_value())<=128:
            raise ValueError('Use a password of 12–128 characters')
        return value


class Registration(Credentials):
    display_name:str=Field(min_length=2,max_length=80)
    role:Literal['client','master']='client'

    @field_validator('display_name')
    @classmethod
    def name(cls,value):
        if len(value.strip())<2:
            raise ValueError('Enter your name')
        return value.strip()


class Recovery(BaseModel):
    model_config=ConfigDict(extra='forbid')
    username:str=Field(min_length=3,max_length=32,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$')
    recovery_code:SecretStr
    new_password:SecretStr

    @field_validator('username')
    @classmethod
    def normalized(cls,value):
        return value.lower()

    @field_validator('new_password')
    @classmethod
    def password_size(cls,value):
        return Credentials.password_size(value)

    @field_validator('recovery_code')
    @classmethod
    def code_size(cls,value):
        if not 16<=len(value.get_secret_value())<=128:
            raise ValueError('Enter your recovery code')
        return value


class ProfileUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    guide_seen:bool=True


class Onboarding(BaseModel):
    model_config=ConfigDict(extra='forbid')
    master:scheduling.MasterInput
    service:scheduling.ServiceInput


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS accounts(
      id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
      recovery_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS profiles(
      owner TEXT PRIMARY KEY, display_name TEXT NOT NULL, role TEXT NOT NULL,
      master_id TEXT, guide_seen INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS login_sessions(
      hash TEXT PRIMARY KEY, owner TEXT NOT NULL, expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS auth_limits(
      bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS runtime_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    db.execute('INSERT OR IGNORE INTO runtime_metadata VALUES(?,?)',('installation_id',secrets.token_urlsafe(18)))


def password_hash(password,salt=None):
    salt=salt or secrets.token_hex(16)
    digest=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=32768,r=8,p=3,maxmem=64*1024*1024,dklen=32).hex()
    return 'scrypt$32768$8$3$'+salt+'$'+digest


def verify_password(password,encoded):
    try:
        algorithm,n,r,p,salt,_=encoded.split('$')
        if (algorithm,n,r,p)!=('scrypt','32768','8','3'):
            return False
        return hmac.compare_digest(password_hash(password,salt),encoded)
    except (ValueError,TypeError):
        return False


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def consume_limit(connection,bucket,maximum,seconds):
    now=int(time.time())
    key=digest(bucket)
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM auth_limits WHERE expires<=?',(now,))
        db.execute('INSERT INTO auth_limits VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1',(key,now+seconds))
        count=db.execute('SELECT count FROM auth_limits WHERE bucket=?',(key,)).fetchone()['count']
    if count>maximum:
        raise HTTPException(429,'Too many attempts. Try again later',headers={'Retry-After':str(seconds)})


def profile(db,user):
    row=db.execute('SELECT * FROM profiles WHERE owner=?',(user,)).fetchone()
    if row:
        result=dict(row)
        result.pop('owner')
        result['guide_seen']=bool(result['guide_seen'])
        account=db.execute('SELECT username FROM accounts WHERE id=?',(user,)).fetchone()
        result['username']=account['username'] if account else None
        return result
    return {'display_name':'Мой кабинет','role':'client','master_id':None,'guide_seen':False,'username':None}


def start_session(db,request,user):
    sid=secrets.token_urlsafe(48)
    now=int(time.time())
    db.execute('DELETE FROM login_sessions WHERE expires<=?',(now,))
    db.execute('INSERT INTO login_sessions VALUES(?,?,?)',(digest(sid),user,now+7*86400))
    request.session.clear()
    request.session.update(user=user,sid=sid,csrf=secrets.token_urlsafe(32))


def check_session(db,request,user):
    if user.startswith('local:') or request.session.get('sid'):
        sid=request.session.get('sid','')
        if not sid or not db.execute('SELECT 1 FROM login_sessions WHERE hash=? AND owner=? AND expires>?',(digest(sid),user,int(time.time()))).fetchone():
            request.session.clear()
            raise HTTPException(401,'Sign in required')


def router(connection,owner,check_csrf):
    routes=APIRouter(prefix='/api')

    def anonymous(request):
        check_csrf(request)
        if request.session.get('user') and not request.session['user'].startswith('demo:'):
            raise HTTPException(409,'Sign out before switching accounts')

    def browser_user(request):
        user=owner(request)
        if request.headers.get('authorization'):
            raise HTTPException(403,'Use a browser session for account settings')
        return user

    @routes.post('/account/register',status_code=201)
    def register(data:Registration,request:Request):
        anonymous(request)
        # The proxy's client address may be shared, so username attempts are
        # separately limited; no X-Forwarded-For header supplied by a caller is trusted.
        consume_limit(connection,'signup:'+str(request.client.host),30,3600)
        encoded=password_hash(data.password.get_secret_value())
        recovery=secrets.token_urlsafe(24)
        user='local:'+secrets.token_urlsafe(18)
        try:
            with connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO accounts VALUES(?,?,?,?,?)',(user,data.username,encoded,digest(recovery),int(time.time())))
                db.execute('INSERT INTO profiles VALUES(?,?,?,NULL,0)',(user,data.display_name,data.role))
                start_session(db,request,user)
                result=profile(db,user)
        except sqlite3.IntegrityError:
            raise HTTPException(409,'This username is already taken') from None
        return {'profile':result,'recovery_code':recovery,'csrf':request.session['csrf']}

    @routes.post('/account/login')
    def login(data:Credentials,request:Request):
        anonymous(request)
        consume_limit(connection,'login:'+data.username,10,900)
        consume_limit(connection,'login-ip:'+str(request.client.host),120,900)
        with connection() as db:
            row=db.execute('SELECT * FROM accounts WHERE username=?',(data.username,)).fetchone()
        # Same KDF workload for unknown usernames; never disclose which field failed.
        valid=verify_password(data.password.get_secret_value(),row['password_hash']) if row else verify_password(data.password.get_secret_value(),'scrypt$32768$8$3$'+'00'*16+'$'+'00'*32)
        if not row or not valid:
            raise HTTPException(401,'Invalid username or password')
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT * FROM accounts WHERE id=?',(row['id'],)).fetchone()
            if current['password_hash']!=row['password_hash']:
                raise HTTPException(401,'Invalid username or password')
            start_session(db,request,row['id'])
            result=profile(db,row['id'])
        return {'profile':result,'csrf':request.session['csrf']}

    @routes.post('/account/recover')
    def recover(data:Recovery,request:Request):
        anonymous(request)
        consume_limit(connection,'recovery:'+data.username,5,1800)
        consume_limit(connection,'recovery-ip:'+str(request.client.host),30,1800)
        new_hash=password_hash(data.new_password.get_secret_value())
        new_code=secrets.token_urlsafe(24)
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM accounts WHERE username=?',(data.username,)).fetchone()
            if not row or not hmac.compare_digest(row['recovery_hash'],digest(data.recovery_code.get_secret_value())):
                raise HTTPException(401,'Invalid username or recovery code')
            db.execute('UPDATE accounts SET password_hash=?,recovery_hash=? WHERE id=?',(new_hash,digest(new_code),row['id']))
            db.execute('DELETE FROM login_sessions WHERE owner=?',(row['id'],))
            db.execute('DELETE FROM tokens WHERE owner=?',(row['id'],))
            db.execute('DELETE FROM auth_limits WHERE bucket=?',(digest('login:'+data.username),))
            start_session(db,request,row['id'])
        return {'recovery_code':new_code,'csrf':request.session['csrf']}

    @routes.post('/account/guide')
    def guide(data:ProfileUpdate,request:Request):
        user=browser_user(request)
        with connection() as db:
            db.execute('INSERT OR IGNORE INTO profiles VALUES(?,?,?,NULL,0)',(user,'Мой кабинет','client'))
            db.execute('UPDATE profiles SET guide_seen=? WHERE owner=?',(int(data.guide_seen),user))
        return {'ok':True}

    @routes.post('/onboarding/master',status_code=201)
    def onboard(data:Onboarding,request:Request):
        import json
        user=browser_user(request)
        if user.startswith('demo:'):
            raise HTTPException(403,'Sign in with a permanent account')
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT master_id FROM profiles WHERE owner=?',(user,)).fetchone()
            if previous and previous['master_id']:
                return scheduling.serialize_master(db,scheduling.master_for(db,previous['master_id'],user),user)
            if not data.service.active or not any(data.master.weekly.values()):
                raise HTTPException(422,'Add an active service and at least one working day')
            ident='master:'+secrets.token_urlsafe(16)
            item=data.master
            weekly=json.dumps({key:[w.model_dump() for w in windows] for key,windows in item.weekly.items()})
            db.execute('INSERT INTO masters VALUES(?,?,?,?,?,?,?,?,1)',(ident,user,item.name,item.timezone,item.step_minutes,item.notice_minutes,item.horizon_days,weekly))
            service=data.service
            db.execute('INSERT INTO services VALUES(?,?,?,?,?,?,1,1)',(ident+':service',ident,service.name,service.duration_minutes,service.buffer_minutes,service.price))
            db.execute('INSERT INTO master_publication VALUES(?,1)',(ident,))
            db.execute('INSERT INTO profiles VALUES(?,?,?, ?,1) ON CONFLICT(owner) DO UPDATE SET role=excluded.role,master_id=excluded.master_id,guide_seen=1',(user,item.name,'master',ident))
            return scheduling.serialize_master(db,scheduling.master_for(db,ident,user),user)

    return routes
