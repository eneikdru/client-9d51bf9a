from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import app
import scheduling
from test_app import client


def enter(client):
    csrf=client.get('/api/config').json()['csrf']
    response=client.post('/api/demo-session',headers={'X-CSRF-Token':csrf})
    assert response.status_code==200
    return client.get('/api/masters').json()


def headers(client,key=None):
    result={'X-CSRF-Token':client.get('/api/me').json()['csrf']}
    if key:
        result['Idempotency-Key']=key
    return result


@pytest.fixture
def workspace(client):
    master=enter(client)[0]
    day=(datetime.now(ZoneInfo(master['timezone'])).date()+timedelta(days=1)).isoformat()
    response=client.put(f'/api/masters/{master["id"]}/days/{day}',headers=headers(client),json={'revision':master['revision'],'windows':[{'start':'10:00','end':'13:00'},{'start':'14:00','end':'19:00'}]})
    assert response.status_code==200,response.text
    return response.json(),day


def available(client,master,day,service=None):
    response=client.get('/api/calendar/availability',params={'master_id':master['id'],'service_id':service or master['services'][0]['id'],'day':day})
    assert response.status_code==200,response.text
    return response.json()['times']


def book(client,slot,key='calendar-booking'):
    return client.post('/api/bookings',headers=headers(client,key),json={'slot_id':slot['id']})


def clock(timestamp):
    return datetime.fromtimestamp(timestamp,ZoneInfo('Asia/Tbilisi')).strftime('%H:%M')


def master_payload(master,**changes):
    return {**{key:master[key] for key in ['name','timezone','step_minutes','notice_minutes','horizon_days','weekly','revision']},**changes}


def service_payload(service,**changes):
    return {**{key:service[key] for key in ['name','duration_minutes','buffer_minutes','price','revision']},'active':bool(service['active']),**changes}


def test_default_catalog_is_editable_and_seeded_once(client,workspace):
    master,day=workspace
    assert len(client.get('/api/masters').json())==3
    assert len(master['services'])==2
    assert master['can_edit'] is True
    response=client.put(f'/api/masters/{master["id"]}',headers=headers(client),json=master_payload(master,name='Мой мастер',step_minutes=20))
    assert response.status_code==200
    for _ in range(2):
        after=client.get('/api/masters').json()[0]
        assert after['name']=='Мой мастер'
        assert after['step_minutes']==20
        assert day in after['overrides']


def test_availability_uses_service_duration_breaks_and_buffer(client,workspace):
    master,day=workspace
    times=available(client,master,day)
    starts=[clock(slot['start']) for slot in times]
    assert starts[:3]==['10:00','10:15','10:30']
    assert '12:00' in starts
    assert '12:15' not in starts
    assert '13:00' not in starts
    assert starts[-1]=='18:00'
    slot=times[0]
    assert slot['end']-slot['start']==45*60
    assert slot['occupied_until']-slot['end']==15*60
    assert book(client,slot).status_code==201
    remaining=available(client,master,day)
    assert clock(remaining[0]['start'])=='11:00'
    # Another service of the same master also observes this booking's buffer.
    other=available(client,master,day,master['services'][1]['id'])
    assert clock(other[0]['start'])=='11:00'


def test_daily_override_day_off_and_reset(client,workspace):
    master,day=workspace
    response=client.put(f'/api/masters/{master["id"]}/days/{day}',headers=headers(client),json={'revision':master['revision'],'windows':[]})
    assert response.status_code==200
    assert available(client,master,day)==[]
    response=client.post(f'/api/masters/{master["id"]}/days/{day}/reset',headers=headers(client),json={'revision':response.json()['revision']})
    assert response.status_code==200
    assert day not in response.json()['overrides']


@pytest.mark.parametrize('windows',[
    [{'start':'19:00','end':'10:00'}],
    [{'start':'10:00','end':'10:00'}],
    [{'start':'24:00','end':'25:00'}],
    [{'start':'10:00','end':'13:00'},{'start':'12:00','end':'15:00'}],
])
def test_invalid_windows_rejected_without_mutation(client,workspace,windows):
    master,day=workspace
    response=client.put(f'/api/masters/{master["id"]}/days/{day}',headers=headers(client),json={'revision':master['revision'],'windows':windows})
    assert response.status_code==422
    assert client.get('/api/masters').json()[0]['revision']==master['revision']


def test_stale_revision_and_stale_availability_rejected(client,workspace):
    master,day=workspace
    slot=available(client,master,day)[0]
    payload={'revision':master['revision'],'windows':[{'start':'11:00','end':'18:00'}]}
    path=f'/api/masters/{master["id"]}/days/{day}'
    assert client.put(path,headers=headers(client),json=payload).status_code==200
    assert client.put(path,headers=headers(client),json=payload).status_code==409
    assert book(client,slot).status_code==409
    assert clock(available(client,master,day)[0]['start'])=='11:00'


def test_existing_booking_prevents_destructive_schedule_change(client,workspace):
    master,day=workspace
    slot=available(client,master,day)[0]
    assert book(client,slot).status_code==201
    response=client.put(f'/api/masters/{master["id"]}/days/{day}',headers=headers(client),json={'revision':master['revision'],'windows':[]})
    assert response.status_code==409
    assert client.get('/api/masters').json()[0]['revision']==master['revision']
    assert len(available(client,master,day))>0
    assert client.get('/api/bookings').json()[0]['status']=='confirmed'


