'use strict';
let csrf='', user=null, masters=[], selectedMaster='', selectedService='', selectedDay='', month='', chosen=null, view='client';
let availabilityRequest=0, agendaRequest=0;
const $=id=>document.getElementById(id);
const weekdayNames=['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'];
const errors={
  'Sign in required':'Войдите или откройте демоверсию.',
  'Invalid CSRF token':'Сессия устарела. Обновите страницу.',
  'Time is already booked':'Это время уже занято. Выберите другое начало записи.',
  'Schedule or service changed. Refresh available times':'Мастер изменил график или услугу. Обновите свободное время.',
  'Settings changed in another tab. Reload before saving':'Настройки изменились в другой вкладке. Обновите данные перед сохранением.',
  'This schedule would exclude an existing booking. Keep its occupied time or arrange cancellation first':'В этом времени уже есть запись. Сохраните для неё рабочий интервал с учётом перерыва после услуги.',
  'Not enough notice for this appointment':'До визита осталось слишком мало времени. Выберите другое окно.',
  'Date outside booking horizon':'Эта дата вне периода доступной записи.',
  'Started or completed appointments cannot be cancelled':'Визит уже начался или завершился. Отмена недоступна.',
  'Only this master can edit the schedule':'Изменять график может только его владелец.'
};
async function api(path,options={}){
  const response=await fetch(new URL(path.replace(/^\//,''),document.baseURI),{...options,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
  let data;try{data=await response.json()}catch{throw Error('Сервис временно недоступен. Обновите данные.')}
  if(!response.ok){let detail=data.detail;if(Array.isArray(detail))detail='Проверьте интервалы, часовой пояс и числовые значения. Рабочие интервалы не должны пересекаться.';const error=Error(errors[detail]||detail||'Не удалось выполнить запрос.');error.status=response.status;throw error}
  return data;
}
function notify(text,error=false){$('message').textContent=text;$('message').classList.toggle('danger',error);$('retry').hidden=!error}
function bind(button,run){button.addEventListener('click',async()=>{button.disabled=true;notify('');try{await run()}catch(error){notify(error.message,true)}finally{button.disabled=false}})}
function master(){return masters.find(item=>item.id===selectedMaster)}
function service(){return master()?.services.find(item=>item.id===selectedService)}
function addDays(day,count){const date=new Date(day+'T12:00:00Z');date.setUTCDate(date.getUTCDate()+count);return date.toISOString().slice(0,10)}
function today(){const zone=master()?.timezone||'Asia/Tbilisi';const parts=new Intl.DateTimeFormat('en',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const part=type=>parts.find(p=>p.type===type).value;return `${part('year')}-${part('month')}-${part('day')}`}
function dayLabel(day){return new Date(day+'T12:00:00Z').toLocaleDateString('ru-RU',{timeZone:'UTC',weekday:'long',day:'numeric',month:'long'})}
function clock(timestamp,zone){return new Date(timestamp*1000).toLocaleTimeString('ru-RU',{timeZone:zone,hour:'2-digit',minute:'2-digit'})}
function money(value){return new Intl.NumberFormat('ru-RU',{style:'currency',currency:'GEL'}).format(value/100)}
function windowsFor(day){const item=master();const weekday=(new Date(day+'T12:00:00Z').getUTCDay()+6)%7;return item.overrides[day]??item.weekly[String(weekday)]}
function clearChoice(){chosen=null;$('confirmation').hidden=true}
function setView(next){view=next;for(const name of ['client','settings','history'])$(name).hidden=name!==next;document.querySelectorAll('[data-tab]').forEach(button=>button.setAttribute('aria-selected',String(button.dataset.tab===next)));if(next==='history')loadBookings().catch(e=>notify(e.message,true));if(next==='settings')renderSettings()}
function renderCalendar(){
  const grid=$('calendar');grid.replaceChildren();$('month-title').textContent=new Date(month+'-01T12:00:00Z').toLocaleDateString('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'});
  for(const title of ['Пн','Вт','Ср','Чт','Пт','Сб','Вс']){const text=document.createElement('span');text.className='dow';text.textContent=title;grid.append(text)}
  const first=month+'-01',offset=(new Date(first+'T12:00:00Z').getUTCDay()+6)%7,start=addDays(first,-offset),minimum=today(),maximum=addDays(minimum,master().horizon_days);
  for(let i=0;i<42;i++){const day=addDays(start,i),button=document.createElement('button');button.textContent=String(Number(day.slice(8)));button.type='button';button.setAttribute('aria-label',dayLabel(day));button.setAttribute('aria-pressed',String(day===selectedDay));button.classList.toggle('selected',day===selectedDay);button.classList.toggle('off',!windowsFor(day).length);button.classList.toggle('today',day===minimum);button.disabled=day<minimum||day>maximum||day.slice(0,7)!==month;button.onclick=()=>chooseDate(day);grid.append(button)}
  $('booking-date').min=minimum;$('booking-date').max=maximum;$('booking-date').value=selectedDay;
}
async function chooseDate(day){if(!day)return;selectedDay=day;month=day.slice(0,7);renderCalendar();clearChoice();try{await loadAvailability()}catch(e){notify(e.message,true)}}
async function loadAvailability(){
  const sequence=++availabilityRequest;clearChoice();$('times').textContent='Проверяем свободное время…';$('date-title').textContent=dayLabel(selectedDay);$('timezone-label').textContent=master().timezone;
  if(!selectedService){$('times').textContent='У мастера пока нет активных услуг.';$('working-hours').textContent='';return}
  const params=new URLSearchParams({master_id:selectedMaster,service_id:selectedService,day:selectedDay});
  const data=await api('/api/calendar/availability?'+params);if(sequence!==availabilityRequest)return;
  $('working-hours').textContent=data.windows.length?'Работает: '+data.windows.map(w=>w.start+'–'+w.end).join(', '):'Выходной день';$('times').replaceChildren();
  if(!data.times.length)$('times').textContent=data.windows.length?'Для этой услуги свободного времени нет. Выберите другую дату.':'На этот день запись закрыта. Выберите другой день.';
  for(const slot of data.times){const button=document.createElement('button');button.textContent=clock(slot.start,data.timezone);button.onclick=()=>{chosen={...slot,key:crypto.randomUUID()};$('times').querySelectorAll('button').forEach(b=>b.classList.toggle('selected',b===button));$('confirmation').hidden=false;$('summary').textContent=slot.service+' · '+slot.master+' · '+dayLabel(selectedDay)+', '+clock(slot.start,data.timezone)+'–'+clock(slot.end,data.timezone);$('summary-price').textContent=money(slot.price);$('confirm').focus()};$('times').append(button)}
}
function renderServices(){
  const select=$('service');select.replaceChildren();const enabled=master().services.filter(item=>item.active);if(!enabled.some(item=>item.id===selectedService))selectedService=enabled[0]?.id||'';
  for(const item of enabled){const option=document.createElement('option');option.value=item.id;option.textContent=item.name+' · '+money(item.price);select.append(option)}select.value=selectedService;
  const item=service();$('service-description').textContent=item?`${item.duration_minutes} мин · перерыв после услуги ${item.buffer_minutes} мин · начало каждые ${master().step_minutes} мин`:'';
}
function fillMaster(){
  $('settings-tab').hidden=!master().can_edit;$('mode-description').textContent=master().demo?'ДЕМОВЕРСИЯ · Ваши изменения сохраняются в этой демосессии. Все мастера вымышленные.':'Актуальное расписание мастера';
  const minimum=today(),maximum=addDays(minimum,master().horizon_days);if(!selectedDay||selectedDay<minimum||selectedDay>maximum)selectedDay=addDays(minimum,1);month=selectedDay.slice(0,7);renderServices();renderCalendar();if(view==='settings'&&master().can_edit)renderSettings();else if(view==='settings')setView('client');loadAvailability().catch(e=>notify(e.message,true));
}
async function loadMasters(){
  masters=await api('/api/masters');const select=$('master');select.replaceChildren();for(const item of masters){const option=document.createElement('option');option.value=item.id;option.textContent=item.name;select.append(option)}
  if(!masters.some(item=>item.id===selectedMaster))selectedMaster=masters[0]?.id||'';select.value=selectedMaster;
  if(master())fillMaster();else{$('workspace').hidden=true;notify('Мастера пока не добавлены. В демоверсии доступен пример каталога.')}
}
async function boot(){
  const settings=await api('/api/config');csrf=settings.csrf;user=null;try{user=await api('/api/me');csrf=user.csrf}catch(error){if(error.status!==401)throw error}
  $('welcome').hidden=!!user;$('workspace').hidden=!user;$('logout').hidden=!user;$('demo').hidden=!!user;$('google').hidden=!!user||!settings.providers.google;$('facebook').hidden=!!user||!settings.providers.facebook;$('session-label').textContent=user?.mode==='demo'?'ДЕМОВЕРСИЯ':user?'ЛИЧНЫЙ КАБИНЕТ':'ОНЛАЙН-ЗАПИСЬ';$('login-info').textContent=settings.providers.google||settings.providers.facebook?'Войдите или проверьте сервис на демонстрационных данных.':'Внешний вход пока не подключён. Демоверсия работает без Google и Facebook.';
  if(user){await loadMasters();if(view==='history')await loadBookings()}
}
async function loadBookings(){
  const rows=await api('/api/bookings');const container=$('bookings');container.replaceChildren();if(!rows.length)container.textContent='У вас пока нет записей.';
  for(const item of rows){const row=document.createElement('article');row.className='booking';const p=document.createElement('p'),detail=item.appointment;let zone=masters.find(m=>m.name===detail?.master)?.timezone||'Asia/Tbilisi';p.textContent=detail?detail.service+' · '+detail.master+' · '+new Date(detail.start*1000).toLocaleString('ru-RU',{timeZone:zone,dateStyle:'medium',timeStyle:'short'})+' · '+money(detail.price):item.slot;row.append(p);const status=document.createElement('span');status.className='tag';status.textContent=item.status==='cancelled'?'Отменена':detail?.start*1000<=Date.now()?'Визит начался / завершён':'Подтверждена';row.append(status);if(item.status==='confirmed'&&(!detail||detail.start*1000>Date.now())){const button=document.createElement('button');button.className='secondary';button.textContent='Отменить запись';bind(button,async()=>{await api('/api/bookings/'+encodeURIComponent(item.id)+'/cancel',{method:'POST'});await loadBookings();await loadAvailability();notify('Запись отменена. Время снова свободно.')});row.append(document.createElement('br'),button)}container.append(row)}
}
function addWindow(container,window={start:'10:00',end:'19:00'},dirty=()=>{}){
  const row=document.createElement('div');row.className='window';for(const [key,label] of [['start','Начало интервала'],['end','Конец интервала']]){const input=document.createElement('input');input.type='time';input.required=true;input.value=window[key];input.dataset.boundary=key;input.setAttribute('aria-label',label);input.oninput=dirty;row.append(input)}const remove=document.createElement('button');remove.type='button';remove.className='secondary';remove.textContent='Убрать';remove.onclick=()=>{row.remove();dirty()};row.append(remove);container.append(row);
}
function readWindows(container){return [...container.querySelectorAll('.window')].map(row=>({start:row.querySelector('[data-boundary=start]').value,end:row.querySelector('[data-boundary=end]').value}))}
function dirtyDay(){$('day-dirty').hidden=false}
function renderDayEditor(){
  const day=$('work-date').value;if(!day)return;const container=$('day-windows');container.replaceChildren();for(const window of windowsFor(day))addWindow(container,window,dirtyDay);$('day-source').textContent=Object.hasOwn(master().overrides,day)?'Индивидуальный график на эту дату':'По шаблону недели';$('day-dirty').hidden=true;$('repeat-week').checked=false;loadAgenda().catch(e=>notify(e.message,true));
}
async function loadAgenda(){
  const sequence=++agendaRequest,id=selectedMaster,day=$('work-date').value;const rows=await api('/api/masters/'+encodeURIComponent(id)+'/agenda?'+new URLSearchParams({day}));if(sequence!==agendaRequest)return;$('agenda').replaceChildren();if(!rows.length)$('agenda').textContent='Записей на этот день пока нет.';for(const item of rows){const p=document.createElement('p');p.className='booking';p.textContent=clock(item.start,master().timezone)+'–'+clock(item.end,master().timezone)+' · '+item.service+' · '+(item.status==='confirmed'?'подтверждена':'отменена')+' (включая перерыв)';$('agenda').append(p)}
}
function inputField(title,value,type='text',min,max){const label=document.createElement('label');label.textContent=title;const input=document.createElement('input');input.type=type;input.value=value;input.required=true;if(min!==undefined)input.min=min;if(max!==undefined)input.max=max;if(type==='text')input.maxLength=80;label.append(input);return {label,input}}
function renderServiceSettings(){
  $('service-settings').replaceChildren();for(const item of master().services){serviceEditor(item)}
}
function serviceEditor(item){
  const form=document.createElement('form');form.className='service-row';const fields=document.createElement('div');fields.className='fields';const name=inputField('Название',item?.name||''),duration=inputField('Длительность, мин',item?.duration_minutes||60,'number',5,480),buffer=inputField('Перерыв после, мин',item?.buffer_minutes??15,'number',0,180),price=inputField('Цена, GEL',item?item.price/100:50,'number',0,100000);price.input.step='0.01';for(const field of [name,duration,buffer,price])fields.append(field.label);form.append(fields);const check=document.createElement('label');check.className='check';const active=document.createElement('input');active.type='checkbox';active.checked=item?!!item.active:true;check.append(active,document.createTextNode('Доступна для новых записей'));form.append(check);const save=document.createElement('button');save.textContent=item?'Сохранить услугу':'Добавить услугу';save.type='submit';form.append(save);
  form.onsubmit=async event=>{event.preventDefault();save.disabled=true;try{const payload={name:name.input.value,duration_minutes:Number(duration.input.value),buffer_minutes:Number(buffer.input.value),price:Math.round(Number(price.input.value)*100),active:active.checked};const path='/api/masters/'+encodeURIComponent(selectedMaster)+'/services'+(item?'/'+encodeURIComponent(item.id):'');if(item)payload.revision=item.revision;await api(path,{method:item?'PUT':'POST',body:JSON.stringify(payload)});await loadMasters();notify('Услуга сохранена. Существующие записи не изменены.')}catch(e){notify(e.message,true)}finally{save.disabled=false}};$('service-settings').append(form);
}
function renderSettings(){
  if(!master()?.can_edit)return;const item=master();$('master-name').value=item.name;$('master-timezone').value=item.timezone;$('step').value=item.step_minutes;$('notice').value=item.notice_minutes;$('horizon').value=item.horizon_days;
  const minimum=today();if(!$('work-date').value||$('work-date').value<minimum)$('work-date').value=selectedDay;$('work-date').min=minimum;$('work-date').max=addDays(minimum,item.horizon_days);renderDayEditor();
  $('weekly').replaceChildren();for(let day=0;day<7;day++){const row=document.createElement('div');row.className='day-row';const title=document.createElement('strong');title.textContent=weekdayNames[day];const controls=document.createElement('div');const windows=document.createElement('div');windows.dataset.weekday=day;for(const window of item.weekly[String(day)])addWindow(windows,window);const add=document.createElement('button');add.type='button';add.textContent='+ Интервал';add.className='secondary';add.onclick=()=>addWindow(windows);controls.append(windows,add);row.append(title,controls);$('weekly').append(row)}renderServiceSettings();
}
bind($('demo'),async()=>{await api('/api/demo-session',{method:'POST'});await boot();notify('Демоверсия готова. График и услуги можно менять в кабинете мастера.')});
bind($('logout'),async()=>{await api('/api/logout',{method:'POST'});masters=[];selectedMaster='';selectedService='';await boot();notify('Вы вышли.')});
for(const provider of ['google','facebook'])$(provider).onclick=()=>{location.href=new URL('auth/'+provider,document.baseURI).href};
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{notify('');setView(button.dataset.tab)});
$('master').onchange=()=>{selectedMaster=$('master').value;selectedService='';$('work-date').value='';fillMaster()};
$('service').onchange=()=>{selectedService=$('service').value;renderServices();loadAvailability().catch(e=>notify(e.message,true))};
$('booking-date').onchange=()=>chooseDate($('booking-date').value);
for(const [id,direction] of [['month-prev',-1],['month-next',1]])$(id).onclick=()=>{const date=new Date(month+'-01T12:00:00Z');date.setUTCMonth(date.getUTCMonth()+direction);month=date.toISOString().slice(0,7);renderCalendar()};
bind($('confirm'),async()=>{if(!chosen)return;const snapshot=chosen;await api('/api/bookings',{method:'POST',headers:{'Idempotency-Key':snapshot.key},body:JSON.stringify({slot_id:snapshot.id})});await loadAvailability();await loadBookings();setView('history');notify('Запись подтверждена. Вы найдёте её здесь, в разделе «Мои записи».')});
$('work-date').onchange=renderDayEditor;
for(const [id,offset] of [['work-today',0],['work-tomorrow',1]])$(id).onclick=()=>{$('work-date').value=addDays(today(),offset);renderDayEditor()};
$('day-off').onclick=()=>{$('day-windows').replaceChildren();dirtyDay()};$('day-add').onclick=()=>{addWindow($('day-windows'),undefined,dirtyDay);dirtyDay()};
bind($('save-day'),async()=>{const day=$('work-date').value,days=Array.from({length:$('repeat-week').checked?7:1},(_,i)=>addDays(day,i));const result=await api('/api/masters/'+encodeURIComponent(selectedMaster)+'/days',{method:'PUT',body:JSON.stringify({revision:master().revision,days,windows:readWindows($('day-windows'))})});masters=masters.map(m=>m.id===result.id?result:m);renderDayEditor();renderCalendar();await loadAvailability();notify(days.length===1?'График дня сохранён. Остальные даты не изменены.':'График сохранён на 7 выбранных дней.')});
bind($('reset-day'),async()=>{const result=await api('/api/masters/'+encodeURIComponent(selectedMaster)+'/days/'+$('work-date').value+'/reset',{method:'POST',body:JSON.stringify({revision:master().revision})});masters=masters.map(m=>m.id===result.id?result:m);renderDayEditor();renderCalendar();await loadAvailability();notify('Для этой даты снова действует шаблон недели.')});
$('add-service').onclick=()=>serviceEditor(null);
$('profile-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter;button.disabled=true;try{const weekly={};$('weekly').querySelectorAll('[data-weekday]').forEach(container=>weekly[container.dataset.weekday]=readWindows(container));const payload={name:$('master-name').value,timezone:$('master-timezone').value,step_minutes:Number($('step').value),notice_minutes:Number($('notice').value),horizon_days:Number($('horizon').value),weekly,revision:master().revision};await api('/api/masters/'+encodeURIComponent(selectedMaster),{method:'PUT',body:JSON.stringify(payload)});await loadMasters();notify('Профиль и шаблон недели сохранены. Индивидуальные изменения дат остались прежними.')}catch(e){notify(e.message,true)}finally{button.disabled=false}};
bind($('retry'),async()=>{await boot();notify('Данные обновлены.')});
boot().catch(error=>notify(error.message,true));
