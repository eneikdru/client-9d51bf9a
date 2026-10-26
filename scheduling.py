"""Mutable master calendars; immutable appointment snapshots stay in slots."""
import hashlib
import json
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Window(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    end: str = Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')

    @model_validator(mode='after')
    def ordered(self):
        if self.start >= self.end:
            raise ValueError('Working interval must end after it starts; split overnight shifts into separate days')
        return self


def validate_windows(windows):
    ordered = sorted(windows, key=lambda item: item.start)
    if any(a.end > b.start for a, b in zip(ordered, ordered[1:])):
        raise ValueError('Working intervals must not overlap')
    return ordered


class MasterInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    timezone: str = 'Asia/Tbilisi'
    step_minutes: int = Field(default=15, ge=5, le=120, strict=True)
    notice_minutes: int = Field(default=60, ge=0, le=10080, strict=True)
    horizon_days: int = Field(default=60, ge=1, le=180, strict=True)
    weekly: dict[str, list[Window]]

    @field_validator('name')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Name must not be blank')
        return value.strip()

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Unknown IANA timezone') from None
        return value

    @field_validator('weekly')
    @classmethod
    def week(cls, value):
        if set(value) != set(map(str, range(7))):
            raise ValueError('Provide weekdays 0 (Monday) through 6 (Sunday)')
        for key in value:
            if len(value[key]) > 8:
                raise ValueError('At most eight intervals per day')
            value[key] = validate_windows(value[key])
        return value


class MasterUpdate(MasterInput):
    revision: int = Field(ge=1, strict=True)


class ServiceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    duration_minutes: int = Field(ge=5, le=480, strict=True)
    buffer_minutes: int = Field(default=15, ge=0, le=180, strict=True)
    price: int = Field(ge=0, le=10000000, strict=True)
    active: bool = True

    @field_validator('name')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Name must not be blank')
        return value.strip()


class ServiceUpdate(ServiceInput):
    revision: int = Field(ge=1, strict=True)


class DayUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1, strict=True)
    windows: list[Window] = Field(max_length=8)

    @field_validator('windows')
    @classmethod
    def ordered(cls, value):
        return validate_windows(value)


class ResetDay(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1, strict=True)


class DaysUpdate(DayUpdate):
    days: list[date] = Field(min_length=1,max_length=14)

    @field_validator('days')
    @classmethod
    def unique_days(cls,value):
        if len(value)!=len(set(value)):
            raise ValueError('Dates must be unique')
        return value


