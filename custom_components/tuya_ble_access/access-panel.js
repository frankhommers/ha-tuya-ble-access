/* Local HA access manager. Secrets are input-only, never fetched or rendered. */
const WORDS = {
  nl: {
    title: 'Toegang', subtitle: 'Beheer wie dit slot kan openen.', lock: 'Slot', add: 'Toegang toevoegen', refresh: 'Vernieuwen',
    label: 'Label', person: 'Persoon', type: 'Type', status: 'Status', validity: 'Geldigheid', actions: 'Acties',
    finger: 'Vinger', chooseFinger: 'Kies een vinger',
    right_thumb: 'Rechterduim', right_index: 'Rechterwijsvinger', right_middle: 'Rechtermiddelvinger', right_ring: 'Rechterringvinger', right_pinky: 'Rechterpink',
    left_thumb: 'Linkerduim', left_index: 'Linkerwijsvinger', left_middle: 'Linkermiddelvinger', left_ring: 'Linkerringvinger', left_pinky: 'Linkerpink',
    fingerBusy: 'Vingerafdruk inschrijven… leg dezelfde vinger telkens op de sensor en til hem weer op wanneer het slot daarom vraagt.',
    pin: 'Pincode', temporary_pin: 'Tijdelijke pincode', card: 'Kaart', fingerprint: 'Vingerafdruk', other: 'Overig',
    active: 'Actief', paused: 'Gepauzeerd', unknown: 'Onzeker', expired: 'Verlopen', scheduled: 'Gepland', removed: 'Verwijderd', replaced: 'Vervangen',
    pause: 'Pauzeren', resume: 'Hervatten', edit: 'Bewerken', save: 'Opslaan', cancel: 'Annuleren', none: 'Geen persoon',
    empty: 'Nog geen toegang opgeslagen voor dit slot.', noLocks: 'Geen slot beschikbaar. Voeg eerst een slot toe aan Tuya BLE Access.',
    busy: 'Bezig… houd het slot wakker en binnen Bluetooth-bereik.', loading: 'Laden…', unknownValidity: 'Niet vastgelegd', until: 't/m',
    editTitle: 'Toegang bewerken', editHelp: 'Label en persoon zijn onafhankelijk. Dit wijzigt de inschrijving op het slot niet.',
    pinInput: 'Pincode', existingPin: 'Bestaande pincode', pinHelp: 'HA onthoudt deze code lokaal voor pauzeren en hervatten.',
    existingHelp: 'Voer één keer dezelfde bestaande code in. Een andere code zou de code op het slot wijzigen.',
    remembered: 'Pincode opgeslagen', missingPin: 'Pincode nog niet opgeslagen', start: 'Geldig vanaf', end: 'Geldig tot',
    enrollHelp: 'Maak het slot wakker. Houd bij een kaart de kaart bij de lezer; raak bij een vingerafdruk de sensor aan als het slot daarom vraagt.',
    saved: 'Opgeslagen.', done: 'Het slot heeft de wijziging bevestigd.', admin: 'Dit beheer is alleen beschikbaar voor beheerders.',
    policy: 'Oorspronkelijke geldigheid ontbreekt; deze oudere inschrijving kan nog niet worden gepauzeerd.',
    unsupported: 'Pauzeren is niet beschikbaar voor dit toegangstype op dit slot.', invalid: 'Controleer de ingevulde gegevens.',
    stale: 'Deze inschrijving is niet meer actueel.', expiredHelp: 'De oorspronkelijke geldigheid is verstreken.',
    uncertain: 'De laatste wijziging is niet bevestigd. Kies opnieuw Pauzeren of Hervatten.',
    failed: 'De opdracht is niet bevestigd. Controleer de status en probeer opnieuw.', enroll: 'Inschrijven', delete: 'Verwijderen', deleteHelp: 'Verwijder deze inschrijving definitief van het slot. Opnieuw gebruiken vereist opnieuw inschrijven.',
  },
  en: {
    title: 'Access', subtitle: 'Manage who can open this lock.', lock: 'Lock', add: 'Add access', refresh: 'Refresh',
    label: 'Label', person: 'Person', type: 'Type', status: 'Status', validity: 'Validity', actions: 'Actions',
    finger: 'Finger', chooseFinger: 'Choose a finger',
    right_thumb: 'Right thumb', right_index: 'Right index', right_middle: 'Right middle', right_ring: 'Right ring', right_pinky: 'Right pinky',
    left_thumb: 'Left thumb', left_index: 'Left index', left_middle: 'Left middle', left_ring: 'Left ring', left_pinky: 'Left pinky',
    fingerBusy: 'Enrolling fingerprint… place the same finger on the sensor and lift it again whenever the lock prompts you.',
    pin: 'PIN', temporary_pin: 'Temporary PIN', card: 'Card', fingerprint: 'Fingerprint', other: 'Other',
    active: 'Active', paused: 'Paused', unknown: 'Uncertain', expired: 'Expired', scheduled: 'Scheduled', removed: 'Removed', replaced: 'Replaced',
    pause: 'Pause', resume: 'Resume', edit: 'Edit', save: 'Save', cancel: 'Cancel', none: 'No person',
    empty: 'No access stored for this lock yet.', noLocks: 'No lock available. Add a lock to Tuya BLE Access first.',
    busy: 'Working… keep the lock awake and within Bluetooth range.', loading: 'Loading…', unknownValidity: 'Not recorded', until: 'to',
    editTitle: 'Edit access', editHelp: 'Label and person are independent. This does not change enrollment on the lock.',
    pinInput: 'PIN', existingPin: 'Existing PIN', pinHelp: 'HA remembers this code locally for pause and resume.',
    existingHelp: 'Enter the same existing code once. A different code would replace the code on the lock.',
    remembered: 'PIN stored', missingPin: 'PIN not stored yet', start: 'Valid from', end: 'Valid until',
    enrollHelp: 'Wake the lock. For a card, hold it against the reader; for a fingerprint, touch the sensor when prompted by the lock.',
    saved: 'Saved.', done: 'The lock confirmed the change.', admin: 'This manager is only available to administrators.',
    policy: 'Original validity is missing; this older enrollment cannot be paused yet.',
    unsupported: 'Pause is not available for this access type on this lock.', invalid: 'Check the entered values.',
    stale: 'This enrollment is no longer current.', expiredHelp: 'The original validity has expired.',
    uncertain: 'The last change is unconfirmed. Choose Pause or Resume again.',
    failed: 'The operation was not confirmed. Check the status and try again.', enroll: 'Enroll', delete: 'Delete', deleteHelp: 'Permanently remove this enrollment from the lock. Using it again requires re-enrollment.',
  },
};
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const FINGERS = ['right','left'].flatMap(side=>['thumb','index','middle','ring','pinky'].map(finger=>`${side}_${finger}`));

