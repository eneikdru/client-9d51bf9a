import base64
import json
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
import pytest

import accounts
import app
import scheduling
from test_app import client

TEST_PASSWORD='test-only-strong-password-2026'


def csrf(client):
    return {'X-CSRF-Token':client.get('/api/config').json()['csrf']}


def register(client,username='testclient',role='client'):
    return client.post('/api/account/register',headers=csrf(client),json={'username':username,'password':TEST_PASSWORD,'display_name':'Тестовый клиент','role':role})


def logout(client):
    return client.post('/api/logout',headers=csrf(client))


def login(client,username='testclient',password=TEST_PASSWORD):
    return client.post('/api/account/login',headers=csrf(client),json={'username':username,'password':password})


def onboarding_payload():
    return {'master':{'name':'Тестовый мастер','timezone':'Asia/Tbilisi','step_minutes':15,'notice_minutes':0,'horizon_days':60,'weekly':{str(i):[{'start':'10:00','end':'19:00'}] for i in range(7)}},'service':{'name':'Тестовая услуга','duration_minutes':45,'buffer_minutes':15,'price':5000,'active':True}}


def onboard(client):
    response=client.post('/api/onboarding/master',headers=csrf(client),json=onboarding_payload())
    assert response.status_code==201,response.text
    return response.json()


def test_register_login_logout_and_secret_storage(client):
    response=register(client,username='Mixed.Name')
    assert response.status_code==201
    assert len(response.json()['recovery_code'])>=24
    me=client.get('/api/me').json()
    assert me['id'].startswith('local:')
    assert me['profile']['username']=='mixed.name'
    with app.connection() as db:
        account=db.execute('SELECT * FROM accounts').fetchone()
        assert TEST_PASSWORD not in account['password_hash']
        assert response.json()['recovery_code']!=account['recovery_hash']
        assert accounts.verify_password(TEST_PASSWORD,account['password_hash'])
        assert not accounts.verify_password('wrong',account['password_hash'])
    cookies=list(client.cookies.jar)
    old_cookie=next(c.value for c in cookies if c.name=='session')
    assert logout(client).status_code==200
    replay=TestClient(app.app,base_url='https://testserver')
    replay.cookies.set('session',old_cookie,domain='testserver.local',path='/')
    assert replay.get('/api/me').status_code==401
    assert login(client,username='MIXED.NAME').status_code==200
    assert client.get('/api/me').json()['id']==me['id']


def test_real_mode_disables_demo_and_keeps_catalog_clean(client,monkeypatch):
    monkeypatch.setattr(app,'ALLOW_DEMO',False)
    assert client.get('/api/config').json()['demo_available'] is False
    assert client.post('/api/demo-session',headers=csrf(client)).status_code==404
    assert client.get('/api/catalog').json()==[]
    assert register(client).status_code==201
    assert client.get('/api/masters').json()==[]


@pytest.mark.parametrize('changes',[
    {'username':'a'}, {'username':'name with spaces'}, {'password':'short'},
    {'password':'x'*129}, {'display_name':'  '}, {'role':'administrator'},
])
def test_invalid_registration_never_echoes_password(client,changes):
    data={'username':'testclient','password':TEST_PASSWORD,'display_name':'Test','role':'client',**changes}
    response=client.post('/api/account/register',headers=csrf(client),json=data)
    assert response.status_code==422
    assert data['password'] not in response.text
    with app.connection() as db:
        assert db.execute('SELECT count(*) FROM accounts').fetchone()[0]==0


def test_auth_requires_csrf_and_duplicate_registration_is_atomic(client):
    data={'username':'tester','password':TEST_PASSWORD,'display_name':'Test'}
    assert client.post('/api/account/register',json=data).status_code==403
    assert register(client).status_code==201
    assert logout(client).status_code==200
    assert register(client).status_code==409
    with app.connection() as db:
        assert db.execute('SELECT count(*) FROM accounts').fetchone()[0]==1


def test_failed_login_is_uniform_and_rate_limited(client):
    assert register(client).status_code==201
    logout(client)
    for _ in range(10):
        response=login(client,password='incorrect-password')
        assert response.status_code==401
        assert response.json()['detail']=='Invalid username or password'
    assert login(client).status_code==429
    with app.connection() as db:
        db.execute('DELETE FROM auth_limits')
    assert login(client,username='doesnotexist').json()['detail']=='Invalid username or password'


