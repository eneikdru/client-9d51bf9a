import hashlib
import os
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ['SESSION_SECRET'] = 'test-only-session-secret-at-least-32-characters'
_initial_database = tempfile.TemporaryDirectory()
os.environ['DATABASE_PATH'] = os.path.join(_initial_database.name, 'initial.sqlite3')
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


def test_demo_end_to_end_and_isolation(client):
    assert client.post('/api/demo-session').status_code == 403
    csrf = client.get('/api/config').json()['csrf']
    response = client.post('/api/demo-session', headers={'X-CSRF-Token':csrf})
    assert response.status_code == 200
    csrf = response.json()['csrf']
    user = client.get('/api/me').json()['id']
    assert user.startswith('demo:')
    assert client.post('/api/demo-session', headers={'X-CSRF-Token':csrf}).status_code == 200
    assert client.get('/api/me').json()['id'] == user
    slots = client.get('/api/slots').json()
    assert len(slots) == 9
    assert all(s['id'].startswith('demo:') for s in slots)
    headers = {'X-CSRF-Token':csrf, 'Idempotency-Key':'demo-create'}
    result = client.post('/api/bookings', headers=headers, json={'slot_id':slots[0]['id']})
    assert result.status_code == 201
    ident = result.json()['id']
    assert client.post('/api/bookings', headers=headers, json={'slot_id':slots[0]['id']}).json()['id'] == ident
    assert client.post('/api/bookings', headers={**headers,'Idempotency-Key':'real'}, json={'slot_id':'a'}).status_code == 404
    assert book(client, slot=slots[1]['id'], key='cross-demo').status_code == 404
    assert len(client.get('/api/bookings').json()) == 1
    assert client.post(f'/api/bookings/{ident}/cancel', headers=headers).status_code == 200
    assert slots[0]['id'] in [s['id'] for s in client.get('/api/slots').json()]
    assert client.post('/api/logout',headers=headers).status_code == 200
    assert client.get('/api/me').status_code == 401
    assert not any(s['id'].startswith('demo:') for s in client.get('/api/slots').json())


@pytest.mark.parametrize('prefix', ['', '/preview/test'])
def test_mounted_docs_and_auth_redirects(client, monkeypatch, prefix):
    from fastapi import FastAPI
    monkeypatch.setenv('GOOGLE_CLIENT_ID', '')
    outer = FastAPI()
    if prefix:
        outer.mount(prefix, app.app)
    mounted = TestClient(outer if prefix else app.app, base_url='https://testserver')
    docs = mounted.get(prefix+'/docs')
    assert docs.status_code == 200
    assert "url: './openapi.json'" in docs.text
    schema = mounted.get(prefix+'/openapi.json').json()
    assert '/api/bookings' in schema['paths']
    # Try-it-out must use the mount, not the domain root.
    if prefix:
        assert {'url':prefix} in schema['servers']
    unavailable = mounted.get(prefix+'/auth/google', follow_redirects=False)
    assert unavailable.status_code == 303
    from urllib.parse import urljoin
    assert urljoin(str(unavailable.url), unavailable.headers['location']) == 'https://testserver'+prefix+'/?notice=provider_unavailable'
    class Provider:
        async def authorize_access_token(self, request):
            return {'userinfo':{'sub':'mounted-user'}}
        async def authorize_redirect(self, request, redirect_uri):
            return app.JSONResponse({'redirect_uri':redirect_uri})
    monkeypatch.setenv('GOOGLE_CLIENT_ID','configured-id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET','configured-secret')
    monkeypatch.setattr(app.oauth,'create_client',lambda _: Provider())
    assert mounted.get(prefix+'/auth/google').json()['redirect_uri'] == 'https://testserver'+prefix+'/auth/google/callback'
    response = mounted.get(prefix+'/auth/google/callback',follow_redirects=False)
    assert response.status_code == 303
    assert urljoin(str(response.url),response.headers['location']) == 'https://testserver'+prefix+'/'


def test_cannot_cancel_started_booking(client):
    ident=book(client).json()['id']
    with app.connection() as db:
        db.execute("UPDATE slots SET start=1 WHERE id='a'")
    assert client.post(f'/api/bookings/{ident}/cancel', headers={'Authorization':'Bearer alice'}).status_code == 409
    assert client.get('/api/bookings',headers={'Authorization':'Bearer alice'}).json()[0]['status'] == 'confirmed'


