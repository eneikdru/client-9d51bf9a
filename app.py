import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.openapi.docs import get_swagger_ui_html
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware
import scheduling
import accounts
import storage
from fastapi.exceptions import RequestValidationError

DB = storage.database_path()
secret = storage.session_secret()
ALLOW_DEMO=os.getenv('ALLOW_DEMO','false').lower()=='true'
ROOT_PATH = os.getenv('ROOT_PATH', '').rstrip('/')
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '').rstrip('/')
app = FastAPI(title='Время · Booking', version='2.0.0', root_path=ROOT_PATH, docs_url=None, redoc_url=None)


class SlotOutput(BaseModel):
    id: str
    master: str
    service: str
    start: int
    end: int
    price: int = Field(ge=0)


class BookingOutput(BaseModel):
    id: str
    owner: str
    slot: str
    status: Literal['confirmed', 'cancelled']
    idem: str
    appointment: SlotOutput | None = None


@app.exception_handler(sqlite3.OperationalError)
async def storage_failure(request: Request, exc):
    return JSONResponse(status_code=503, content={'detail': 'Storage temporarily unavailable'}, headers={'Retry-After': '1'})

@app.exception_handler(RequestValidationError)
async def validation_failure(request:Request,exc):
    # Never echo a password or recovery code in validation responses.
    return JSONResponse(status_code=422,content={'detail':[{key:error[key] for key in ('loc','msg','type')} for error in exc.errors()]})

app.add_middleware(SessionMiddleware, secret_key=secret, https_only=os.getenv('COOKIE_SECURE', 'true') == 'true', same_site='lax',max_age=7*86400)


@app.middleware('http')
async def response_headers(request:Request,call_next):
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    if request.scope.get('path','').startswith('/api/') or request.url.path.startswith(request.scope.get('root_path','')+'/api/'):
        response.headers['Cache-Control']='no-store'
    return response
oauth = OAuth()
oauth.register('google', client_id=os.getenv('GOOGLE_CLIENT_ID'), client_secret=os.getenv('GOOGLE_CLIENT_SECRET'), server_metadata_url='https://accounts.google.com/.well-known/openid-configuration', client_kwargs={'scope': 'openid email profile'})
facebook_version = os.getenv('FACEBOOK_GRAPH_VERSION', '')
oauth.register('facebook', overwrite=True, client_id=os.getenv('FACEBOOK_CLIENT_ID'), client_secret=os.getenv('FACEBOOK_CLIENT_SECRET'), authorize_url=f'https://www.facebook.com/{facebook_version}/dialog/oauth', access_token_url=f'https://graph.facebook.com/{facebook_version}/oauth/access_token', api_base_url=f'https://graph.facebook.com/{facebook_version}/', client_kwargs={'scope': 'public_profile', 'token_endpoint_auth_method': 'client_secret_post'})