def test_recovery_rotates_code_and_revokes_sessions_and_tokens(client):
    result=register(client).json()
    old_code=result['recovery_code']
    token=client.post('/api/agent-token',headers=csrf(client)).json()['access_token']
    second=TestClient(app.app,base_url='https://testserver')
    assert login(second).status_code==200
    recover=TestClient(app.app,base_url='https://testserver')
    response=recover.post('/api/account/recover',headers=csrf(recover),json={'username':'testclient','recovery_code':old_code,'new_password':'replacement-test-password'})
    assert response.status_code==200
    assert response.json()['recovery_code']!=old_code
    assert client.get('/api/me').status_code==401
    assert second.get('/api/me').status_code==401
    assert client.get('/api/bookings',headers={'Authorization':'Bearer '+token}).status_code==401
    logout(recover)
    replay=recover.post('/api/account/recover',headers=csrf(recover),json={'username':'testclient','recovery_code':old_code,'new_password':'another-test-password'})
    assert replay.status_code==401
    assert login(client).status_code==401
    assert login(client,password='replacement-test-password').status_code==200


def test_forged_or_expired_local_session_rejected(client):
    assert register(client).status_code==201
    with app.connection() as db:
        db.execute('UPDATE login_sessions SET expires=0')
    assert client.get('/api/me').status_code==401
    value=base64.b64encode(json.dumps({'user':'local:invented','csrf':'known'}).encode())
    forged=TimestampSigner(app.secret).sign(value).decode()
    client.cookies.clear()
    client.cookies.set('session',forged,domain='testserver.local',path='/')
    assert client.get('/api/me').status_code==401


def test_master_onboarding_is_atomic_published_and_idempotent(client):
    assert register(client,role='master').status_code==201
    master=onboard(client)
    assert master['published'] is True
    assert client.get('/api/me').json()['profile']['master_id']==master['id']
    assert onboard(client)['id']==master['id']
    catalog=client.get('/api/catalog').json()
    assert len(catalog)==1
    assert catalog[0]['can_edit'] is False
    assert 'owner' not in catalog[0]
    with app.connection() as db:
        assert db.execute('SELECT count(*) FROM services').fetchone()[0]==1


def test_failed_onboarding_leaves_no_partial_master(client):
    register(client,role='master')
    payload=onboarding_payload()
    payload['master']['weekly']={str(i):[] for i in range(7)}
    response=client.post('/api/onboarding/master',headers=csrf(client),json=payload)
    assert response.status_code==422
    assert client.get('/api/masters').json()==[]
    assert client.get('/api/me').json()['profile']['master_id'] is None


def test_two_real_accounts_book_and_master_sees_customer_name(client):
    register(client,username='masteraccount',role='master')
    master=onboard(client)
    customer=TestClient(app.app,base_url='https://testserver')
    assert register(customer,username='customeraccount').status_code==201
    tomorrow=(datetime.now(ZoneInfo('Asia/Tbilisi')).date()+timedelta(days=1)).isoformat()
    params={'master_id':master['id'],'service_id':master['services'][0]['id'],'day':tomorrow}
    anonymous=TestClient(app.app,base_url='https://testserver')
    slot=anonymous.get('/api/calendar/availability',params=params).json()['times'][0]
    response=customer.post('/api/bookings',headers={**csrf(customer),'Idempotency-Key':'real-account-booking'},json={'slot_id':slot['id']})
    assert response.status_code==201
    assert client.get('/api/bookings').json()==[]
    assert len(customer.get('/api/bookings').json())==1
    assert customer.get(f'/api/masters/{master["id"]}/agenda',params={'day':tomorrow}).status_code==403
    agenda=client.get(f'/api/masters/{master["id"]}/agenda',params={'day':tomorrow}).json()
    assert agenda[0]['customer_name']=='Тестовый клиент'
    old_revision=master['revision']
    result=client.post(f'/api/masters/{master["id"]}/publication',headers=csrf(client),json={'revision':old_revision,'published':False})
    assert result.status_code==200
    assert anonymous.get('/api/catalog').json()==[]
    assert anonymous.get('/api/calendar/availability',params=params).status_code==404
    # Unpublishing never deletes an existing customer's booking or cancellation rights.
    assert customer.post('/api/bookings/'+response.json()['id']+'/cancel',headers=csrf(customer)).status_code==200


def test_identity_and_schedule_survive_database_reinitialization(client):
    register(client,username='persistentmaster',role='master')
    master=onboard(client)
    installation=client.get('/api/status').json()['installation_id']
    logout(client)
    app.initialize()
    assert client.get('/api/status').json()['installation_id']==installation
    assert login(client,username='persistentmaster').status_code==200
    assert client.get('/api/masters').json()[0]['id']==master['id']


def test_guide_preference_is_saved_and_bearer_cannot_change_it(client):
    register(client)
    assert client.get('/api/me').json()['profile']['guide_seen'] is False
    assert client.post('/api/account/guide',headers=csrf(client),json={'guide_seen':True}).status_code==200
    assert client.get('/api/me').json()['profile']['guide_seen'] is True
    token=client.post('/api/agent-token',headers=csrf(client)).json()['access_token']
    assert client.post('/api/account/guide',headers={'Authorization':'Bearer '+token},json={'guide_seen':False}).status_code==403