def test_overlapping_slot_race_and_same_key_race(client):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda pair: book(client,slot=pair[0],key=pair[1],user=pair[1]), [('a','alice'),('b','bob')]))
    assert sorted(r.status_code for r in results) == [201,409]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _: book(client,slot='d',key='same-key'), range(4)))
    assert all(r.status_code == 201 for r in results)
    assert len({r.json()['id'] for r in results}) == 1


def test_tokens_cannot_delegate_and_logout_revokes(client):
    assert client.post('/api/agent-token',headers={'Authorization':'Bearer alice'}).status_code == 403
    import base64,json
    from itsdangerous import TimestampSigner
    value=base64.b64encode(json.dumps({'user':'alice','csrf':'logout-test'}).encode())
    client.cookies.set('session',TimestampSigner(os.environ['SESSION_SECRET']).sign(value).decode(),domain='testserver.local',path='/')
    assert client.post('/api/logout',headers={'X-CSRF-Token':'logout-test'}).status_code == 200
    assert book(client).status_code == 401


def test_failed_transaction_rolls_back(client):
    assert book(client,slot='missing').status_code == 404
    assert book(client).status_code == 201
    with app.connection() as db:
        assert db.execute('SELECT count(*) FROM bookings').fetchone()[0] == 1


def test_seed_is_repeatable_and_never_modifies_real_slots(client):
    app.seed_demo()
    app.seed_demo()
    with app.connection() as db:
        assert db.execute("SELECT count(*) FROM slots WHERE id LIKE 'demo:%'").fetchone()[0] == 9
        assert db.execute("SELECT price FROM slots WHERE id='a'").fetchone()[0] == 1000
        with pytest.raises(app.sqlite3.IntegrityError):
            db.execute("INSERT INTO slots VALUES('invalid','m','s',10,10,100)")
        with pytest.raises(app.sqlite3.IntegrityError):
            db.execute("INSERT INTO slots VALUES('negative','m','s',10,20,-1)")


def test_live_session_cannot_be_replaced_by_demo(client):
    import base64,json
    from itsdangerous import TimestampSigner
    cookie = TimestampSigner(os.environ['SESSION_SECRET']).sign(base64.b64encode(json.dumps({'user':'google:real','csrf':'real-session'}).encode())).decode()
    client.cookies.set('session',cookie,domain='testserver.local',path='/')
    assert client.post('/api/demo-session',headers={'X-CSRF-Token':'real-session'}).status_code == 409
    assert client.get('/api/me').json()['id'] == 'google:real'


def test_cancel_race_is_idempotent_and_releases_slot(client):
    ident=book(client).json()['id']
    def cancel(_):
        return client.post(f'/api/bookings/{ident}/cancel',headers={'Authorization':'Bearer alice'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(cancel,range(4)))
    assert all(r.status_code == 200 for r in results)
    assert book(client,user='bob').status_code == 201


def test_database_errors_do_not_leak_paths(client, monkeypatch):
    monkeypatch.setattr(app,'DB','missing-parent-directory/private.sqlite3')
    response=client.get('/api/slots')
    assert response.status_code == 503
    assert response.json() == {'detail':'Storage temporarily unavailable'}


def test_facebook_verified_profile_and_failed_exchange(client, monkeypatch):
    monkeypatch.setenv('FACEBOOK_CLIENT_SECRET','test-facebook-secret')
    class Profile:
        def raise_for_status(self):
            pass
        def json(self):
            return {'id':'facebook-user'}
    class Provider:
        async def authorize_access_token(self, request):
            return {'access_token':'test-access-token'}
        async def get(self,path,token,params):
            import hmac
            assert path == 'me'
            assert params['appsecret_proof'] == hmac.new(b'test-facebook-secret',b'test-access-token',hashlib.sha256).hexdigest()
            return Profile()
    monkeypatch.setattr(app.oauth,'create_client',lambda _: Provider())
    assert client.get('/auth/facebook/callback',follow_redirects=False).status_code == 303
    assert client.get('/api/me').json()['id'] == 'facebook:facebook-user'
    class Failed:
        async def authorize_access_token(self,request):
            raise RuntimeError('private-provider-error')
    monkeypatch.setattr(app.oauth,'create_client',lambda _: Failed())
    response=client.get('/auth/facebook/callback')
    assert response.status_code == 401
    assert 'private-provider-error' not in response.text


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
    assert client.get('/auth/google/callback',follow_redirects=False).status_code == 303
    assert client.get('/api/me').json()['id'] == 'google:verified-id'
    class Invalid:
        async def authorize_access_token(self, request):
            return {'userinfo': {'email':'untrusted@example.test'}}
    monkeypatch.setattr(app.oauth, 'create_client', lambda _: Invalid())
    assert client.get('/auth/google/callback').status_code == 401