@contextmanager
def connection():
    db = sqlite3.connect(DB, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize():
    with connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS slots(id TEXT PRIMARY KEY, master TEXT NOT NULL, service TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, price INTEGER NOT NULL CHECK(price>=0), CHECK(end>start));
        CREATE TABLE IF NOT EXISTS bookings(id TEXT PRIMARY KEY, owner TEXT NOT NULL, slot TEXT NOT NULL REFERENCES slots(id), status TEXT NOT NULL CHECK(status IN ('confirmed','cancelled')), idem TEXT NOT NULL, UNIQUE(owner,idem));
        CREATE UNIQUE INDEX IF NOT EXISTS active_slot ON bookings(slot) WHERE status='confirmed';
        CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, owner TEXT NOT NULL, expires INTEGER NOT NULL);
        ''')
        scheduling.initialize(db)
        accounts.initialize(db)


initialize()


def seed_demo():
    """Only reserved demo IDs are generated; real availability is never synthesized."""
    day = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    with connection() as db:
        for number, (master, service, price) in enumerate([
            ('Анна · демо', 'Стрижка', 5000),
            ('Нино · демо', 'Маникюр', 7000),
            ('Георгий · демо', 'Массаж', 9000),
        ]):
            for hour in (0, 2, 4):
                start = int((day + timedelta(hours=hour)).timestamp())
                db.execute('INSERT OR IGNORE INTO slots VALUES(?,?,?,?,?,?)',
                           (f'demo:{start}:{number}', f'demo:{master}', service, start, start + 3600, price))


if ALLOW_DEMO:
    seed_demo()


def check_csrf(request):
    expected = request.session.get('csrf')
    supplied = request.headers.get('x-csrf-token', '')
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(403, 'Invalid CSRF token')


def provider_ready(provider):
    return bool(os.getenv(provider.upper() + '_CLIENT_ID') and os.getenv(provider.upper() + '_CLIENT_SECRET')
                and (provider != 'facebook' or facebook_version))


def public_url(request, route):
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL + '/' + route.lstrip('/')
    root = request.scope.get('root_path', '').rstrip('/')
    return str(request.base_url).rstrip('/') + '/' + route.lstrip('/') if not root else str(request.url.replace(path=root + '/' + route.lstrip('/'), query=''))


def owner(request):
    authorization = request.headers.get('authorization', '')
    if authorization:
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Invalid authorization scheme')
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with connection() as db:
            row = db.execute('SELECT owner FROM tokens WHERE hash=? AND expires>?', (digest, int(time.time()))).fetchone()
        if row:
            return row['owner']
        raise HTTPException(401, 'Invalid or expired access token')
    user = request.session.get('user')
    if not user:
        raise HTTPException(401, 'Sign in required')
    if user.startswith('demo:') and not ALLOW_DEMO:
        request.session.clear()
        raise HTTPException(401,'Sign in required')
    with connection() as db:
        accounts.check_session(db,request,user)
    if request.method not in ('GET', 'HEAD'):
        check_csrf(request)
    return user


@app.get('/', response_class=HTMLResponse)
def home():
    return HTMLResponse(Path(__file__).with_name('index.html').read_text(encoding='utf-8'),headers={'Cache-Control':'no-store'})


@app.get('/calendar.js',include_in_schema=False)
def calendar_script():
    return FileResponse(Path(__file__).with_name('calendar.js'),media_type='application/javascript',headers={'Cache-Control':'no-cache, max-age=0, must-revalidate'})


@app.get('/app.css',include_in_schema=False)
def stylesheet():
    return FileResponse(Path(__file__).with_name('app.css'),media_type='text/css',headers={'Cache-Control':'no-cache, max-age=0, must-revalidate'})


@app.get('/docs', include_in_schema=False)
def docs():
    response = get_swagger_ui_html(openapi_url='./openapi.json', title='Booking API')
    # A proxy may strip the mount without passing ASGI root_path. Resolve local
    # Try-it-out calls from the visible docs URL, just as the booking UI does.
    interceptor = """requestInterceptor: (req) => {
      const target = new URL(req.url, location.href);
      const base = new URL('./', location.href);
      if (target.origin === base.origin && !target.pathname.startsWith(base.pathname)) {
        target.pathname = base.pathname + target.pathname.replace(/^\\//, '');
        req.url = target.href;
      }
      return req;
    },"""
    html = response.body.decode().replace('const ui = SwaggerUIBundle({', 'const ui = SwaggerUIBundle({' + interceptor)
    return HTMLResponse(html)


@app.get('/api/config')
def config(request: Request):
    request.session.setdefault('csrf', secrets.token_urlsafe(32))
    return {'providers': {name: provider_ready(name) for name in ('google', 'facebook')},
            'csrf': request.session['csrf'], 'demo_available': ALLOW_DEMO,'local_auth':True,'mode':'live'}


@app.get('/api/status')
def status():
    with connection() as db:
        installation=db.execute("SELECT value FROM runtime_metadata WHERE key='installation_id'").fetchone()['value']
    return {'status':'ok','version':'2.0.0','mode':'live','installation_id':installation}


@app.post('/api/demo-session')
def demo_session(request: Request):
    if not ALLOW_DEMO:
        raise HTTPException(404,'Demo mode is disabled')
    check_csrf(request)
    current = request.session.get('user', '')
    if current and not current.startswith('demo:'):
        raise HTTPException(409, 'Sign out before entering demo mode')
    if not current:
        request.session.update(user='demo:' + secrets.token_urlsafe(24), csrf=secrets.token_urlsafe(32))
    seed_demo()
    return {'mode': 'demo', 'csrf': request.session['csrf']}


@app.get('/auth/{provider}')
async def login(provider: str, request: Request):
    if provider not in ('google', 'facebook'):
        raise HTTPException(404, 'Unknown provider')
    client = oauth.create_client(provider)
    if not provider_ready(provider):
        return RedirectResponse('../?notice=provider_unavailable', status_code=303)
    return await client.authorize_redirect(request, public_url(request, f'auth/{provider}/callback'))


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
    with connection() as db:
        accounts.start_session(db,request,provider + ':' + user['sub'])
    return RedirectResponse('../../', status_code=303)


@app.get('/api/me')
def me(request: Request):
    user = owner(request)
    with connection() as db:
        details=accounts.profile(db,user)
    return {'id': user, 'csrf': request.session.get('csrf'), 'mode': 'demo' if user.startswith('demo:') else 'live','profile':details}


@app.post('/api/logout')
def logout(request: Request):
    user = owner(request)
    with connection() as db:
        db.execute('DELETE FROM tokens WHERE owner=?', (user,))
        sid=request.session.get('sid')
        if sid:
            db.execute('DELETE FROM login_sessions WHERE hash=?',(accounts.digest(sid),))
    request.session.clear()
    return {'ok': True}


@app.post('/api/agent-token')
def agent_token(request: Request):
    user = owner(request)
    if request.headers.get('authorization'):
        raise HTTPException(403, 'Use a browser session to issue an agent token')
    token = secrets.token_urlsafe(48)
    with connection() as db:
        db.execute('INSERT INTO tokens VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user, int(time.time()) + 3600))
    return {'access_token': token, 'token_type': 'Bearer', 'expires_in': 3600}


@app.get('/api/slots', response_model=list[SlotOutput])
def slots(request: Request):
    user = owner(request) if request.session.get('user') or request.headers.get('authorization') else ''
    demo = user.startswith('demo:')
    if demo:
        seed_demo()
    with connection() as db:
        return [dict(r) for r in db.execute("SELECT s.* FROM slots s WHERE start>? AND (s.id LIKE 'demo:%')=? AND NOT EXISTS (SELECT 1 FROM calendar_slots c WHERE c.slot_id=s.id) AND NOT EXISTS (SELECT 1 FROM bookings b JOIN slots busy ON busy.id=b.slot WHERE b.status='confirmed' AND busy.master=s.master AND busy.start<s.end AND busy.end>s.start) ORDER BY start, id", (int(time.time()), demo))]


class BookingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    slot_id: str = Field(min_length=1, max_length=100)


@app.post('/api/bookings', status_code=201, response_model=BookingOutput)
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
        if user.startswith('demo:') != slot['id'].startswith('demo:'):
            raise HTTPException(404, 'Slot not found')
        scheduling.validate_calendar_slot(db,slot,user)
        if slot['start'] <= int(time.time()):
            raise HTTPException(409, 'Slot is in the past')
        conflict = db.execute("SELECT 1 FROM bookings b JOIN slots s ON s.id=b.slot WHERE b.status='confirmed' AND s.master=? AND s.start<? AND s.end>?", (slot['master'], slot['end'], slot['start'])).fetchone()
        if conflict:
            raise HTTPException(409, 'Time is already booked')
        booking_id = secrets.token_urlsafe(18)
        db.execute('INSERT INTO bookings VALUES(?,?,?,?,?)', (booking_id, user, data.slot_id, 'confirmed', key))
        return dict(db.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone())


@app.get('/api/bookings', response_model=list[BookingOutput])
def bookings(request: Request):
    user = owner(request)
    with connection() as db:
        rows = db.execute('SELECT b.*,COALESCE(m.name,s.master) AS master,s.service,s.start,s.end,s.price FROM bookings b JOIN slots s ON s.id=b.slot LEFT JOIN masters m ON m.id=s.master WHERE b.owner=? ORDER BY s.start,b.id', (user,))
        return [{**dict(r), 'appointment': {'id': r['slot'], **{key:r[key] for key in ('master','service','start','end','price')}}} for r in rows]


@app.post('/api/bookings/{booking_id}/cancel')
def cancel(booking_id: str, request: Request):
    user = owner(request)
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT b.*,s.start FROM bookings b JOIN slots s ON s.id=b.slot WHERE b.id=? AND b.owner=?', (booking_id, user)).fetchone()
        if not row:
            raise HTTPException(404, 'Booking not found')
        if row['status'] == 'confirmed' and row['start'] <= int(time.time()):
            raise HTTPException(409, 'Started or completed appointments cannot be cancelled')
        db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
    return {'id': booking_id, 'status': 'cancelled'}


app.include_router(scheduling.router(connection,owner))
app.include_router(accounts.router(connection,owner,check_csrf))