class Publication(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision:int=Field(ge=1,strict=True)
    published:bool


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS masters(
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
      timezone TEXT NOT NULL, step_minutes INTEGER NOT NULL,
      notice_minutes INTEGER NOT NULL, horizon_days INTEGER NOT NULL,
      weekly TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS services(
      id TEXT PRIMARY KEY, master_id TEXT NOT NULL REFERENCES masters(id),
      name TEXT NOT NULL, duration_minutes INTEGER NOT NULL CHECK(duration_minutes>0),
      buffer_minutes INTEGER NOT NULL CHECK(buffer_minutes>=0), price INTEGER NOT NULL CHECK(price>=0),
      active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS day_overrides(
      master_id TEXT NOT NULL REFERENCES masters(id), day TEXT NOT NULL,
      windows TEXT NOT NULL, PRIMARY KEY(master_id,day));
    CREATE TABLE IF NOT EXISTS calendar_slots(
      slot_id TEXT PRIMARY KEY REFERENCES slots(id), master_id TEXT NOT NULL REFERENCES masters(id),
      service_id TEXT NOT NULL REFERENCES services(id), master_revision INTEGER NOT NULL,
      service_revision INTEGER NOT NULL, day TEXT NOT NULL, appointment_end INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS master_calendar_slots ON calendar_slots(master_id, day);
    CREATE TABLE IF NOT EXISTS master_publication(master_id TEXT PRIMARY KEY REFERENCES masters(id), published INTEGER NOT NULL CHECK(published IN (0,1)));
    ''')


def default_week():
    return {str(day): ([{'start':'10:00','end':'13:00'},{'start':'14:00','end':'19:00'}] if day < 5
                      else [{'start':'10:00','end':'15:00'}] if day == 5 else []) for day in range(7)}


def seed_workspace(db, user):
    if not user.startswith('demo:'):
        return
    namespace = 'demo:calendar:' + hashlib.sha256(user.encode()).hexdigest()[:24]
    for index, (name, services) in enumerate([
        ('Анна · демо', [('Стрижка',45,15,5000),('Укладка',30,10,3500)]),
        ('Нино · демо', [('Маникюр',60,15,7000),('Маникюр с покрытием',90,15,9500)]),
        ('Георгий · демо', [('Массаж',60,15,9000),('Массаж спины',30,10,5000)]),
    ]):
        master_id = f'{namespace}:{index}'
        # A marker row prevents deleted/edited service settings being seeded again.
        added = db.execute('INSERT OR IGNORE INTO masters VALUES(?,?,?,?,?,?,?,?,1)',
                           (master_id,user,name,'Asia/Tbilisi',15,60,60,json.dumps(default_week()))).rowcount
        if added:
            for number,(title,duration,buffer,price) in enumerate(services):
                db.execute('INSERT INTO services VALUES(?,?,?,?,?,?,1,1)',
                           (f'{master_id}:s{number}',master_id,title,duration,buffer,price))


def master_for(db, master_id, user, edit=False):
    row = db.execute('SELECT * FROM masters WHERE id=?', (master_id,)).fetchone()
    if not row or row['owner'].startswith('demo:') != user.startswith('demo:') or (row['owner'].startswith('demo:') and row['owner'] != user):
        raise HTTPException(404, 'Master not found')
    if edit and row['owner'] != user:
        raise HTTPException(403, 'Only this master can edit the schedule')
    if not row['owner'].startswith('demo:') and row['owner']!=user:
        public=db.execute('SELECT published FROM master_publication WHERE master_id=?',(master_id,)).fetchone()
        if not public or not public['published']:
            raise HTTPException(404,'Master not found')
    return row


def revision_check(row, revision):
    if row['revision'] != revision:
        raise HTTPException(409, 'Settings changed in another tab. Reload before saving')


def serialize_master(db, row, user):
    result = dict(row)
    result.pop('owner')
    result['weekly'] = json.loads(row['weekly'])
    result['can_edit'] = row['owner'] == user
    result['demo'] = row['owner'].startswith('demo:')
    publication=db.execute('SELECT published FROM master_publication WHERE master_id=?',(row['id'],)).fetchone()
    result['published']=result['demo'] or bool(publication and publication['published'])
    result['services'] = [dict(service) for service in db.execute('SELECT * FROM services WHERE master_id=? ORDER BY id', (row['id'],))]
    result['overrides'] = {r['day']:json.loads(r['windows']) for r in db.execute('SELECT * FROM day_overrides WHERE master_id=?', (row['id'],))}
    return result


def windows_for(db, master, day):
    override = db.execute('SELECT windows FROM day_overrides WHERE master_id=? AND day=?', (master['id'], day.isoformat())).fetchone()
    return json.loads(override['windows']) if override else json.loads(master['weekly'])[str(day.weekday())]


def local_timestamp(day, clock, zone):
    naive = datetime.fromisoformat(day.isoformat() + 'T' + clock)
    local = naive.replace(tzinfo=zone, fold=0)
    # Nonexistent DST wall times must never turn into a different clock time.
    if datetime.fromtimestamp(local.timestamp(), zone).replace(tzinfo=None) != naive:
        return None
    return int(local.timestamp())


def day_intervals(db, master, day):
    zone = ZoneInfo(master['timezone'])
    result=[]
    for window in windows_for(db,master,day):
        start,end = (local_timestamp(day,window[key],zone) for key in ('start','end'))
        if start is not None and end is not None and end>start:
            result.append((start,end,window['start']))
    return result


def check_day(master, day):
    today = datetime.now(ZoneInfo(master['timezone'])).date()
    if day < today or day > today+timedelta(days=master['horizon_days']):
        raise HTTPException(422, 'Date outside booking horizon')


def ensure_existing_fit(db, master):
    rows=db.execute("SELECT s.start,s.end FROM bookings b JOIN slots s ON s.id=b.slot WHERE b.status='confirmed' AND s.master=? AND s.end>?",(master['id'],int(time.time())))
    for row in rows:
        day=datetime.fromtimestamp(row['start'],ZoneInfo(master['timezone'])).date()
        if not any(start<=row['start'] and row['end']<=end for start,end,_ in day_intervals(db,master,day)):
            raise HTTPException(409, 'This schedule would exclude an existing booking. Keep its occupied time or arrange cancellation first')


def validate_calendar_slot(db, slot, user):
    link=db.execute('SELECT * FROM calendar_slots WHERE slot_id=?',(slot['id'],)).fetchone()
    if not link:
        return
    master=master_for(db,link['master_id'],user)
    service=db.execute('SELECT * FROM services WHERE id=?',(link['service_id'],)).fetchone()
    if master['revision']!=link['master_revision'] or service['revision']!=link['service_revision'] or not service['active']:
        raise HTTPException(409, 'Schedule or service changed. Refresh available times')
    day=date.fromisoformat(link['day'])
    check_day(master,day)
    if slot['start'] < int(time.time())+master['notice_minutes']*60:
        raise HTTPException(409, 'Not enough notice for this appointment')
    if not any(start<=slot['start'] and slot['end']<=end for start,end,_ in day_intervals(db,master,day)):
        raise HTTPException(409, 'Time is outside working hours')


def router(connection, owner):
    routes=APIRouter(prefix='/api')

    def edit_owner(request):
        user=owner(request)
        if request.headers.get('authorization'):
            raise HTTPException(403,'Settings changes require a browser session')
        return user

    @routes.get('/masters')
    def masters(request: Request):
        user=owner(request)
        with connection() as db:
            seed_workspace(db,user)
            rows=db.execute("SELECT * FROM masters WHERE owner=?" if user.startswith('demo:') else "SELECT * FROM masters m WHERE owner NOT LIKE 'demo:%' AND (owner=? OR EXISTS (SELECT 1 FROM master_publication p WHERE p.master_id=m.id AND p.published=1)) ORDER BY CASE WHEN owner=? THEN 0 ELSE 1 END,name", (user,) if user.startswith('demo:') else (user,user))
            return [serialize_master(db,row,user) for row in rows]

    @routes.get('/catalog')
    def catalog():
        with connection() as db:
            rows=db.execute("SELECT m.* FROM masters m JOIN master_publication p ON p.master_id=m.id WHERE p.published=1 AND m.owner NOT LIKE 'demo:%' ORDER BY m.name")
            return [serialize_master(db,row,'') for row in rows]

    @routes.post('/masters/{master_id}/publication')
    def publication(master_id:str,data:Publication,request:Request):
        user=edit_owner(request)
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            master=master_for(db,master_id,user,True)
            revision_check(master,data.revision)
            if data.published and (not any(json.loads(master['weekly']).values()) or not db.execute('SELECT 1 FROM services WHERE master_id=? AND active=1',(master_id,)).fetchone()):
                raise HTTPException(422,'Add an active service and at least one working day')
            db.execute('INSERT INTO master_publication VALUES(?,?) ON CONFLICT(master_id) DO UPDATE SET published=excluded.published',(master_id,int(data.published)))
            db.execute('UPDATE masters SET revision=revision+1 WHERE id=?',(master_id,))
            return serialize_master(db,master_for(db,master_id,user),user)

    @routes.post('/masters',status_code=201)
    def create_master(data: MasterInput,request: Request):
        user=edit_owner(request)
        master_id=('demo:' if user.startswith('demo:') else '')+'master:'+secrets.token_urlsafe(16)
        with connection() as db:
            db.execute('INSERT INTO masters VALUES(?,?,?,?,?,?,?,?,1)',(master_id,user,data.name,data.timezone,data.step_minutes,data.notice_minutes,data.horizon_days,json.dumps({k:[w.model_dump() for w in v] for k,v in data.weekly.items()})))
            return serialize_master(db,master_for(db,master_id,user),user)

    @routes.put('/masters/{master_id}')
    @routes.post('/masters/{master_id}')
    def update_master(master_id:str,data:MasterUpdate,request:Request):
        user=edit_owner(request)
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=master_for(db,master_id,user,True)
            revision_check(previous,data.revision)
            weekly=json.dumps({k:[w.model_dump() for w in v] for k,v in data.weekly.items()})
            db.execute('UPDATE masters SET name=?,timezone=?,step_minutes=?,notice_minutes=?,horizon_days=?,weekly=?,revision=revision+1 WHERE id=?',(data.name,data.timezone,data.step_minutes,data.notice_minutes,data.horizon_days,weekly,master_id))
            current=master_for(db,master_id,user)
            ensure_existing_fit(db,current)
            return serialize_master(db,current,user)

    @routes.put('/masters/{master_id}/days/{day}')
    @routes.post('/masters/{master_id}/days/{day}')
    def update_day(master_id:str,day:date,data:DayUpdate,request:Request):
        user=edit_owner(request)
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            master=master_for(db,master_id,user,True)
            revision_check(master,data.revision)
            check_day(master,day)
            db.execute('INSERT INTO day_overrides VALUES(?,?,?) ON CONFLICT(master_id,day) DO UPDATE SET windows=excluded.windows',(master_id,day.isoformat(),json.dumps([w.model_dump() for w in data.windows])))
            ensure_existing_fit(db,master)
            db.execute('UPDATE masters SET revision=revision+1 WHERE id=?',(master_id,))
            return serialize_master(db,master_for(db,master_id,user),user)

    @routes.post('/masters/{master_id}/days/{day}/reset')
    def reset_day(master_id:str,day:date,data:ResetDay,request:Request):
        user=edit_owner(request)
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            master=master_for(db,master_id,user,True)
            revision_check(master,data.revision)
            check_day(master,day)
            db.execute('DELETE FROM day_overrides WHERE master_id=? AND day=?',(master_id,day.isoformat()))
            ensure_existing_fit(db,master)
            db.execute('UPDATE masters SET revision=revision+1 WHERE id=?',(master_id,))
            return serialize_master(db,master_for(db,master_id,user),user)

    @routes.put('/masters/{master_id}/days')
    @routes.post('/masters/{master_id}/days')
    def update_days(master_id:str,data:DaysUpdate,request:Request):
        user=edit_owner(request)
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            master=master_for(db,master_id,user,True)
            revision_check(master,data.revision)
            for day in data.days:
                check_day(master,day)
                db.execute('INSERT INTO day_overrides VALUES(?,?,?) ON CONFLICT(master_id,day) DO UPDATE SET windows=excluded.windows',(master_id,day.isoformat(),json.dumps([w.model_dump() for w in data.windows])))
            ensure_existing_fit(db,master)
            db.execute('UPDATE masters SET revision=revision+1 WHERE id=?',(master_id,))
            return serialize_master(db,master_for(db,master_id,user),user)

    @routes.post('/masters/{master_id}/services',status_code=201)
    def create_service(master_id:str,data:ServiceInput,request:Request):
        user=edit_owner(request)
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            master_for(db,master_id,user,True)
            ident=master_id+':s:'+secrets.token_urlsafe(10)
            db.execute('INSERT INTO services VALUES(?,?,?,?,?,?,?,1)',(ident,master_id,data.name,data.duration_minutes,data.buffer_minutes,data.price,int(data.active)))
            return dict(db.execute('SELECT * FROM services WHERE id=?',(ident,)).fetchone())

    @routes.put('/masters/{master_id}/services/{service_id}')
    @routes.post('/masters/{master_id}/services/{service_id}')
    def update_service(master_id:str,service_id:str,data:ServiceUpdate,request:Request):
        user=edit_owner(request)
        with connection() as db:
            db.execute('BEGIN IMMEDIATE')
            master_for(db,master_id,user,True)
            previous=db.execute('SELECT * FROM services WHERE id=? AND master_id=?',(service_id,master_id)).fetchone()
            if not previous:
                raise HTTPException(404,'Service not found')
            revision_check(previous,data.revision)
            db.execute('UPDATE services SET name=?,duration_minutes=?,buffer_minutes=?,price=?,active=?,revision=revision+1 WHERE id=?',(data.name,data.duration_minutes,data.buffer_minutes,data.price,int(data.active),service_id))
            return dict(db.execute('SELECT * FROM services WHERE id=?',(service_id,)).fetchone())

    @routes.get('/calendar/availability')
    def availability(request:Request,master_id:str,service_id:str,day:date):
        user=owner(request) if request.session.get('user') or request.headers.get('authorization') else ''
        with connection() as db:
            # Generation and configuration edits use the same writer lock, so a
            # response never materializes a mixture of two schedule revisions.
            db.execute('BEGIN IMMEDIATE')
            master=master_for(db,master_id,user)
            check_day(master,day)
            service=db.execute('SELECT * FROM services WHERE id=? AND master_id=? AND active=1',(service_id,master_id)).fetchone()
            if not service:
                raise HTTPException(404,'Service not found')
            busy=[(r['start'],r['end']) for r in db.execute("SELECT s.start,s.end FROM bookings b JOIN slots s ON s.id=b.slot WHERE b.status='confirmed' AND s.master=?",(master_id,))]
            duration=service['duration_minutes']*60
            occupied=duration+service['buffer_minutes']*60
            earliest=int(time.time())+master['notice_minutes']*60
            result=[]
            for window_start,window_end,clock in day_intervals(db,master,day):
                naive=datetime.fromisoformat(day.isoformat()+'T'+clock)
                end_local=datetime.fromtimestamp(window_end,ZoneInfo(master['timezone'])).replace(tzinfo=None)
                while naive<end_local:
                    start=local_timestamp(day,naive.strftime('%H:%M'),ZoneInfo(master['timezone']))
                    naive+=timedelta(minutes=master['step_minutes'])
                    if start is None or start<earliest or start+occupied>window_end or any(start<b and start+occupied>a for a,b in busy):
                        continue
                    fingerprint=f'{master_id}|{service_id}|{start}|{master["revision"]}|{service["revision"]}'
                    slot_id=('demo:' if user.startswith('demo:') else '')+'cal:'+hashlib.sha256(fingerprint.encode()).hexdigest()
                    db.execute('INSERT OR IGNORE INTO slots VALUES(?,?,?,?,?,?)',(slot_id,master_id,service['name'],start,start+occupied,service['price']))
                    db.execute('INSERT OR IGNORE INTO calendar_slots VALUES(?,?,?,?,?,?,?)',(slot_id,master_id,service_id,master['revision'],service['revision'],day.isoformat(),start+duration))
                    result.append({'id':slot_id,'master':master['name'],'service':service['name'],'start':start,'end':start+duration,'occupied_until':start+occupied,'price':service['price']})
            return {'day':day.isoformat(),'timezone':master['timezone'],'revision':master['revision'],'windows':windows_for(db,master,day),'times':result}

    @routes.get('/masters/{master_id}/agenda')
    def agenda(master_id:str,day:date,request:Request):
        user=owner(request)
        with connection() as db:
            master=master_for(db,master_id,user,True)
            zone=ZoneInfo(master['timezone'])
            start=int(datetime.combine(day,datetime.min.time(),zone).timestamp())
            end=int(datetime.combine(day+timedelta(days=1),datetime.min.time(),zone).timestamp())
            rows=db.execute("SELECT b.id,b.status,s.service,s.start,s.end,COALESCE(p.display_name,'Клиент') AS customer_name FROM bookings b JOIN slots s ON s.id=b.slot LEFT JOIN profiles p ON p.owner=b.owner WHERE s.master=? AND s.start>=? AND s.start<? ORDER BY s.start",(master_id,start,end))
            return [dict(row) for row in rows]

    return routes