class TuyaAccessPanel extends HTMLElement {
  constructor() {
    super(); this.attachShadow({mode:'open'}); this._items=[]; this._locks=[]; this._loading=false; this._busy=false;
  }
  set hass(value) {
    this._hass=value;
    const lang=value.language?.startsWith('nl') ? 'nl' : 'en';
    if (!this._started) { this._started=true; this._lang=lang; this.refresh(); }
    else if (lang!==this._lang) { this._lang=lang; if (!this._dialog) this.render(); }
  }
  get t() { return WORDS[this._lang || 'en']; }
  async call(service, data, response=true) {
    const result=await this._hass.callWS({type:'call_service', domain:'tuya_ble_access', service, service_data:data, return_response:response});
    return result?.response;
  }
  async refresh() {
    if (!this._hass.user?.is_admin) { this.render(); return; }
    this._loading=true; this.render();
    try {
      const data=await this.call('list_access',this._lock ? {device_id:this._lock} : {});
      this._items=data.items; this._locks=data.locks; this._lock=data.device_id;
    } catch(error) { this._error=error.message || this.t.failed; }
    finally { this._loading=false; this.render(); }
  }
  peopleOptions(selected) {
    const people=Object.values(this._hass.states).filter(s=>s.entity_id.startsWith('person.'));
    let html=`<option value="">${esc(this.t.none)}</option>`;
    if (selected && !people.some(s=>s.entity_id===selected)) html+=`<option selected value="${esc(selected)}">${esc(selected)}</option>`;
    return html+people.map(s=>`<option value="${esc(s.entity_id)}" ${s.entity_id===selected?'selected':''}>${esc(s.attributes.friendly_name || s.entity_id)}</option>`).join('');
  }
  personName(id) { return id ? this._hass.states[id]?.attributes.friendly_name || id : '—'; }
  date(ts) { return ts == null ? this.t.unknownValidity : new Date(ts*1000).toLocaleString(this._lang,{dateStyle:'medium',timeStyle:'short'}); }
  reason(row) {
    if (!row.pause_reason) return row.status==='unknown' ? this.t.uncertain : '';
    if (row.pause_reason.includes('expired')) return this.t.expiredHelp;
    if (row.pause_reason.includes('policy')) return this.t.policy;
    if (row.pause_reason.includes('not_current')) return this.t.stale;
    return this.t.unsupported;
  }
  render() {
    const t=this.t, disabled=this._loading||this._busy;
    this.shadowRoot.innerHTML=`<style>
      :host{display:block;height:100%;overflow:auto;background:var(--primary-background-color,#f4f5f7);color:var(--primary-text-color,#20252b);font:14px var(--paper-font-body1_-_font-family,system-ui)}
      *{box-sizing:border-box}header{display:flex;align-items:center;gap:16px;height:64px;padding:0 24px;background:var(--app-header-background-color,#fff);border-bottom:1px solid var(--divider-color,#ddd)}
      header b{font-size:18px}main{max-width:1250px;margin:auto;padding:32px 24px}h1{font-size:30px;margin:0 0 8px}p{line-height:1.6;color:var(--secondary-text-color,#68717c)}
      .toolbar{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:26px 0 18px}.toolbar label{min-width:230px;margin-right:auto}button,input,select{font:inherit;border-radius:8px;border:1px solid var(--divider-color,#cbd0d8);padding:10px 14px;color:inherit;background:var(--card-background-color,#fff)}
      button{cursor:pointer;font-weight:600;min-height:42px}button:hover:not(:disabled){background:var(--secondary-background-color,#eef1f5)}button:disabled{opacity:.45;cursor:default}.primary{background:var(--primary-color,#087fa4);color:var(--text-primary-color,#fff);border-color:transparent}
      label{display:grid;gap:7px;font-weight:500}input,select{width:100%;min-height:44px}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid var(--primary-color,#087fa4);outline-offset:2px}
      .table{overflow:auto;background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#ddd);border-radius:12px}table{width:100%;border-collapse:collapse;text-align:left}th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--secondary-text-color,#68717c)}td,th{padding:17px 18px;border-bottom:1px solid var(--divider-color,#e3e6eb);vertical-align:top}tr:last-child td{border:0}td:first-child{min-width:160px;font-weight:600}td small{display:block;font-weight:400;color:var(--secondary-text-color,#68717c);margin-top:5px}td.actions{min-width:240px}.actions button{margin:0 6px 6px 0;padding:7px 10px;min-height:36px}.badge{display:inline-block;padding:5px 9px;border-radius:20px;background:var(--secondary-background-color,#eef1f5);white-space:nowrap}.active{color:#17663d;background:#e0f3e7}.paused,.unknown{color:#80520c;background:#fff0d6}.notice{padding:14px 18px;border-radius:8px;margin:14px 0;background:var(--secondary-background-color,#e7edf5)}.error{border-left:4px solid #cb4141}.empty{text-align:center;padding:50px 20px}.muted{font-size:12px;max-width:280px;line-height:1.5;color:var(--secondary-text-color,#68717c)}
      dialog{color:inherit;background:var(--card-background-color,#fff);border:1px solid var(--divider-color,#ddd);border-radius:14px;width:min(500px,calc(100vw - 32px));max-height:90vh;padding:24px;box-shadow:0 20px 80px #0004}dialog::backdrop{background:#0007}dialog h2{margin:0 0 12px}form{display:grid;gap:18px}.form-actions{display:flex;justify-content:end;gap:10px;margin-top:8px}.secret-note{margin:0;font-size:13px}
      @media(max-width:700px){main{padding:22px 12px}header{padding:0 12px}.toolbar label{width:100%}td,th{padding:12px}h1{font-size:25px}table{min-width:0}thead{display:none}tbody{display:block}tr{display:grid;grid-template-columns:1fr 1fr;padding:10px;border-bottom:1px solid var(--divider-color,#ddd)}tr:last-child{border:0}td{display:block;border:0;min-width:0!important}td:first-child,td.actions{grid-column:1 / -1}td:first-child{font-size:17px}td[data-title]::before{content:attr(data-title);display:block;font-size:11px;color:var(--secondary-text-color,#68717c);margin-bottom:6px;text-transform:uppercase}td.actions{padding-top:14px}}
    </style><header><button id="menu" aria-label="Menu">☰</button><b>Tuya BLE Access</b></header><main><h1>${esc(t.title)}</h1><p>${esc(t.subtitle)}</p>
    ${!this._hass?.user?.is_admin ? `<p>${esc(t.admin)}</p>` : `
      <div class="toolbar"><label>${esc(t.lock)}<select id="lock" ${disabled?'disabled':''}>${this._locks.map(l=>`<option value="${esc(l.id)}" ${l.id===this._lock?'selected':''}>${esc(l.name)}</option>`).join('')}</select></label><button id="refresh" ${disabled?'disabled':''}>${esc(t.refresh)}</button><button id="add" class="primary" ${disabled||!this._lock?'disabled':''}>＋ ${esc(t.add)}</button></div>
      <div role="status" aria-live="polite">${this._busy?`<div class="notice">${esc(this._busyService==='add_fingerprint'?t.fingerBusy:t.busy)}</div>`:this._loading?`<div class="notice">${esc(t.loading)}</div>`:this._message?`<div class="notice">${esc(this._message)}</div>`:''}</div>
      ${this._error?`<div role="alert" class="notice error">${esc(this._error)}</div>`:''}
      <div class="table">${this._items.length?`<table><thead><tr>${['label','person','type','status','validity','actions'].map(k=>`<th scope="col">${esc(t[k])}</th>`).join('')}</tr></thead><tbody>${this._items.map((r,i)=>`<tr><td>${esc(r.name)}</td><td data-title="${esc(t.person)}">${esc(this.personName(r.person))}</td><td data-title="${esc(t.type)}">${esc(t[r.kind])}${r.kind==='fingerprint'&&r.finger?`<small>${esc(t[r.finger]||r.finger)}</small>`:''}${r.kind==='pin'?`<small>${esc(r.needs_pin?t.missingPin:t.remembered)}</small>`:''}</td><td data-title="${esc(t.status)}"><span class="badge ${esc(r.status)}">${esc(t[r.status]||r.status)}</span></td><td data-title="${esc(t.validity)}">${r.effective_ts==null?esc(t.unknownValidity):`${esc(this.date(r.effective_ts))}<small>${esc(t.until)} ${esc(this.date(r.expiry_ts))}</small>`}</td><td class="actions"><button data-edit="${i}" ${disabled?'disabled':''}>${esc(t.edit)}</button>${r.can_pause?`<button data-pause="${i}" ${disabled?'disabled':''}>${esc(t.pause)}</button>`:''}${r.can_resume?`<button data-resume="${i}" ${disabled?'disabled':''}>${esc(t.resume)}</button>`:''}${r.can_delete?`<button data-delete="${i}" ${disabled?'disabled':''}>${esc(t.delete)}</button>`:''}<div class="muted">${esc(this.reason(r))}</div></td></tr>`).join('')}</tbody></table>`:`<div class="empty">${esc(this._locks.length?t.empty:t.noLocks)}</div>`}</div>`}</main>`;
    this.shadowRoot.querySelector('#menu').onclick=()=>this.dispatchEvent(new CustomEvent('hass-toggle-menu',{bubbles:true,composed:true}));
    if (!this._hass?.user?.is_admin) return;
    this.shadowRoot.querySelector('#refresh').onclick=()=>{this._error='';this.refresh();};
    this.shadowRoot.querySelector('#lock').onchange=e=>{this._lock=e.target.value;this._items=[];this._error='';this._message='';this.refresh();};
    this.shadowRoot.querySelector('#add').onclick=()=>this.openDialog('add');
    for (const action of ['edit','pause','resume','delete']) this.shadowRoot.querySelectorAll(`[data-${action}]`).forEach(b=>b.onclick=()=>{
      const row=this._items[Number(b.dataset[action])];
      if (action==='edit'||action==='delete'||row.needs_pin) this.openDialog(action,row); else this.changePause(action,row);
    });
  }
  openDialog(action,row) {
    const t=this.t; this._dialog={action,row};
    const d=document.createElement('dialog');
    const metadata=action==='edit'||action==='add';
    d.innerHTML=`<h2>${esc(action==='add'?t.add:action==='edit'?t.editTitle:t[action])}</h2><p>${esc(action==='add'?t.enrollHelp:action==='edit'?t.editHelp:action==='delete'?t.deleteHelp:t.existingHelp)}</p><form>
      ${action==='add'?`<label>${esc(t.type)}<select name="kind">${['pin','temporary_pin','card','fingerprint'].map(k=>`<option value="${k}">${esc(t[k])}</option>`).join('')}</select></label>`:''}
      ${action==='add'?`<div id="finger-fields" hidden><label>${esc(t.finger)}<select name="finger" disabled><option value="">${esc(t.chooseFinger)}</option>${FINGERS.map(f=>`<option value="${f}">${esc(t[f])}</option>`).join('')}</select></label></div>`:''}
      ${metadata?`<label>${esc(t.label)}<input name="name" maxlength="100" value="${esc(row?.name||'')}" ${action==='edit'?'required':''}></label><label>${esc(t.person)}<select name="person">${this.peopleOptions(row?.person)}</select></label>`:''}
      ${['add','pause','resume'].includes(action)?`<div id="pin-fields"><label>${esc(action==='add'?t.pinInput:t.existingPin)}<input name="pin_code" type="password" inputmode="numeric" autocomplete="new-password" pattern="[0-9]{6,10}" minlength="6" maxlength="10" required></label><p class="secret-note">${esc(t.pinHelp)}</p></div>`:''}
      ${action==='add'?`<div id="dates" hidden><label>${esc(t.start)}<input name="effective_time" type="datetime-local"></label><label>${esc(t.end)}<input name="expiry_time" type="datetime-local"></label></div>`:''}
      <div class="form-actions"><button type="button" id="cancel">${esc(t.cancel)}</button><button class="primary" type="submit">${esc(action==='add'?t.enroll:action==='edit'?t.save:t[action])}</button></div></form>`;
    this.shadowRoot.append(d); d.showModal();
    const close=()=>{d.querySelector('form').reset();d.remove();this._dialog=null;};
    d.oncancel=close; d.querySelector('#cancel').onclick=close;
    if(action==='add') d.querySelector('[name=kind]').onchange=e=>{
      const pin=['pin','temporary_pin'].includes(e.target.value), temp=e.target.value==='temporary_pin';
      d.querySelector('#pin-fields').hidden=!pin; d.querySelector('[name=pin_code]').required=pin;
      const fingerprint=e.target.value==='fingerprint';
      d.querySelector('#finger-fields').hidden=!fingerprint;
      d.querySelector('[name=finger]').disabled=!fingerprint;
      d.querySelector('[name=finger]').required=fingerprint;
      d.querySelector('#dates').hidden=!temp;
      for(const field of ['effective_time','expiry_time']) d.querySelector(`[name=${field}]`).required=temp;
    };
    d.querySelector('form').onsubmit=e=>{
      e.preventDefault(); const values=Object.fromEntries(new FormData(e.target)); close();
      if(action==='edit') this.run('update_access',{device_id:this._lock,access_id:row.id,name:values.name,person:values.person||null},true,t.saved);
      else if(action==='delete') this.run('delete_credential',{device_id:this._lock,credential_id:row.id},false,t.done);
      else if(action==='add') {
        const kind=values.kind, data={device_id:this._lock};
        data.name=values.name.trim() || (values.person?this.personName(values.person)+' ':'')+(kind==='fingerprint'?t[values.finger]:t[kind]);
        if(values.person) data.person=values.person;
        if(kind==='fingerprint') data.finger=values.finger;
        if(['pin','temporary_pin'].includes(kind)) data.pin_code=values.pin_code;
        if(kind==='temporary_pin') {
          data.effective_time=new Date(values.effective_time).toISOString();data.expiry_time=new Date(values.expiry_time).toISOString();
          this.run('create_temp_password',data,true,t.done);
        } else this.run({pin:'add_pin',card:'add_card',fingerprint:'add_fingerprint'}[kind],data,false,t.done);
      } else this.changePause(action,row,values.pin_code);
    };
  }
  changePause(action,row,pin) {
    const temp=row.kind==='temporary_pin';
    const data={device_id:this._lock,[temp?'password_id':'credential_id']:row.id};
    if(pin) data.pin_code=pin;
    return this.run(`${action}_${temp?'temp_password':'credential'}`,data,true,this.t.done);
  }
  async run(service,data,response,message) {
    if(this._busy) return;
    this._busy=true; this._busyService=service; this._error='';this._message='';this.render();
    try { await this.call(service,data,response);this._message=message; }
    catch(error) { this._error=error.message||this.t.failed; }
    finally { delete data.pin_code;this._busy=false;await this.refresh(); }
  }
}
if (!customElements.get('tuya-access-panel')) customElements.define('tuya-access-panel',TuyaAccessPanel);
