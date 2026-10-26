import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware

DB = os.getenv('DATABASE_PATH', 'booking.sqlite3')
secret = os.getenv('SESSION_SECRET', '')
if len(secret) < 32:
    raise RuntimeError('Set SESSION_SECRET to at least 32 random characters')
app = FastAPI(title='Master Booking', version='1.0.0')
app.add_middleware(SessionMiddleware, secret_key=secret, https_only=os.getenv('COOKIE_SECURE', 'true') == 'true', same_site='lax')
oauth = OAuth()
for name, url in [('google', 'https://accounts.google.com/.well-known/openid-configuration'), ('facebook', 'https://www.facebook.com/.well-known/openid-configuration/')]:
    oauth.register(name, client_id=os.getenv(name.upper() + '_CLIENT_ID'), client_secret=os.getenv(name.upper() + '_CLIENT_SECRET'), server_metadata_url=url, client_kwargs={'scope': 'openid email profile'})
facebook_version = os.getenv('FACEBOOK_GRAPH_VERSION', '')
oauth.register('facebook', overwrite=True, client_id=os.getenv('FACEBOOK_CLIENT_ID'), client_secret=os.getenv('FACEBOOK_CLIENT_SECRET'), authorize_url=f'https://www.facebook.com/{facebook_version}/dialog/oauth', access_token_url=f'https://graph.facebook.com/{facebook_version}/oauth/access_token', api_base_url=f'https://graph.facebook.com/{facebook_version}/', client_kwargs={'scope': 'public_profile', 'token_endpoint_auth_method': 'client_secret_post'})


def connection():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def initialize():
    with connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS slots(id TEXT PRIMARY KEY, master TEXT NOT NULL, service TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, price INTEGER NOT NULL CHECK(price>=0), CHECK(end>start));
        CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY, owner TEXT NOT NULL, slot TEXT NOT NULL REFERENCES slots(id), status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')), idem TEXT NOT NULL, UNIQUE(owner,idem));
        CREATE UNIQUE INDEX IF NOT EXISTS active_slot ON bookings(slot) WHERE status='confirmed';
        CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, owner TEXT NOT NULL, expires INTEGER NOT NULL);
        ''')


initialize()


def owner(request):
    authorization = request.headers.get('authorization', '')
    if authorization.startswith('Bearer '):
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with connection() as db:
            row = db.execute('SELECT owner FROM tokens WHERE hash=? AND expires>?', (digest, int(time.time()))).fetchone()
        if row:
            return row['owner']
        raise HTTPException(401, 'Invalid or expired access token')
    user = request.session.get('user')
    if not user:
        raise HTTPException(401, 'Sign in required')
    if request.method not in ('GET', 'HEAD') and not secrets.compare_digest(request.headers.get('x-csrf-token', ''), request.session.get('csrf', 'missing')):
        raise HTTPException(403, 'Invalid CSRF token')
    return user


@app.get('/', response_class=HTMLResponse)
def home():
    return Path(__file__).with_name('index.html').read_text(encoding='utf-8')


@app.get('/auth/{provider}')
async def login(provider: str, request: Request):
    if provider not in ('google', 'facebook'):
        raise HTTPException(404, 'Unknown provider')
    client = oauth.create_client(provider)
    if not client.client_id or (provider == 'facebook' and not facebook_version):
        raise HTTPException(503, 'Login provider is not configured')
    return await client.authorize_redirect(request, str(request.url_for('callback', provider=provider)))


@app.get('/auth/{provider}/callback', name='callback')
async def callback(provider: str, request: Request):
    if provider not in ('google', 'facebook'):
        raise HTTPException(404, 'Unknown provider')
    try:
        client = oauth.create_client(provider)
        token = await client.authorize_access_token(request)
        user = token.get('userinfo')
        if provider == 'facebook':
            proof = hmac.new(os.environ['FACEBOOK_CLIENT_SECRET'].encode(), token['access_token'].encode(), hashlib.sha256).hexdigest()
            response = await client.get('me', token=token, params={'fields': 'id', 'appsecret_proof': proof})
            response.raise_for_status()
            user = {'sub': response.json().get('id')}
        if not user or not user.get('sub'):
            raise ValueError('Missing verified identity')
    except Exception:
        raise HTTPException(401, 'Authentication failed') from None
    request.session.clear()
    request.session.update(user=provider + ':' + user['sub'], csrf=secrets.token_urlsafe(32))
    return RedirectResponse('/')


@app.get('/api/me')
def me(request: Request):
    return {'id': owner(request), 'csrf': request.session.get('csrf')}


@app.post('/api/logout')
def logout(request: Request):
    owner(request)
    request.session.clear()
    return {'ok': True}


@app.post('/api/agent-token')
def agent_token(request: Request):
    user = owner(request)
    token = secrets.token_urlsafe(48)
    with connection() as db:
        db.execute('INSERT INTO tokens VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user, int(time.time()) + 3600))
    return {'access_token': token, 'token_type': 'Bearer', 'expires_in': 3600}


@app.get('/api/slots')
def slots():
    with connection() as db:
        return [dict(r) for r in db.execute("SELECT s.* FROM slots s WHERE start>? AND NOT EXISTS (SELECT 1 FROM bookings b JOIN slots busy ON busy.id=b.slot WHERE b.status='confirmed' AND busy.master=s.master AND busy.start<s.end AND busy.end>s.start) ORDER BY start", (int(time.time()),))]


class BookingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    slot_id: str = Field(min_length=1, max_length=100)


@app.post('/api/bookings', status_code=201)
def book(data: BookingInput, request: Request):
    user = owner(request)
    key = request.headers.get('idempotency-key', '')
    if not 1 <= len(key) <= 128:
        raise HTTPException(400, 'Idempotency-Key required (1–128 characters)')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM bookings WHERE owner=? AND idem=?', (user, key)).fetchone()
        if previous:
            if previous['slot'] != data.slot_id:
                raise HTTPException(409, 'Idempotency key already used for another request')
            return dict(previous)
        slot = db.execute('SELECT * FROM slots WHERE id=?', (data.slot_id,)).fetchone()
        if not slot:
            raise HTTPException(404, 'Slot not found')
        if slot['start'] <= int(time.time()):
            raise HTTPException(409, 'Slot is in the past')
        conflict = db.execute("SELECT 1 FROM bookings b JOIN slots s ON s.id=b.slot WHERE b.status='confirmed' AND s.master=? AND s.start<? AND s.end>?", (slot['master'], slot['end'], slot['start'])).fetchone()
        if conflict:
            raise HTTPException(409, 'Time is already booked')
        booking_id = secrets.token_urlsafe(18)
        db.execute('INSERT INTO bookings VALUES(?,?,?,?,?)', (booking_id, user, data.slot_id, 'confirmed', key))
        return dict(db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())


@app.get('/api/bookings')
def bookings(request: Request):
    user = owner(request)
    with connection() as db:
        return [dict(r) for r in db.execute('SELECT * FROM bookings WHERE owner=?', (user,))]


@app.post('/api/bookings/{booking_id}/cancel')
def cancel(booking_id: str, request: Request):
    user = owner(request)
    with connection() as db:
        row = db.execute('SELECT * FROM bookings WHERE id=? AND owner=?', (booking_id, user)).fetchone()
        if not row:
            raise HTTPException(404, 'Booking not found')
        db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
    return {'id': booking_id, 'status': 'cancelled'}