def test_weekly_change_also_preserves_existing_booking(client,workspace):
    master,day=workspace
    # Remove override so this test exercises the weekly rule, not an override.
    reset=client.post(f'/api/masters/{master["id"]}/days/{day}/reset',headers=headers(client),json={'revision':master['revision']}).json()
    day_obj=date.fromisoformat(day)
    while not reset['weekly'][str(day_obj.weekday())]:
        day_obj+=timedelta(days=1)
    day=day_obj.isoformat()
    assert book(client,available(client,reset,day)[0]).status_code==201
    weekly={str(i):[] for i in range(7)}
    response=client.put(f'/api/masters/{master["id"]}',headers=headers(client),json=master_payload(reset,weekly=weekly))
    assert response.status_code==409


def test_service_changes_preserve_old_booking_snapshot(client,workspace):
    master,day=workspace
    slot=available(client,master,day)[0]
    ident=book(client,slot).json()['id']
    service=master['services'][0]
    path=f'/api/masters/{master["id"]}/services/{service["id"]}'
    payload=service_payload(service,name='Новая услуга',price=12345,duration_minutes=90,buffer_minutes=30)
    assert client.put(path,headers=headers(client),json=payload).status_code==200
    assert client.put(path,headers=headers(client),json=payload).status_code==409
    original=client.get('/api/bookings').json()[0]
    assert original['id']==ident
    assert original['appointment']['price']==5000
    assert original['appointment']['service']=='Стрижка'
    assert book(client,slot).json()['id']==ident
    refreshed=available(client,master,day)
    assert refreshed[0]['price']==12345
    assert refreshed[0]['occupied_until']-refreshed[0]['start']==120*60


def test_batch_schedule_update_is_atomic(client,workspace):
    master,day=workspace
    assert book(client,available(client,master,day)[0]).status_code==201
    following=(date.fromisoformat(day)+timedelta(days=1)).isoformat()
    response=client.put(f'/api/masters/{master["id"]}/days',headers=headers(client),json={'revision':master['revision'],'days':[following,day],'windows':[]})
    assert response.status_code==409
    after=client.get('/api/masters').json()[0]
    assert following not in after['overrides']
    assert after['revision']==master['revision']


def test_demo_tenant_and_live_boundaries(client,workspace):
    master,day=workspace
    slot=available(client,master,day)[0]
    other=TestClient(app.app,base_url='https://testserver')
    others=enter(other)
    assert others[0]['id']!=master['id']
    params={'master_id':master['id'],'service_id':master['services'][0]['id'],'day':day}
    assert other.get('/api/calendar/availability',params=params).status_code==404
    assert book(other,slot).status_code==404
    assert other.put(f'/api/masters/{master["id"]}/days/{day}',headers=headers(other),json={'revision':master['revision'],'windows':[]}).status_code==404
    with app.connection() as db:
        db.execute('INSERT INTO masters VALUES(?,?,?,?,?,?,?,?,1)',('live-master','alice','Live','Asia/Tbilisi',15,0,60,'{}'))
    assert client.get('/api/masters/live-master/agenda',params={'day':day}).status_code==404


def test_mutations_require_csrf_and_agent_cannot_edit(client,workspace):
    master,day=workspace
    path=f'/api/masters/{master["id"]}/days/{day}'
    payload={'revision':master['revision'],'windows':[]}
    assert client.put(path,json=payload).status_code==403
    token=client.post('/api/agent-token',headers=headers(client)).json()['access_token']
    assert client.put(path,json=payload,headers={'Authorization':'Bearer '+token}).status_code==403


def test_dst_gap_is_not_silently_shifted():
    zone=ZoneInfo('Europe/Berlin')
    assert scheduling.local_timestamp(date(2027,3,28),'02:30',zone) is None
    assert scheduling.local_timestamp(date(2027,3,28),'03:30',zone) is not None
    timestamp=scheduling.local_timestamp(date(2026,10,25),'02:30',zone)
    assert datetime.fromtimestamp(timestamp,zone).fold==0


def test_calendar_race_different_start_times_one_booking(client,workspace):
    master,day=workspace
    times=available(client,master,day)
    auth=headers(client)
    def create(pair):
        slot,key=pair
        return client.post('/api/bookings',json={'slot_id':slot['id']},headers={**auth,'Idempotency-Key':key})
    with ThreadPoolExecutor(max_workers=2) as pool:
        result=list(pool.map(create,[(times[0],'first'),(times[1],'second')]))
    assert sorted(r.status_code for r in result)==[201,409]


def test_horizon_timezone_and_strict_numbers(client,workspace):
    master,day=workspace
    for changes in [{'timezone':'Unknown/Zone'},{'step_minutes':0},{'step_minutes':15.5},{'weekly':{'0':[]}}]:
        response=client.put(f'/api/masters/{master["id"]}',headers=headers(client),json=master_payload(master,**changes))
        assert response.status_code==422
    params={'master_id':master['id'],'service_id':master['services'][0]['id'],'day':'2000-01-01'}
    assert client.get('/api/calendar/availability',params=params).status_code==422


def test_create_and_disable_service(client,workspace):
    master,day=workspace
    path=f'/api/masters/{master["id"]}/services'
    payload={'name':'Консультация','duration_minutes':20,'buffer_minutes':5,'price':2000,'active':True}
    result=client.post(path,headers=headers(client),json=payload)
    assert result.status_code==201
    service=result.json()
    slot=available(client,master,day,service['id'])[0]
    assert client.put(path+'/'+service['id'],headers=headers(client),json=service_payload(service,active=False)).status_code==200
    assert book(client,slot).status_code==409
    assert any(s['id']==service['id'] and not s['active'] for s in client.get('/api/masters').json()[0]['services'])
