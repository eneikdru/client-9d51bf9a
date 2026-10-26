import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor

os.environ['SESSION_SECRET'] = 'test-only-session-secret-at-least-32-characters'
os.environ['DATABASE_PATH'] = ':memory:'
import app
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DB', str(tmp_path / 'test.sqlite3'))
    app.initialize()
    now = int(time.time()) + 3600
    with app.connection() as db:
        for name, master, start, end in [('a','m',now,now+600),('b','m',now+599,now+900),('c','m',now+600,now+1200),('d','other',now,now+600),('past','m',1,2)]:
            db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?)', (name, master, 'Service', start, end, 1000))
        for token in ['alice', 'bob']:
            db.execute('INSERT INTO tokens VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(),token,now))
    return TestClient(app.app, base_url='https://testserver')


def book(client, slot='a', key='one', user='alice'):
    return client.post('/api/bookings', json={'slot_id':slot}, headers={'Authorization':'Bearer '+user,'Idempotency-Key':key})


def test_auth_and_validation(client):
    assert client.get('/api/me').status_code == 401
    assert book(client,user='forged').status_code == 401
    assert book(client,key='').status_code == 400
    assert book(client,key='x'*129).status_code == 400
    assert book(client,slot='').status_code == 422
    assert book(client,slot='missing').status_code == 404
    assert book(client,slot='past').status_code == 409
    assert client.get('/auth/unknown').status_code == 404
    assert client.get('/auth/google/callback').status_code == 401
    assert client.get('/').status_code == 200


def test_overlap_boundary_and_idempotency(client):
    first=book(client)
    assert first.status_code == 201
    assert book(client).json()['id'] == first.json()['id']
    assert book(client,slot='c').status_code == 409
    assert book(client,slot='b',key='two').status_code == 409
    assert book(client,slot='c',key='three').status_code == 201
    assert book(client,slot='d',key='four').status_code == 201
    assert not client.get('/api/slots').json() or all(s['id']!='b' for s in client.get('/api/slots').json())


def test_ownership_cancellation(client):
    ident=book(client).json()['id']
    path=f'/api/bookings/{ident}/cancel'
    assert client.post(path,headers={'Authorization':'Bearer bob'}).status_code == 404
    assert client.get('/api/bookings',headers={'Authorization':'Bearer bob'}).json() == []
    for _ in range(2):
        assert client.post(path,headers={'Authorization':'Bearer alice'}).status_code == 200
    assert book(client).json()['status'] == 'cancelled'
    assert book(client,key='new').status_code == 201


def test_race(client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda u: book(client,key=u,user=u).status_code, ['alice','bob']))
    assert sorted(results) == [201,409]


def test_expired_token(client):
    with app.connection() as db:
        db.execute('UPDATE tokens SET expires=0')
    assert book(client).status_code == 401


def test_preview_links_stay_under_mount(client):
    from html.parser import HTMLParser
    from urllib.parse import urljoin
    class Links(HTMLParser):
        paths = []
        def handle_starttag(self, tag, attrs):
            if tag == 'a':
                self.paths.append(dict(attrs)['href'])
    parser = Links()
    parser.feed(client.get('/').text)
    for base in ['https://example.test/', 'https://example.test/api/preview/9d51bf9a/']:
        for path in parser.paths:
            assert urljoin(base, path).startswith(base)
    assert 'fetch(new URL(' in client.get('/').text


def test_browser_csrf_and_token(client):
    import base64
    import json
    from itsdangerous import TimestampSigner
    value = base64.b64encode(json.dumps({'user':'alice','csrf':'csrf-test'}).encode())
    cookie = TimestampSigner(os.environ['SESSION_SECRET']).sign(value).decode()
    client.cookies.set('session', cookie, domain='testserver.local', path='/')
    assert client.post('/api/agent-token').status_code == 403
    response = client.post('/api/agent-token',headers={'X-CSRF-Token':'csrf-test'})
    assert response.status_code == 200
    token = response.json()['access_token']
    assert client.get('/api/me',headers={'Authorization':'Bearer '+token}).json()['id'] == 'alice'
    assert client.post('/api/logout',headers={'X-CSRF-Token':'csrf-test'}).status_code == 200
    assert client.get('/api/me').status_code == 401


def test_oauth_success_and_rejection(client, monkeypatch):
    class Provider:
        async def authorize_access_token(self, request):
            return {'userinfo': {'sub':'verified-id'}}
    monkeypatch.setattr(app.oauth, 'create_client', lambda _: Provider())
    assert client.get('/auth/google/callback',follow_redirects=False).status_code == 307
    assert client.get('/api/me').json()['id'] == 'google:verified-id'
    class Invalid:
        async def authorize_access_token(self, request):
            return {'userinfo': {'email':'untrusted@example.test'}}
    monkeypatch.setattr(app.oauth, 'create_client', lambda _: Invalid())
    assert client.get('/auth/google/callback').status_code == 401
