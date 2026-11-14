// AI-Hub UI: реестр агентов, запуск заданий, история, детали, отмена.
// Без сборки и зависимостей — обычные fetch + DOM.

const API = window.location.pathname.startsWith('/ui') ? '' : '';
const TOKEN_KEY = 'aihub_token';
const BUILD = 'ui25-core03';
function storeGet(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } }
function storeSet(k, v) { try { localStorage.setItem(k, v); return true; } catch (e) { return false; } }
function storeDel(k) { try { localStorage.removeItem(k); } catch (e) { /* заблокировано */ } }
let token = storeGet(TOKEN_KEY);
let currentJob = null;
let currentStatus = '';
let agents = [];
let agentsLoading = false;
let agentNames = new Map();
let runProfileGen = 0;
let pollTimers = [];
let detailGen = 0;
let pendingRun = null;
let jobsLoaded = 0;
let jobsTotal = 0;
// Поколение запроса списка: поздний ответ опроса не затирает результат «показать ещё»
// и не применяется после смены фильтра.
let listGen = 0;
let listBusy = false;
const seenJobs = new Set();

// Русские подписи: значения из API остаются английскими, переводим только показ.
const STATUS_RU = {
  queued: 'в очереди', running: 'в работе', succeeded: 'успешно', failed: 'ошибка',
  timeout: 'таймаут', unknown: 'неизвестно', cancelled: 'отменено',
  cancel_requested: 'отмена запрошена', already_finished: 'уже завершено',
};
const EVENT_RU = {
  queued: 'создано', created: 'создано', leased: 'взято воркером',
  succeeded: 'успешно', failed: 'ошибка', timeout: 'таймаут',
  cancelled: 'отменено', cancel_requested: 'запрошена отмена',
  lease_expired: 'аренда истекла', finished: 'завершено',
};
const AGENT_STATUS_RU = { verified: 'проверен', unverified: 'не проверен', error: 'ошибка' };
// DIAG-01: статус агента — набор наблюдений, а не слово в реестре.
const OBS_KINDS = ['configured', 'compatibility', 'availability', 'quality'];
const OBS_KIND_RU = { configured: 'конфигурация', compatibility: 'совместимость', availability: 'доступность', quality: 'качество' };
const OBS_STATE_RU = { ok: 'ok', degraded: 'ухудшено', failed: 'сбой', stale: 'устарело', unknown: 'нет данных' };
const OBS_EFFECTIVE_RU = { ok: 'ok', degraded: 'деградация', failed: 'сбой', unknown: 'нет свежих данных' };
const ruStatus = s => STATUS_RU[s] || s || '—';
const ruEvent = e => EVENT_RU[e] || e;

const newKey = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID() : 'ui-' + Date.now() + '-' + Math.random().toString(16).slice(2));
const $ = sel => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fetchOnce(path, opts, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers.Authorization = 'Bearer ' + token;
  return fetch(API + path, { ...opts, headers, signal: ctrl.signal, cache: 'no-store' })
    .finally(() => clearTimeout(timer));
}

async function api(path, opts = {}) {
  const timeout = opts.timeout || 10000;
  let resp;
  let lastErr;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      resp = await fetchOnce(path, opts, timeout);
      break;
    } catch (e) {
      lastErr = e;
      // обрыв или таймаут — повторяем один раз: прокси на хабе иногда вешает соединение
      if (attempt === 0) await new Promise(r => setTimeout(r, 600));
    }
  }
  if (!resp) {
    const reason = lastErr && lastErr.name === 'AbortError'
      ? `хаб не ответил за ${Math.round(timeout / 1000)} с`
      : (lastErr ? lastErr.message : 'сеть недоступна');
    throw new Error('сеть: ' + reason);
  }
  if (resp.status === 401) { stopPolling(); openGate('Токен не подошёл — введи заново.'); throw new Error('unauthorized'); }
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!resp.ok) {
    const detail = data && (data.detail || data.error) ? (data.detail || data.error) : resp.status;
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    error.detail = detail;
    throw error;
  }
  return data;
}

// CORE-03: the original job outcome remains separate from any human historical decision.
const Recon={decisionState:'unresolved',jobStatus:'all',cursor:null,items:[],generation:0,busy:false,reloadWanted:false,selected:null,detailGeneration:0,decisionBusy:false,notice:''};
const RECON_ACTION={unresolved:'не принято',accepted_historical:'принят как исторический артефакт',rejected:'отклонён',accept_historical:'Принять как исторический артефакт',reject:'Отклонить'};
const RECON_JOB_STATUS={unknown:'неизвестно',cancel_requested:'отмена запрошена',succeeded:'успешно',failed:'ошибка',running:'в работе',queued:'в очереди',cancelled:'отменено',timeout:'таймаут'};
function reconDate(value){if(!value)return '—';const date=new Date(value);return Number.isNaN(date.getTime())?String(value):date.toLocaleString()}
function reconValue(value){if(value===null||value===undefined||value==='')return '—';return typeof value==='object'?JSON.stringify(value):String(value)}
function reconIsLate(item){return String(item.case_id||'').startsWith('late:')||/late/i.test(String(item.kind||''))||!!item.late_result_id}
function reconField(parent,label,value,warning=false){const row=el('div','recon-field'+(warning?' recon-warning':''));row.append(el('span','recon-label',label),el('span','recon-value',reconValue(value)));parent.append(row);return row}
function reconRenderList(append=false){const box=$('#recon-list');if(!append)box.replaceChildren();for(const item of Recon.items){const caseId=String(item.case_id||'');const button=el('button','recon-case'+(Recon.selected?.case_id===caseId?' active':''));button.type='button';button.dataset.caseId=caseId;button.append(el('strong','recon-case-title',(reconIsLate(item)?'Поздний результат':'Неизвестное исполнение')+' · '+(item.job_id||caseId)));const states=el('span','recon-case-states');states.append(el('span','badge '+(item.job_status||'unknown'),'Задание: '+(RECON_JOB_STATUS[item.job_status]||item.job_status||'—')),el('span','badge recon-decision-'+(item.decision_state||'unresolved'),'Сверка: '+(RECON_ACTION[item.decision_state]||item.decision_state||'открыта')));button.append(states,el('span','recon-case-meta',(item.operation?.operation||item.operation?.kind||item.operation||'операция')+' · '+reconDate(item.received_at||item.created_at)));button.onclick=()=>reconOpenCase(caseId);box.append(button)}$('#recon-more').hidden=!Recon.cursor}
function reconRenderDetail(item){const box=$('#recon-detail');box.replaceChildren();box.hidden=false;if(Recon.notice){const notice=el('p','msg ok recon-notice',Recon.notice);notice.setAttribute('role','status');box.append(notice)}const head=el('div','recon-detail-head');head.append(el('h3','',(reconIsLate(item)?'Поздний артефакт':'Сверка задания')+' · '+(item.job_id||item.case_id)));const close=el('button','ghost small','Закрыть');close.type='button';close.onclick=()=>{box.hidden=true;Recon.selected=null;reconRenderList()};head.append(close);box.append(head);const states=el('div','recon-state-grid');reconField(states,'Исходный статус задания',RECON_JOB_STATUS[item.job_status]||item.job_status);reconField(states,'Состояние исполнения',item.execution_state);reconField(states,'Решение сверки',RECON_ACTION[item.decision_state]||item.decision_state||'не принято');reconField(states,'Статус попытки',item.attempt_status);reconField(states,'Попытка',item.attempt_no);reconField(states,'Ревизия',item.revision);box.append(states);const evidence=item.evidence||{};const facts=el('section','recon-evidence');facts.append(el('h4','','Связь и доказательства'));reconField(facts,'Задание',evidence.job_id||item.job_id);reconField(facts,'Попытка',evidence.attempt_id||item.attempt_id);reconField(facts,'Операция',evidence.operation||item.operation?.operation||item.operation?.kind);reconField(facts,'Ключ операции',evidence.operation_key||item.operation?.operation_key);reconField(facts,'Кандидат',evidence.candidate_digest||item.candidate_digest);reconField(facts,'Дерево',evidence.tree_digest);reconField(facts,'Preimage',evidence.preimage_digest);reconField(facts,'Происхождение',evidence.provenance);if(item.artifact_summary)reconField(facts,'Краткое описание артефакта',item.artifact_summary);if(item.disabled_reason)reconField(facts,'Причина недоступности принятия',item.disabled_reason,true);box.append(facts);const actions=Array.isArray(item.allowed_actions)?item.allowed_actions:[];if(actions.includes('accept_historical')||actions.includes('reject')){const form=el('form','recon-decision');form.append(el('h4','','Решение оператора'));form.append(el('p','hint','Принятие отмечает только исторический артефакт. Оно не меняет статус задания, попытки или текущего кандидата.'));const label=el('label','recon-reason-label','Обязательное основание');const reason=el('textarea','');reason.required=true;reason.minLength=10;reason.maxLength=1000;reason.rows=3;reason.placeholder='Опишите, что именно проверено';reason.setAttribute('aria-label','Обязательное основание решения');label.append(reason);form.append(label);const buttons=el('div','row');for(const action of ['accept_historical','reject'])if(actions.includes(action)){const button=el('button',action==='accept_historical'?'primary':'ghost',action==='accept_historical'?'Принять как исторический артефакт':'Отклонить');button.type='submit';button.dataset.action=action;buttons.append(button)}form.append(buttons);const message=el('p','msg recon-decision-msg');message.setAttribute('role','status');form.append(message);form.onsubmit=event=>{event.preventDefault();if(!event.submitter||Recon.decisionBusy)return;reconDecide(item,event.submitter.dataset.action,reason.value,form)};box.append(form)}else{reconField(box,'Действия','Решение недоступно по имеющимся доказательствам.');if(item.disabled_reason)reconField(box,'Причина',item.disabled_reason,true)}const history=el('section','recon-audit');history.append(el('h4','','История решений'));const decisions=item.audit||[];if(!decisions.length)history.append(el('p','hint','Решений пока нет.'));for(const decision of decisions){const entry=el('article','recon-audit-entry');entry.append(el('strong','',RECON_ACTION[decision.action]||decision.action),el('span','recon-audit-meta',reconDate(decision.created_at)+' · '+reconValue(decision.actor)+' · ревизия '+reconValue(decision.revision)));entry.append(el('p','',reconValue(decision.reason)));history.append(entry)}box.append(history)}
async function reconLoad(append=false){if(Recon.busy){if(!append){Recon.generation++;Recon.reloadWanted=true}return}Recon.busy=true;const generation=++Recon.generation;const status=$('#recon-status');if(!append){Recon.cursor=null;Recon.items=[];status.textContent='Загружаю исключения…'}try{const params=new URLSearchParams({decision_state:Recon.decisionState,job_status:Recon.jobStatus,limit:'50'});if(append&&Recon.cursor)params.set('cursor',Recon.cursor);const data=await api('/workspace/reconciliation/cases?'+params.toString());if(generation!==Recon.generation)return;const page=data.items||[];if(append){const known=new Set(Recon.items.map(item=>item.case_id));for(const item of page)if(!known.has(item.case_id))Recon.items.push(item)}else Recon.items=page;Recon.cursor=data.next_cursor||null;status.textContent=Recon.items.length?'Показано '+Recon.items.length+' исключений':'Исключений нет';reconRenderList(!append);if(Recon.selected)reconOpenCase(Recon.selected.case_id,true)}catch(error){if(generation===Recon.generation){status.textContent='Не удалось загрузить исключения: '+error.message;status.className='quality-error recon-error';status.setAttribute('role','alert');$('#recon-refresh').textContent='Повторить';$('#recon-more').hidden=true}}finally{Recon.busy=false;if(Recon.reloadWanted){Recon.reloadWanted=false;reconLoad(false)}}}
async function reconOpenCase(caseId,preserveNotice=false){if(!preserveNotice)Recon.notice='';const generation=++Recon.detailGeneration;try{const item=await api('/workspace/reconciliation/cases/'+encodeURIComponent(caseId));if(generation!==Recon.detailGeneration)return;Recon.selected=item;reconRenderList();reconRenderDetail(item)}catch(error){if(generation===Recon.detailGeneration&&error.message!=='unauthorized')$('#recon-status').textContent='Не удалось открыть сверку: '+error.message}}
async function reconDecide(item,action,reason,form){if(Recon.decisionBusy)return;const clean=String(reason||'').trim();if(clean.length<10||clean.length>1000){form.querySelector('.recon-decision-msg').textContent='Укажите проверяемое основание длиной от 10 до 1000 символов.';return}Recon.decisionBusy=true;form.querySelectorAll('button').forEach(button=>button.disabled=true);const message=form.querySelector('.recon-decision-msg');message.textContent='Сохраняю решение…';try{await api('/workspace/reconciliation/cases/'+encodeURIComponent(item.case_id)+'/decision',{method:'POST',body:JSON.stringify({action,expected_revision:item.revision,reason:clean})});Recon.notice='Решение сохранено в истории. Исходный статус задания не изменён.';await reconOpenCase(item.case_id,true);await reconLoad(false)}catch(error){if(/409|revision|conflict|stale/i.test(error.message)){Recon.notice='Ревизия изменилась. Показана актуальная карточка и история; проверьте их перед новым решением.';await reconOpenCase(item.case_id,true)}else {Recon.notice='';message.textContent='Решение не подтверждено: '+error.message}}finally{Recon.decisionBusy=false;form.querySelectorAll('button').forEach(button=>button.disabled=false)}}
function wireReconciliation(){const decision=$('#recon-decision-state'),job=$('#recon-job-status'),refresh=$('#recon-refresh'),more=$('#recon-more');if(decision)decision.onchange=()=>{Recon.decisionState=decision.value;Recon.selected=null;Recon.notice='';$('#recon-detail').hidden=true;reconLoad(false)};if(job)job.onchange=()=>{Recon.jobStatus=job.value;Recon.selected=null;Recon.notice='';$('#recon-detail').hidden=true;reconLoad(false)};if(refresh)refresh.onclick=()=>reconLoad(false);if(more)more.onclick=()=>reconLoad(true)}

// ---------- токен ----------
function node(id) { return document.getElementById(id); }

function openGate(msg) {
  const gate = node('gate');
  if (!gate) return;
  gate.hidden = false;
  const err = node('token-err');
  if (err) err.textContent = msg || '';
  const input = node('token-input');
  if (input) { input.value = ''; input.focus(); }
}
function closeGate() { const g = node('gate'); if (g) g.hidden = true; }

function setStatus(text, cls) {
  const st = node('token-status');
  if (st) { st.className = 'msg ' + (cls || ''); st.textContent = text || ''; }
}
function setErr(text) {
  const err = node('token-err');
  if (err) err.textContent = text || '';
}

async function saveToken() {
  const input = node('token-input');
  const btn = node('token-save');
  const val = input ? input.value.trim() : '';
  setErr('');
  if (!val) { setErr('Поле пустое.'); return; }
  token = val;
  if (btn) btn.disabled = true;
  setStatus('Проверяю токен…', '');
  try {
    await api('/jobs?limit=1', { timeout: 8000 });   // единственный защищённый запрос на этом шаге
    token = '';                                      // временно, чтобы boot() шёл с проверенным токеном
    token = val;
    storeSet(TOKEN_KEY, val);
    setStatus('Ок, загружаю данные…', 'ok');
    closeGate();
    await boot();
    setStatus('', '');
  } catch (e) {
    const msg = String(e && e.message || e);
    if (msg === 'unauthorized' || /401/.test(msg)) {
      setErr('Неверный токен. Скопируй HUB_TOKEN целиком, без кавычек и пробелов.');
      storeDel(TOKEN_KEY);
      token = '';
    } else {
      setErr('Ошибка: ' + msg);
    }
    setStatus('', '');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Диагностика: одна кнопка, чтобы не гадать, где обрыв.
async function diagnose() {
  const out = node('diag');
  if (!out) return;
  out.hidden = false;
  const lines = [];
  const add = (k, v) => lines.push(k + ': ' + v);
  out.textContent = 'Собираю…';
  add('сборка UI', BUILD);
  add('страница', location.href);
  add('браузер', navigator.userAgent);
  try { localStorage.setItem('_t', '1'); localStorage.removeItem('_t'); add('localStorage', 'доступен'); }
  catch (e) { add('localStorage', 'ЗАБЛОКИРОВАН — ' + e.message); }
  add('токен в поле', (node('token-input') && node('token-input').value.trim() ? 'есть, ' + node('token-input').value.trim().length + ' симв.' : 'пусто'));
  add('токен в браузере', token ? 'есть, ' + token.length + ' симв.' : 'нет');

  const probe = async (path, withToken) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    const headers = {};
    if (withToken && (node('token-input') || {}).value) headers.Authorization = 'Bearer ' + node('token-input').value.trim();
    const t0 = performance.now();
    try {
      const r = await fetch(path, { headers, cache: 'no-store', signal: ctrl.signal });
      const body = (await r.text()).slice(0, 120).replace(/\s+/g, ' ');
      add((withToken ? 'с токеном ' : '') + path, r.status + ' за ' + Math.round(performance.now() - t0) + ' мс | ' + body);
    } catch (e) {
      add((withToken ? 'с токеном ' : '') + path, 'ОШИБКА ' + e.name + ': ' + e.message);
    } finally { clearTimeout(timer); }
  };
  await probe('/health', false);
  await probe('/agents', false);
  await probe('/jobs?limit=1', true);

  add('JS-ошибки', window.__jsErrors && window.__jsErrors.length ? window.__jsErrors.join(' | ') : 'нет');
  out.textContent = lines.join('\n');
}

// Глобальный перехват ошибок: без этого «ничего не происходит» неотличимо от ошибки скрипта.
window.__jsErrors = [];
window.addEventListener('error', ev => {
  const msg = (ev.message || 'js error') + (ev.filename ? ' @' + ev.filename.split('/').pop() + ':' + ev.lineno : '');
  window.__jsErrors.push(msg);
  if (node('gate') && !node('gate').hidden) setErr('Ошибка страницы: ' + msg);
});
window.addEventListener('unhandledrejection', ev => {
  window.__jsErrors.push('promise: ' + (ev.reason && ev.reason.message || ev.reason));
});

function wireGate() {
  const btnSave = node('token-save');
  const btnCancel = node('token-cancel');
  const btnDiag = node('token-diag');
  const input = node('token-input');
  const btnToken = node('token-btn');
  if (btnSave) btnSave.onclick = () => { saveToken().catch(e => setErr('Сбой: ' + e.message)); };
  if (btnCancel) btnCancel.onclick = () => { if (token) closeGate(); };
  if (btnDiag) btnDiag.onclick = () => { diagnose().catch(e => setErr('Диагностика: ' + e.message)); };
  if (btnToken) btnToken.onclick = () => openGate('');
  if (input) input.addEventListener('keydown', e => {
    // Enter во время набора CJK/IME — это подтверждение ввода, а не отправка формы.
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter') { e.preventDefault(); saveToken(); }
  });
}

// ---------- здоровье ----------
async function health() {
  try {
    const h = await api('/health');
    $('#health-dot').className = 'dot ' + (h.db === true ? 'ok' : 'bad');
    $('#health-text').textContent = h.db === true ? 'хаб и БД в норме' : 'БД: ' + h.db;
    $('#version').textContent = 'v' + h.version;
    // Auth gate closes only after a protected request succeeds.
  } catch (e) {
    $('#health-dot').className = 'dot bad';
    $('#health-text').textContent = 'хаб недоступен';
  }
}

// ---------- агенты ----------
function obsAge(iso, ageSeconds) {
  const serverAge = Number(ageSeconds);
  if (Number.isFinite(serverAge) && ageSeconds !== null && ageSeconds !== undefined) {
    const s = Math.max(0, Math.round(serverAge));
    if (s < 60) return s + ' с назад';
    const m = Math.floor(s / 60);
    if (m < 60) return m + ' мин назад';
    const h = Math.floor(m / 60);
    if (h < 48) return h + ' ч назад';
    return Math.floor(h / 24) + ' дн назад';
  }
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return '';
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return s + ' с назад';
  const m = Math.floor(s / 60);
  if (m < 60) return m + ' мин назад';
  const h = Math.floor(m / 60);
  if (h < 48) return h + ' ч назад';
  return Math.floor(h / 24) + ' дн назад';
}

function obsTtl(o) {
  if (!o || o.ttl_s === null || o.ttl_s === undefined) return 'не истекает';
  const s = o.ttl_s;
  if (s < 3600) return 'TTL ' + Math.round(s / 60) + ' мин';
  if (s < 86400) return 'TTL ' + Math.round(s / 3600) + ' ч';
  return 'TTL ' + Math.round(s / 86400) + ' дн';
}

const OBS_DETAIL_RU = {
  worker_id: 'исполнитель',
  host: 'хост',
  os_user: 'пользователь ОС',
  pid: 'PID',
  protocol: 'протокол',
  adapter_version: 'адаптер',
  cli_path: 'CLI',
  cli_version: 'версия CLI',
  cli_size: 'размер CLI',
  cli_mtime: 'CLI изменён',
  endpoint: 'адрес',
};

function obsDetailLine(detail) {
  const parts = [];
  for (const [k, v] of Object.entries(detail || {})) {
    const label = OBS_DETAIL_RU[k] || k;
    if (Array.isArray(v)) { if (v.length) parts.push(label + ': ' + v.join('/')); continue; }
    if (v === null || v === undefined || v === '') continue;
    parts.push(label + ': ' + v);
  }
  return parts.join(' · ');
}

function obsPanel(a) {
  const panel = el('div', 'obs-panel');
  const head = el('div', 'obs-panel-head');
  head.append(el('b', '', OBS_EFFECTIVE_RU[a.effective_state] || a.effective_state || 'unknown'));
  if (a.effective_reason) head.append(el('span', 'obs-reason', a.effective_reason));
  panel.append(head);
  for (const kind of OBS_KINDS) {
    const o = (a.observations || {})[kind];
    const state = o ? o.state : 'unknown';
    const line = el('div', 'obs-row');
    line.append(el('span', 'obs-kind', OBS_KIND_RU[kind]));
    line.append(el('span', 'obs-state ' + state, OBS_STATE_RU[state] || state));
    const meta = [];
    meta.push(o && o.checked_at ? obsAge(o.checked_at, o.age_s) : 'наблюдений нет');
    if (o && o.checked_at) meta.push(obsTtl(o));
    if (o && o.source) meta.push('источник: ' + o.source);
    line.append(el('span', 'obs-meta', meta.join(' · ')));
    panel.append(line);
    if (o && (o.reason || (o.detail && Object.keys(o.detail).length))) {
      const extra = el('div', 'obs-extra');
      if (o.reason) extra.append(el('span', 'obs-reason', o.reason));
      const text = obsDetailLine(o.detail);
      if (text) extra.append(el('span', 'obs-detail', text));
      panel.append(extra);
    }
  }
  return panel;
}

function obsBadge(a, row) {
  const state = a.effective_state || 'unknown';
  const badge = el('button', 'status obs-' + state, OBS_EFFECTIVE_RU[state] || state);
  badge.type = 'button';
  badge.title = 'Наблюдения: ' + (a.effective_reason
    || (state === 'ok' ? 'все виды свежие' : 'свежих данных нет'));
  badge.onclick = ev => {
    ev.stopPropagation();
    const wasOpen = !!row.querySelector('.obs-panel');
    row.querySelectorAll('.obs-panel').forEach(n => n.remove());
    if (!wasOpen) row.append(obsPanel(a));
  };
  return badge;
}

async function loadAgents() {
  if (agentsLoading) return;
  agentsLoading = true;
  const list = $('#agents-list');
  const select = $('#run-agent');
  const selectedAgent = select.value;
  const activeAgent = list.querySelector('.agent.active')?.dataset.id || '';
  const expanded = new Set(Array.from(list.querySelectorAll('.agent')).filter(row => row.querySelector('.obs-panel')).map(row => row.dataset.id));
  try {
  const data = await api('/agents');
  agents = data.agents || [];
  list.replaceChildren();
  select.innerHTML = '';
  $('#agents-count').textContent = agents.length + ' в реестре';

  for (const a of agents) {
    const row = el('div', 'agent');
    row.dataset.id = a.id;
    if (a.id === activeAgent) row.classList.add('active');
    row.append(el('span', 'id', a.id));
    row.append(obsBadge(a, row));
    row.append(el('span', 'transport', a.transport || ''));
    row.onclick = () => {
      document.querySelectorAll('.agent').forEach(n => n.classList.remove('active'));
      row.classList.add('active');
      select.value = a.id;
      if (!$('#run-prompt').value) $('#run-prompt').focus();
    };
    if (expanded.has(a.id)) row.append(obsPanel(a));
    list.append(row);

    if (a.enabled === false) { row.append(el('span', 'status', 'выключен')); if (expanded.has(a.id)) row.append(obsPanel(a)); list.append(row); continue; }
    const opt = el('option', '', a.id + (a.effective_state === 'ok' ? '' : ' · ' + (OBS_EFFECTIVE_RU[a.effective_state] || a.effective_state || 'нет данных')));
    opt.value = a.id;
    if (!a.bridge_agent) opt.disabled = true;
    select.append(opt);
  }
  agentNames = new Map();
  for (const a of agents) {
    agentNames.set(a.id, a.name || a.id);
    if (a.bridge_agent) agentNames.set(a.bridge_agent, a.name || a.bridge_agent);
  }
  const pickable = agents.filter(a => a.enabled !== false); const wanted = pickable.some(a => a.id === selectedAgent) ? selectedAgent : (pickable[0]?.id || '');
  select.value = wanted;
  if (wanted && wanted !== selectedAgent) await loadAgentProfiles(wanted);
  } finally { agentsLoading = false; }
}

// Профили и модели агента: заполняем селекты из реестра.
// Если адаптер агента пока не применяет выбор — селекты отключены с причиной.
async function loadAgentProfiles(agentId) {
  const gen = ++runProfileGen;
  const profileSel = $('#run-profile');
  const modelSel = $('#run-model');
  const hint = $('#run-model-hint');
  profileSel.innerHTML = '';
  modelSel.innerHTML = '';
  profileSel.disabled = false;
  modelSel.disabled = false;
  if (hint) hint.textContent = '';
  if (!agentId) return;
  const agent = agents.find(a => a.id === agentId);
  if (agent && !agent.model_selection) {
    profileSel.innerHTML = '<option value="">—</option>';
    modelSel.innerHTML = '<option value="">—</option>';
    profileSel.disabled = true;
    modelSel.disabled = true;
    if (hint) hint.textContent = agent.model_selection_reason || 'Адаптер этого агента пока не применяет выбор провайдера/модели.';
    return;
  }
  try {
    const d = await api('/agent-profiles/' + encodeURIComponent(agentId));
    if (gen !== runProfileGen || agentId !== $('#run-agent').value) return;
    const profiles = d.profiles || [];
    for (const p of profiles) {
      const opt = el('option', '', p.name || p.id);
      opt.value = p.id;
      profileSel.append(opt);
    }
    runCaps = d.capabilities || null;
    if (profiles.length) {
      profileSel.value = profiles[0].id;
      await loadProfileModels(agentId, profiles[0].id, profiles);
      updateCapabilityHint();
    } else {
      profileSel.disabled = true;
      modelSel.disabled = true;
      if (hint) hint.textContent = 'Для этого агента провайдер и модель не опубликованы; используется конфигурация агента.';
    }
  } catch (e) {
    if (gen !== runProfileGen || agentId !== $('#run-agent').value) return;
    profileSel.innerHTML = '<option value="">—</option>';
    modelSel.innerHTML = '<option value="">—</option>';
    profileSel.disabled = true;
    modelSel.disabled = true;
    if (hint) hint.textContent = 'Не удалось загрузить каталог провайдеров: ' + e.message;
  }
}

// FLOW-P1: запуски процесса — состояние читается из базы, браузер ничего не держит.
// Создание процесса с целью/критериями/назначениями/бюджетом и экран запуска:
// шаги, причины ожидания/ошибок, назначение, пакет, receipt проверок, решения,
// итог. После reload всё восстанавливается из БД (список + #run=<id> в адресе).
const RUN_STATE_RU = {active:'идёт', paused:'пауза', cancelled:'отменён', done:'завершён', failed:'ошибка'};
const TASK_STATE_RU = {pending:'ожидает', dispatched:'выдан', running:'в работе', done:'готово',
  failed:'ошибка', skipped:'пропущен', cancelled:'отменён', blocked:'заблокирован',
  waiting_context:'ждёт контекста'};
const RUN_EVENT_RU = {run_created:'процесс создан', task_dispatched:'шаг выдан', task_done:'шаг готов',
  task_failed:'ошибка шага', task_blocked:'шаг заблокирован', task_cancelled:'шаг отменён',
  task_waiting_context:'собирается контекст', task_waiting_input:'шаг ждёт вход',
  task_requeued:'возврат к кодеру', task_retry:'повтор', duplicate_result:'повтор результата проигнорирован',
  result_after_cancel:'результат после отмены', candidate_snapshot_started:'снимок кандидата',
  candidate_ready:'кандидат зафиксирован', checks_started:'проверки запущены',
  checks_done:'проверки завершены', checks_unavailable:'проверки недоступны',
  pipeline_failed:'сбой конвейера', run_done:'процесс завершён', run_failed:'процесс упал',
  run_cancel:'процесс отменён', run_pause:'пауза', run_resume:'продолжение',
  budget_exceeded:'бюджет исчерпан', run_created_existing:'повтор создания (тот же процесс)'};
let currentRunId = '';
let runDetailRequest = 0;
const runTaskStates = new Map();
let runCreatePending = false;
try { const match = String(window.location.hash || '').match(/run=([0-9a-f]+)/); if (match) currentRunId = match[1]; } catch (e) { /* тесты без window */ }

function runHashSet(runId) {
  try { window.history.replaceState(null, '', runId ? '#run=' + runId : window.location.pathname); } catch (e) { /* тесты */ }
}

async function loadRuns() {
  const box = $('#runs-list');
  const status = $('#runs-status');
  if (!box) return;
  try {
    const data = await api('/runs?limit=30');
    const items = data.items || [];
    status.textContent = items.length ? 'Запусков: ' + items.length : 'Запусков пока нет';
    box.replaceChildren();
    for (const run of items) {
      const row = el('article', 'run-row' + (run.id === currentRunId ? ' active' : ''));
      const head = el('div', 'run-head');
      const title = el('strong', '', (run.template_id || 'запуск') + ' · ' + run.id.slice(0, 8));
      if (run.goal) title.title = run.goal;
      head.append(title);
      head.append(el('span', 'badge run-' + run.state, RUN_STATE_RU[run.state] || run.state));
      head.append(el('span', 'muted', (run.done || 0) + ' из ' + (run.total || 0) +
        (run.current ? ' · сейчас: ' + run.current : '')));
      row.append(head);
      if (run.goal) row.append(el('div', 'muted run-goal', run.goal.slice(0, 140)));
      const bar = el('div', 'run-bar');
      const fill = el('span', 'run-bar-fill');
      fill.style.width = Math.round(100 * (run.done || 0) / Math.max(1, run.total || 0)) + '%';
      bar.append(fill);
      row.append(bar);
      const actions = el('div', 'row');
      const act = (label, action) => {
        const button = el('button', 'ghost small', label);
        button.type = 'button';
        button.onclick = async (event) => {
          event.stopPropagation();
          button.disabled = true;
          try { await api('/runs/' + run.id + '/' + action, {method: 'POST'}); await loadRuns(); await renderRunDetail(); }
          catch (error) { status.textContent = 'Не удалось: ' + error.message; }
          finally { button.disabled = false; }
        };
        return button;
      };
      const detail = el('button', 'ghost small', 'Подробнее');
      detail.type = 'button';
      detail.onclick = (event) => { event.stopPropagation(); openRunDetail(run.id); };
      actions.append(detail);
      if (run.state === 'active') { actions.append(act('Выполнить шаг', 'dispatch'), act('Пауза', 'pause')); }
      if (run.state === 'paused') actions.append(act('Продолжить', 'resume'));
      if (run.state === 'active' || run.state === 'paused') actions.append(act('Отменить', 'cancel'));
      row.append(actions);
      row.onclick = () => openRunDetail(run.id);
      box.append(row);
    }
  } catch (error) {
    status.textContent = 'Не удалось загрузить запуски: ' + error.message;
  }
  if (currentRunId) await renderRunDetail();
}

async function openRunDetail(runId) {
  currentRunId = runId;
  runHashSet(runId);
  await renderRunDetail();
  await loadRuns();
}

function runCloseDetail() {
  currentRunId = '';
  runDetailRequest++;
  runHashSet('');
  const box = $('#run-detail');
  if (box) { box.hidden = true; box.replaceChildren(); }
  loadRuns();
}

async function renderRunDetail() {
  const box = $('#run-detail');
  if (!box) return;
  const runId = currentRunId;
  const request = ++runDetailRequest;
  if (!runId) { box.hidden = true; return; }
  let run;
  try {
    run = await api('/runs/' + runId);
  } catch (error) {
    if (request !== runDetailRequest || currentRunId !== runId) return;
    box.hidden = false;
    box.replaceChildren(el('p', 'hint', 'Не удалось загрузить запуск: ' + error.message));
    return;
  }
  if (request !== runDetailRequest || currentRunId !== runId) return;
  box.hidden = false;
  box.replaceChildren();
  const head = el('div', 'run-head');
  head.append(el('strong', '', 'Запуск ' + run.id.slice(0, 8)));
  head.append(el('span', 'badge run-' + run.state, RUN_STATE_RU[run.state] || run.state));
  const close = el('button', 'ghost small', 'Закрыть');
  close.type = 'button';
  close.onclick = runCloseDetail;
  head.append(close);
  box.append(head);
  if (run.goal) box.append(el('p', 'run-goal', 'Цель: ' + run.goal));
  if (run.criteria && run.criteria.length) box.append(el('p', 'muted', 'Критерии: ' + run.criteria.join('; ')));
  const budgets = run.budgets || {}, used = run.budget_used || {};
  if (Object.keys(budgets).length) {
    const parts = [];
    if (budgets.max_dispatches) parts.push('шаги ' + (used.dispatches || 0) + '/' + budgets.max_dispatches);
    if (budgets.max_job_calls) parts.push('вызовы ' + (used.job_calls || 0) + '/' + budgets.max_job_calls);
    if (budgets.max_seconds) parts.push('время до ' + budgets.max_seconds + 'с');
    box.append(el('p', 'muted', 'Бюджет: ' + parts.join(' · ')));
  }
  if (run.candidate_digest) box.append(el('p', 'muted', 'Кандидат: ' + run.candidate_digest.slice(0, 24) + '…'));
  const receipt = run.check_receipt || null;
  if (receipt && receipt.checks) {
    const receiptBox = el('div', 'run-receipt');
    receiptBox.append(el('strong', '', 'Receipt проверок: ' +
      (receipt.ok ? 'пройдены' : 'есть падения') + ' (' + receipt.checks.length + ')'));
    for (const check of receipt.checks.slice(0, 10)) {
      receiptBox.append(el('div', 'muted', (check.passed ? '✓ ' : '✗ ') + check.name +
        ' — exit ' + check.exit_code));
    }
    box.append(receiptBox);
  }
  // Шаги: состояние, ошибка/причина ожидания, назначение, пакет, результат.
  const table = el('table', 'run-steps');
  table.append(el('thead'));
  const headerRow = el('tr');
  for (const label of ['Шаг', 'Роль', 'Состояние', 'Назначение', 'Пакет', 'Ошибка / причина']) {
    headerRow.append(el('th', '', label));
  }
  table.children[0].append(headerRow);
  const body = el('tbody');
  for (const task of run.tasks || []) {
    const previousState = runTaskStates.get(task.key);
    const row = el('tr', 'run-step-row' + (previousState !== undefined && previousState !== task.state ? ' run-step-changed' : ''));
    row.dataset.taskKey = task.key;
    row.dataset.taskState = task.state || '';
    row.append(el('td', '', task.key));
    row.append(el('td', '', task.role || '—'));
    const stateCell = el('td');
    stateCell.append(el('span', 'badge task-' + task.state, TASK_STATE_RU[task.state] || task.state));
    if (task.attempt) stateCell.append(el('span', 'muted', ' попытка ' + task.attempt));
    row.append(stateCell);
    const assignment = task.assignment || {};
    row.append(el('td', '', (assignment.agent_id || task.agent || '—') +
      (assignment.model ? ' · ' + assignment.model : '')));
    row.append(el('td', '', task.context_id ? task.context_id.slice(0, 8) +
      (task.packet_revision ? '@р' + task.packet_revision : '') : '—'));
    row.append(el('td', 'run-error', task.error || ''));
    body.append(row);
    if (task.result && Object.keys(task.result).length) {
      const resultRow = el('tr', 'run-result-row');
      const cell = el('td');
      cell.colSpan = 6;
      const resultDetails = el('details', 'run-result-details');
      resultDetails.append(el('summary', '', 'Результат'),
        el('pre', 'run-result-output', JSON.stringify(task.result, null, 2)));
      cell.append(resultDetails);
      resultRow.append(cell);
      body.append(resultRow);
    }
  }
  table.append(body);
  box.append(table);
  for (const task of run.tasks || []) runTaskStates.set(task.key, task.state);
  // Журнал решений и событий — последние 30.
  const eventsBox = el('div', 'run-events');
  eventsBox.append(el('strong', '', 'Журнал событий'));
  for (const event of (run.events || []).slice(-30)) {
    const label = RUN_EVENT_RU[event.event] || event.event;
    const detail = event.payload && (event.payload.reason || event.payload.error);
    const at = event.created_at ? event.created_at.slice(11, 19) : '';
    eventsBox.append(el('div', 'muted', at + ' ' + label + (detail ? ' — ' + String(detail).slice(0, 160) : '')));
  }
  box.append(eventsBox);
  // Итоговое состояние: почему процесс ждёт или упал.
  if (run.state === 'failed' || run.state === 'cancelled') {
    const last = (run.events || []).filter(e => e.event === 'run_failed' || e.event === 'run_cancel').pop();
    if (last && last.payload && last.payload.reason) {
      box.append(el('p', 'run-error', 'Причина: ' + last.payload.reason));
    }
  }
  if (run.cancel_info && (run.cancel_info.stop_requested || []).length) {
    box.append(el('p', 'muted', 'Остановка исполнителя не подтверждена для ' +
      run.cancel_info.stop_requested.length + ' заданий'));
  }
  const actions = el('div', 'row');
  const act = (label, action) => {
    const button = el('button', 'ghost small', label);
    button.type = 'button';
    button.onclick = async () => {
      button.disabled = true;
      try { await api('/runs/' + run.id + '/' + action, {method: 'POST'}); await renderRunDetail(); await loadRuns(); }
      catch (error) { box.append(el('p', 'run-error', 'Не удалось: ' + error.message)); }
      finally { button.disabled = false; }
    };
    return button;
  };
  if (run.state === 'active') { actions.append(act('Выполнить шаг', 'dispatch'), act('Пауза', 'pause')); }
  if (run.state === 'paused') actions.append(act('Продолжить', 'resume'));
  if (run.state === 'active' || run.state === 'paused') actions.append(act('Отменить', 'cancel'));
  if (actions.children.length) box.append(actions);
}

// Создание процесса: цель, критерии, назначения (снимок карточек ролей), бюджет,
// предконтроль доступности агентов и наличия утверждённых проверок проекта.
async function runCurrentProject() {
  const saved = storeGet('aihub.project');
  if (saved) return saved;
  try {
    const data = await api('/projects');
    const first = (data.projects || [])[0];
    return first ? first.id : '';
  } catch (e) { return ''; }
}

async function runPreflight() {
  const hint = $('#flow-preflight');
  const projectId = await runCurrentProject();
  if (!projectId) { hint.textContent = 'Сначала создайте проект во вкладке «Рабочее пространство».'; return ''; }
  try {
    const project = await api('/projects/' + projectId);
    const settings = project.settings || {};
    const checks = settings.checks && typeof settings.checks === 'object' ? Object.keys(settings.checks) : [];
    const lines = ['Проект: ' + (project.name || projectId.slice(0, 8)) +
      (project.workdir ? ' · ' + project.workdir : '')];
    lines.push('Утверждённые проверки проекта: ' + (checks.length ? checks.join(', ') :
      'нет — шаг тестирования будет ждать проверок и не пройдёт'));
    const agentsData = await api('/agents');
    const registry = new Map((agentsData.agents || []).map(a => [a.id, a]));
    const cards = project.roles || [];
    const box = $('#flow-assignments');
    box.replaceChildren();
    box.append(el('strong', '', 'Назначения (снимок на момент создания)'));
    for (const role of Object.keys(ROLE_META)) {
      const card = cards.find(c => c.role_id === role) || {};
      const agentId = card.agent_id || 'codex';
      const known = registry.has(agentId);
      const line = el('div', 'muted', role + ' → ' + agentId +
        (card.model ? ' · ' + card.model : '') + (known ? '' : ' (нет в реестре — шаг будет ждать)'));
      box.append(line);
      if (!known) lines.push('Агент «' + agentId + '» для роли ' + role + ' не зарегистрирован');
    }
    hint.textContent = lines.join('. ');
    return projectId;
  } catch (error) {
    hint.textContent = 'Предконтроль не завершился: ' + error.message;
    return projectId;
  }
}

async function runShowCreate() {
  const form = $('#flow-create');
  if (!form) return;
  form.hidden = false;
  await runPreflight();
  const goal = $('#flow-goal');
  if (goal) goal.focus();
}

async function runSubmitCreate(event) {
  event.preventDefault();
  const status = $('#flow-preflight');
  const submit = $('#flow-submit');
  if (runCreatePending) return;
  runCreatePending = true;
  submit.disabled = true;
  submit.textContent = 'Создаю…';
  try {
    const projectId = await runCurrentProject();
    if (!projectId) { status.textContent = 'Нет проекта: создайте его во вкладке «Рабочее пространство».'; return; }
    const goal = $('#flow-goal').value.trim();
    if (!goal) { status.textContent = 'Укажите цель процесса.'; $('#flow-goal').focus(); return; }
    const parseBudget = (id, label, min) => {
      const raw = $(id).value.trim();
      if (!raw) return {ok: true, value: null};
      const value = Number(raw);
      if (!Number.isSafeInteger(value) || value < min) {
        status.textContent = label + ': введите целое число не меньше ' + min + '.';
        $(id).focus();
        return {ok: false, value: null};
      }
      return {ok: true, value};
    };
    const rawCycles = $('#flow-max-cycles').value.trim();
    const cycles = rawCycles === '' ? 0 : Number(rawCycles);
    if (!Number.isSafeInteger(cycles) || cycles < 0) {
      status.textContent = 'Циклы исправлений: введите целое число не меньше 0.';
      $('#flow-max-cycles').focus();
      return;
    }
    const policy = {max_cycles: cycles, budgets: {}};
    for (const [id, key, label] of [['#flow-max-dispatches', 'max_dispatches', 'Максимум шагов'],
                                    ['#flow-max-calls', 'max_job_calls', 'Максимум вызовов'],
                                    ['#flow-max-seconds', 'max_seconds', 'Лимит времени']]) {
      const parsed = parseBudget(id, label, 1);
      if (!parsed.ok) return;
      if (parsed.value !== null) policy.budgets[key] = parsed.value;
    }
    const body = {
      project_id: projectId,
      template_id: 'five-roles',
      goal,
      criteria: $('#flow-criteria').value.split(';').map(s => s.trim()).filter(Boolean),
      entry: $('#flow-entry').value.trim(),
      contracts: $('#flow-contracts').value.split(',').map(s => s.trim()).filter(Boolean),
      policy,
      request_key: newKey(),
    };
    status.textContent = 'Создаю процесс…';
    const created = await api('/runs', {method: 'POST', body: JSON.stringify(body), timeout: 30000});
    $('#flow-create').hidden = true;
    $('#flow-create').reset ? $('#flow-create').reset() : null;
    status.textContent = '';
    await openRunDetail(created.run_id);
  } catch (error) {
    status.textContent = error.detail?.code === 'idempotency_conflict' || error.message === '409'
      ? 'Конфликт: такой ключ создания уже занят другим содержимым (409).'
      : 'Не удалось создать процесс: ' + (error.detail?.message || error.message);
  } finally {
    runCreatePending = false;
    submit.disabled = false;
    submit.textContent = 'Создать процесс';
  }
}

function runsWire() {
  const refresh = $('#runs-refresh');
  if (refresh) refresh.onclick = () => { loadRuns(); if (currentRunId) renderRunDetail(); };
  const create = $('#runs-new');
  if (create) create.onclick = () => runShowCreate();
  const cancel = $('#flow-create-cancel');
  if (cancel) cancel.onclick = () => { $('#flow-create').hidden = true; };
  const form = $('#flow-create');
  if (form) form.onsubmit = runSubmitCreate;
}

// ADAPTER-01: возможности канала под селектором модели. Выключенное — с причиной.
const CAP_RU = {text:'текст',patch:'файлы',workdir:'каталог',commands:'команды',
  usage:'usage',streaming:'поток',stop:'остановка',output_control:'лимит вывода'};
let runCaps = null;

function updateCapabilityHint() {
  const hint = $('#run-model-hint');
  if (!hint || !runCaps) return;
  const profileId = $('#run-profile').value;
  const modelId = $('#run-model').value;
  const profile = (runCaps.profiles || []).find(p => p.id === profileId);
  const entry = profile && (profile.models || []).find(m => (m.model || '') === modelId);
  if (!entry) { hint.textContent = ''; hint.title = ''; return; }
  const names = list => (list || []).map(n => CAP_RU[n] || n).join(', ');
  const off = entry.unsupported || [];
  hint.textContent = 'Доступно: ' + (names(entry.supported) || '—') +
    (off.length ? ' · выключено: ' + names(off) : '');
  hint.title = off.map(n => (CAP_RU[n] || n) + ': ' +
    ((entry.capabilities[n] || {}).reason || 'не подтверждено')).join('\n');
}

async function loadProfileModels(agentId, profileId, profiles) {
  const modelSel = $('#run-model');
  modelSel.innerHTML = '';
  const p = (profiles || []).find(x => x.id === profileId);
  const models = (p && p.models) || [];
  for (const m of models) {
    const value = typeof m === 'string' ? m : m.id;
    const opt = el('option', '', typeof m === 'string' ? m : (m.name || m.id));
    opt.value = value;
    modelSel.append(opt);
  }
}

$('#run-agent').onchange = () => loadAgentProfiles($('#run-agent').value);
$('#run-model').onchange = () => updateCapabilityHint();
$('#run-profile').onchange = async () => {
  const agentId = $('#run-agent').value;
  const profileId = $('#run-profile').value;
  const d = await api('/agent-profiles/' + encodeURIComponent(agentId)).catch(() => ({profiles:[]}));
  if (agentId !== $('#run-agent').value || profileId !== $('#run-profile').value) return;
  await loadProfileModels(agentId, profileId, d.profiles || []);
};

// ---------- запуск ----------
$('#run-form').onsubmit = async e => {
  e.preventDefault();
  const msg = $('#run-msg');
  msg.className = 'msg';
  msg.textContent = 'Отправка…';
  $('#run-submit').disabled = true;
  try {
    const agent = $('#run-agent').value;
    const prompt = $('#run-prompt').value.trim();
    const timeout = Number($('#run-timeout').value) || 300;
    // Если предыдущая отправка могла дойти до сервера, повтор идёт с тем же ключом:
    // иначе потерянный ответ превращается в дубль задания.
    const model = $('#run-model').value;
    const profile = $('#run-profile').value;
    const reuse = pendingRun && pendingRun.agent === agent
      && pendingRun.prompt === prompt && pendingRun.timeout === timeout
      && pendingRun.model === model && pendingRun.profile === profile;
    const key = reuse ? pendingRun.key : newKey();
    pendingRun = { agent, prompt, timeout, key, model, profile };
    const body = { prompt, timeout, idempotency_key: key };
    if (model) body.model = model;
    if (profile) body.profile = profile;
    const r = await api('/agents/' + encodeURIComponent($('#run-agent').value) + '/run', {
      method: 'POST', body: JSON.stringify(body),
    });
    pendingRun = null;   // подтверждено сервером
    msg.className = 'msg ok';
    msg.textContent = 'Задание ' + r.job_id.slice(0, 8) + ' принято (' + ruStatus(r.status) + ')';
    $('#run-prompt').value = '';
    await loadJobs();
    openJob(r.job_id);
  } catch (err) {
    msg.className = 'msg error';
    msg.textContent = err.message;
  } finally {
    $('#run-submit').disabled = false;
  }
};

// ---------- история ----------
function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDur(a, b, live) {
  const start = a ? new Date(a).getTime() : null;
  if (!start) return '—';
  const end = b ? new Date(b).getTime() : (live ? Date.now() : null);
  if (!end) return '—';
  const s = Math.max(0, (end - start) / 1000);
  if (s < 60) return s.toFixed(1) + ' с';
  const m = Math.floor(s / 60);
  return m + ' мин ' + Math.round(s % 60) + ' с';
}

document.querySelectorAll('.chip').forEach(chip => {
  chip.onclick = () => {
    document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    currentStatus = chip.dataset.status;
    jobsLoaded = 0;
    seenJobs.clear();
    listGen += 1;
    loadJobs();
  };
});

const moreBtn = $('#more');
if (moreBtn) moreBtn.onclick = () => { if (!listBusy) loadJobs(true); };

async function fetchJobsPage(limit, offset) {
  const q = '/jobs?limit=' + limit + '&offset=' + offset
    + (currentStatus ? '&status=' + encodeURIComponent(currentStatus) : '');
  return api(q);
}

async function loadJobs(more) {
  if (listBusy) return;
  listBusy = true;
  const gen = more ? listGen : ++listGen;
  try {
    // Сервер отдаёт не больше 200 строк за запрос. Если на экране больше,
    // опрос дочитывает окно по страницам — иначе список схлопывался до 200.
    const limit = more ? 50 : Math.max(50, jobsLoaded);
    const offset = more ? jobsLoaded : 0;
    let jobs = [];
    let total = 0;
    let hasMore = false;
    if (more) {
      const data = await fetchJobsPage(limit, offset);
      jobs = data.jobs || [];
      total = data.total ?? jobs.length;
      hasMore = !!data.has_more;
    } else {
      let got = 0;
      while (got < limit) {
        const data = await fetchJobsPage(Math.min(200, limit - got), offset + got);
        const page = data.jobs || [];
        jobs = jobs.concat(page);
        total = data.total ?? jobs.length;
        hasMore = !!data.has_more;
        got += page.length;
        if (page.length === 0 || !data.has_more || got >= limit) break;
        if (gen !== listGen) return;
      }
    }
    if (gen !== listGen) return;  // устаревший ответ: фильтр сменили или пришёл новый опрос
    const body = $('#jobs-body');
    if (!more) { body.innerHTML = ''; seenJobs.clear(); }
    for (const j of jobs) {
      if (seenJobs.has(j.id)) continue;  // защита от дублей при пересечении страниц
      seenJobs.add(j.id);
      const tr = el('tr');
      tr.dataset.id = j.id;
      if (currentJob && currentJob.id === j.id) tr.classList.add('selected');
      tr.append(el('td', '', fmtTime(j.created_at)));
      tr.append(el('td', 'mono', agentName(j.agent_id)));
      const st = el('td');
      st.append(el('span', 'badge ' + j.status, ruStatus(j.status)));
      tr.append(st);
      tr.append(el('td', '', fmtDur(j.created_at, j.finished_at, j.status === 'running')));
      tr.append(el('td', '', String(j.attempts ?? 0)));
      const modelCell = el('td', 'model');
      if (j.effective_model) {
        modelCell.textContent = j.effective_model;
        if (j.requested_model && j.effective_model !== j.requested_model) {
          modelCell.title = 'запрошено: ' + j.requested_model + ' \u2192 фактически: ' + j.effective_model;
        }
      } else if (j.requested_model) {
        modelCell.textContent = j.requested_model + ' (факт. не раскрыта)';
        modelCell.title = 'Запрошена ' + j.requested_model
          + '; фактическая модель инструментом не раскрыта.';
      } else {
        modelCell.textContent = '—';
      }
      tr.append(modelCell);
      tr.append(el('td', 'prompt', j.prompt_preview || ''));
      tr.onclick = () => openJob(j.id);
      body.append(tr);
    }
    jobsLoaded = more ? offset + jobs.length : jobs.length;
    jobsTotal = total;
    $('#jobs-empty').hidden = jobsLoaded > 0;
    const moreBtnEl = $('#more');
    if (moreBtnEl) moreBtnEl.hidden = !(hasMore || jobsLoaded < jobsTotal);
  } catch (e) {
    if (e.message !== 'unauthorized') $('#health-text').textContent = 'ошибка истории: ' + e.message;
  } finally {
    listBusy = false;
  }
}

// ---------- детали ----------
function agentName(id) {
  return id ? (agentNames.get(id) || id) : '—';
}

function openJob(id) {
  currentJob = { id };
  detailGen += 1;
  $('#d-note').textContent = '';
  $('#detail').hidden = false;
  document.querySelectorAll('#jobs-body tr').forEach(tr => tr.classList.toggle('selected', tr.dataset.id === id));
  renderJob(true);
}

$('#d-close').onclick = () => { detailGen += 1; currentJob = null; $('#detail').hidden = true; document.querySelectorAll('#jobs-body tr').forEach(tr => tr.classList.remove('selected')); };

$('#d-cancel').onclick = async () => {
  if (!currentJob) return;
  $('#d-cancel').disabled = true;
  try {
    const r = await api('/jobs/' + currentJob.id + '/cancel', { method: 'POST' });
    await renderJob();
    await loadJobs();
    if (r.status === 'cancel_requested') {
      $('#d-note').className = 'msg warn-text';
      $('#d-note').textContent = 'Отмена запрошена. Работающий агент остановку не подтверждает: если он завершит работу, результат сохранится.';
    } else if (r.status === 'cancelled') {
      $('#d-note').className = 'msg ok';
      $('#d-note').textContent = 'Задание отменено до запуска.';
    }
  } catch (e) {
    alert('Отмена не удалась: ' + e.message);
  } finally {
    $('#d-cancel').disabled = false;
  }
};

async function renderJob(first) {
  if (!currentJob) return;
  const id = currentJob.id;
  const gen = detailGen;
  try {
    const [job, ev] = await Promise.all([
      api('/jobs/' + id),
      api('/jobs/' + id + '/events').catch(() => ({ events: [] })),
    ]);
    // Пока грузили, могли открыть другую карточку или закрыть панель — поздний ответ игнорируем.
    if (gen !== detailGen || !currentJob || currentJob.id !== id) return;
    currentJob = job;

    $('#d-id').textContent = job.id;
    const meta = $('#d-meta');
    meta.innerHTML = '';
    const add = (k, v) => {
      const d = el('div');
      d.append(el('span', '', k + ': '));
      d.append(document.createTextNode(v));
      meta.append(d);
    };
    const snapshot = job.run_snapshot || {};
    add('Агент', agentName(snapshot.agent_id || job.agent_id));
    add('Проект', snapshot.project_name || snapshot.project_id || '—');
    add('Роль запуска', roleInfo(snapshot.role_id)[0]);
    add('Устройство', snapshot.host_id === 'server' ? 'Сервер' : snapshot.host_id === 'laptop' ? 'Ноутбук' : (snapshot.host_id || '—'));
    add('Рабочая папка', snapshot.workdir || '—');
    add('Профиль запроса', job.requested_profile || snapshot.provider_profile || '—');
    add('Модель запроса', job.requested_model || snapshot.model || '—');
    add('Фактическая модель', job.effective_model || 'инструментом не раскрыта');
    add('Статус', ruStatus(job.status));
    add('Создано', fmtTime(job.created_at));
    add('Завершено', job.finished_at ? fmtTime(job.finished_at) : '—');
    add('Длительность', fmtDur(job.created_at, job.finished_at, job.status === 'running'));
    add('Попыток', String(job.attempts ?? 0));
    add('Лимит времени', (job.timeout_s ?? '—') + ' с');
    add('Сверка', job.needs_reconcile ? 'нужна (результат неизвестен)' : 'не требуется');

    $('#d-output').textContent = job.result_output || (job.result_error ? 'Ошибка: ' + job.result_error : '—');

    const att = $('#d-attempts');
    att.innerHTML = '';
    const list = job.attempts_detail || [];
    if (!list.length) att.append(el('p', 'muted', 'Попыток не было — задание ещё в очереди.'));
    for (const a of list) {
      const box = el('div', 'attempt');
      const row = el('div', 'row');
      row.append(el('span', 'badge ' + a.status, ruStatus(a.status)));
      row.append(el('span', 'mono', 'попытка №' + a.attempt_no));
      row.append(el('span', 'muted', a.worker || '—'));
      row.append(el('span', 'muted', fmtDur(a.started_at, a.finished_at, a.status === 'running')));
      if (a.usage && Object.keys(a.usage).length) {
        row.append(el('span', 'muted', 'токены ' + (a.usage.input_tokens ?? '?') + ' / ' + (a.usage.output_tokens ?? '?')));
      }
      box.append(row);
      if (a.error) box.append(el('div', 'err', a.error));
      att.append(box);
    }

    const evs = $('#d-events');
    evs.innerHTML = '';
    const items = (ev && ev.events) || [];
    if (!items.length) evs.append(el('li', 'muted', 'событий нет'));
    for (const e of items) {
      const li = el('li');
      li.innerHTML = '<b>' + esc(ruEvent(e.event)) + '</b> — ' + esc(fmtTime(e.at)) +
        (e.attempt_id ? ' <span class="muted">(' + esc(e.attempt_id.slice(0, 8)) + ')</span>' : '');
      evs.append(li);
    }

    $('#d-cancel').hidden = !(job.status === 'queued' || job.status === 'running');

    if (job.status === 'queued' || job.status === 'running') {
      clearTimeout(renderJob._t);
      renderJob._t = setTimeout(() => renderJob(false), 2000);
    }
  } catch (e) {
    if (first) $('#d-id').textContent = 'не удалось загрузить: ' + e.message;
  }
}


// Workspace UI. Jobs and diagnostics use the same API and queue.
const ROLE_META = {
  orchestrator: ['Оркестратор','O','Ставит задачи ролям и собирает результат'],
  planner: ['Архитектор','A','План, требования и технические решения'],
  coder: ['Кодер','C','Реализация и изменения в проекте'],
  tester: ['Тестер','T','Проверки и воспроизведение ошибок'],
  reviewer: ['Ревьюер','R','Проверка результата и замечания'],
  archivist: ['Архивариус','D','Документация и сохранение решений'],
};
const WS={projects:[],project:null,convs:[],conv:null,roles:{},atts:[],messages:[],selectedAtts:new Set(),attFilter:'',roleId:'architect',profiles:new Map(),generation:0,editorGen:0,editorProfilesLoading:false,pollTimer:null,pending:null,dirty:false,sending:false,view:'workspace',newProject:false,projectSaving:false,previewUrls:new Map(),previewLoads:new Map(),previewGeneration:0,activeImageByConversation:new Map(),convFilter:'',convLimit:24,convSignature:'',convShownId:'',msgSignature:'',convCursor:'',convHasMore:false,convTotal:0,convLoading:false,convSeq:0,convFilterTimer:null,reasoning:null,reasoningKey:'',reasoningSpec:null};

function toast(message,error=false){const box=$('#toast');box.textContent=message;box.classList.toggle('error',error);box.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>box.hidden=true,5500)}
function workspaceError(error){const text=String(error?.message||error);if(text==='unauthorized')return;$('#workspace-error-text').textContent=text.includes('404')||text==='Not Found'?'Сервер ещё не обновлён: API проектов недоступен. Обновите backend вместе с интерфейсом.':text;$('#workspace-error').hidden=false}
function action(fn){return async(...args)=>{try{return await fn(...args)}catch(error){workspaceError(error);toast(error.message||String(error),true)}}}
function switchView(name){WS.view=name;document.querySelectorAll('.tab').forEach(b=>{b.classList.toggle('active',b.dataset.view===name);b.setAttribute('aria-pressed',String(b.dataset.view===name))});$('#view-workspace').hidden=name!=='workspace';$('#view-ops').hidden=name!=='ops';$('#detail').hidden=true;currentJob=null;if(name==='workspace'&&token)wsRefreshAll().catch(workspaceError);if(name==='ops'&&token){loadJobs();loadRuns();loadAgents().catch(error=>{if(error.message!=='unauthorized')$('#health-text').textContent='ошибка наблюдений: '+error.message});reconLoad(false)}}
function putOptions(select,items,value){select.replaceChildren();for(const item of items){const option=el('option','',item.name||item.id);option.value=item.id;select.append(option)}if(items.some(x=>x.id===value))select.value=value;else if(items.length)select.value=items[0].id}
function roleInfo(id){return ROLE_META[id]||[id,'?','']}
function selectedAssignment(){return WS.roles[WS.roleId]||{role_id:WS.roleId,agent_id:'codex',profile:'',model:'',instructions:''}}
function setDraftDirty(){WS.dirty=true;$('#role-save-status').textContent=WS.editorProfilesLoading?'Загружаю провайдеры и модели…':'Есть несохранённые настройки';$('#role-save').textContent='Сохранить назначение'}
function conversationStorageKey(){return 'aihub.conv.'+(WS.project?.id||'')}

async function wsRefreshAll(){
 const gen=++WS.generation;WS.convCursor='';WS.convHasMore=false;WS.convTotal=0;WS.convSeq++;$('#workspace-error').hidden=true;
 const data=await api('/projects');if(gen!==WS.generation)return;
 WS.projects=data.projects||[];const saved=storeGet('aihub.project');const previous=WS.project?.id;
 WS.project=WS.projects.find(p=>p.id===saved)||WS.projects[0]||null;
 wsRenderProjects();
 if(!WS.project){wsClearPreviewUrls();WS.roles={};WS.convs=[];WS.conv=null;WS.messages=[];renderMessages();renderRoles();$('#project-title').textContent='Создайте первый проект';$('#project-location').textContent='Рабочая папка, чаты и назначения в одном месте';$('#conv-new').disabled=true;return}
 $('#conv-new').disabled=false;storeSet('aihub.project',WS.project.id);
 const projectId=WS.project.id;
 const [project,attachments]=await Promise.all([api('/projects/'+projectId),api('/projects/'+projectId+'/attachments')]);
 if(gen!==WS.generation||WS.project?.id!==projectId)return;
 WS.project=project;WS.roles=Object.fromEntries((project.roles||[]).map(r=>[r.role_id,r]));if(previous!==projectId){wsClearPreviewUrls();WS.selectedAtts.clear()}WS.atts=attachments.attachments||[];
 await wsLoadConversations({reset:true,quiet:true});
 const savedConv=storeGet(conversationStorageKey());
 let openConv=WS.convs.find(c=>c.id===savedConv)||null;
 if(!openConv&&savedConv){try{openConv=await api('/conversations/'+savedConv)}catch(error){openConv=null}}
 WS.conv=openConv||WS.convs[0]||null;
 if(WS.conv?.role_id&&ROLE_META[WS.conv.role_id])WS.roleId=WS.conv.role_id;
 $('#project-title').textContent=project.name;$('#project-location').textContent=(project.host_id==='server'?'Сервер':'Ноутбук')+' · '+(project.workdir||'Рабочая папка не указана');
 wsRenderProjects();wsRenderConversations();wsRenderAttachments();renderRoles();wsLoadJobs();await wsEditRole(WS.roleId);await wsLoadMessages();wsStartPolling();
}

function wsRenderProjects(){const box=$('#projects-list');box.replaceChildren();for(const project of WS.projects){const button=el('button','proj'+(project.id===WS.project?.id?' active':''));button.type='button';button.setAttribute('aria-pressed',String(project.id===WS.project?.id));button.append(el('span','project-icon',project.name.slice(0,1).toUpperCase()),el('span','project-text',project.name));button.onclick=action(async()=>{if(WS.project?.id===project.id)return;storeSet('aihub.project',project.id);WS.conv=null;WS.messages=[];WS.selectedAtts.clear();WS.pending=null;await wsRefreshAll()});box.append(button)}if(!WS.projects.length)box.append(el('p','sidebar-empty','Добавьте проект, чтобы начать.'))}
function wsConversationTitle(conversation){return (conversation&&conversation.title)||'Новый чат'}
function wsChatCount(n){const lastTwo=n%100,last=n%10;const word=lastTwo>=11&&lastTwo<=14?'чатов':last===1?'чат':last>=2&&last<=4?'чата':'чатов';return n+' '+word}
function wsConversationParams(reset){
 const params=new URLSearchParams({limit:String(reset?24:24)});
 const filter=(WS.convFilter||'').trim();if(filter)params.set('q',filter);
 if(!reset&&WS.convCursor)params.set('cursor',WS.convCursor);
 return params
}
async function wsLoadConversations(options){
 const settings=options||{},reset=!!settings.reset,projectId=WS.project?.id;
 if(!projectId)return;
 if(reset){WS.convCursor='';WS.convHasMore=false;WS.convLoading=true;wsRenderConversations()}
 const seq=++WS.convSeq;WS.convLoading=true;wsRenderConversations();
 try{
  const data=await api('/projects/'+projectId+'/conversations?'+wsConversationParams(reset).toString());
  if(seq!==WS.convSeq||WS.project?.id!==projectId)return;
  const page=data.conversations||[];
  if(reset)WS.convs=[];
  const known=new Set(WS.convs.map(conversation=>conversation.id));
  for(const conversation of page)if(!known.has(conversation.id)){WS.convs.push(conversation);known.add(conversation.id)}
  WS.convCursor=data.next_cursor||'';WS.convHasMore=!!data.has_more;
  WS.convTotal=Number.isFinite(Number(data.total))?Number(data.total):WS.convs.length;
  wsRenderConversations()
 }catch(error){if(seq===WS.convSeq&&!settings.quiet)workspaceError(error)}
 finally{if(seq===WS.convSeq){WS.convLoading=false;wsRenderConversations()}}
}
function wsLoadMoreConversations(){if(WS.convLoading)return Promise.resolve();return wsLoadConversations({})}
function wsConversationSummary(loaded,total,filtered){
 if(!total)return 'чатов пока нет';
 if(filtered)return 'найдено '+wsChatCount(loaded)+(total>loaded?' из '+wsChatCount(total):'');
 return wsChatCount(total)+(loaded<total?' · показаны '+loaded:'');
}
function wsRenderConversations(){
 const box=$('#convs-list');if(!box)return;
 const all=WS.convs||[],filter=(WS.convFilter||'').trim(),activeId=WS.conv?.id||'';
 const shown=all;
 const pinned=WS.conv&&!all.some(conversation=>conversation.id===WS.conv.id)?WS.conv:null;
 const summary=$('#conv-summary');if(summary)summary.textContent=wsConversationSummary(all.length,WS.convTotal||all.length,!!filter);
 const more=$('#conv-more');
 if(more){more.hidden=!WS.convHasMore;more.textContent=WS.convHasMore?(WS.convLoading?'Загружаю…':'Показать ещё'):'Показать ещё ';more.disabled=!!WS.convLoading}
 const signature=[filter,all.length,activeId,pinned?pinned.id:'',WS.convHasMore?'1':'0',
  all.map(conversation=>conversation.id+''+wsConversationTitle(conversation)).join('')].join('');
 if(signature===WS.convSignature&&box.childElementCount>0)return;
 WS.convSignature=signature;
 const previousScroll=box.scrollTop||0,activeChanged=WS.convShownId!==activeId;WS.convShownId=activeId;
 box.replaceChildren();
 const add=conversation=>{
  const isActive=conversation.id===activeId;
  const button=el('button','conv'+(isActive?' active':''));button.type='button';
  button.title=wsConversationTitle(conversation);button.setAttribute('aria-current',isActive?'true':'false');
  button.append(el('span','chat-glyph','◇'),el('span','conv-text',wsConversationTitle(conversation)));
  button.onclick=action(()=>wsOpenConversation(conversation));box.append(button)
 };
 if(pinned)add(pinned);
 for(const conversation of shown)add(conversation);
 if(!all.length)box.append(el('p','sidebar-empty',filter?'Ничего не найдено. Измените запрос.':'Здесь появятся разговоры проекта.'));
 box.scrollTop=activeChanged?0:previousScroll;
 const current=typeof box.querySelector==='function'?box.querySelector('.conv.active'):null;
 if(current&&typeof current.scrollIntoView==='function')current.scrollIntoView({block:'nearest'});
}
async function wsOpenConversation(conversation){WS.conv=conversation;storeSet(conversationStorageKey(),conversation.id);WS.messages=[];WS.pending=null;if(conversation.role_id&&ROLE_META[conversation.role_id])WS.roleId=conversation.role_id;wsRenderConversations();renderRoles();await wsEditRole(WS.roleId);await wsLoadMessages();$('#chat-input').focus()}
async function wsNewConversation(){if(!WS.project)throw Error('Сначала создайте или выберите проект.');const projectId=WS.project.id,button=$('#conv-new');button.disabled=true;try{const conversation=await api('/projects/'+projectId+'/conversations',{method:'POST',body:JSON.stringify({role_id:WS.roleId,title:'Новый чат'})});if(WS.project?.id!==projectId)return;WS.convFilter='';const filterBox=$('#conv-filter');if(filterBox)filterBox.value='';WS.convCursor='';WS.convHasMore=false;WS.convTotal=0;delete WS.convSignature;await wsLoadConversations({reset:true});if(WS.project?.id!==projectId)return;WS.conv=conversation;WS.convs=[conversation,...WS.convs.filter(item=>item.id!==conversation.id)];storeSet(conversationStorageKey(),conversation.id);WS.messages=[];WS.pending=null;if(conversation.role_id&&ROLE_META[conversation.role_id])WS.roleId=conversation.role_id;wsRenderConversations();renderRoles();await wsEditRole(WS.roleId);await wsLoadMessages();wsStartPolling();$('#chat-input').focus();toast('Новый чат создан')}finally{button.disabled=false}}

function wsIsImageAttachment(attachment){return attachment?.kind==='image'||/^image\/(png|jpeg)$/i.test(attachment?.mime||'')}
function wsRevokePreview(id){const url=WS.previewUrls.get(id);if(url){URL.revokeObjectURL(url);WS.previewUrls.delete(id)}}
function wsClearPreviewUrls(){WS.previewGeneration++;for(const url of WS.previewUrls.values())URL.revokeObjectURL(url);WS.previewUrls.clear();WS.previewLoads.clear()}
async function wsPreviewImage(attachment,image,status){
 const cached=WS.previewUrls.get(attachment.id);if(cached){image.src=cached;status.textContent='';return}
 const generation=WS.previewGeneration;let pending=WS.previewLoads.get(attachment.id);
 if(!pending){
  let request;
  request=(async()=>{
   const raw=attachment.preview_url||('/attachments/'+encodeURIComponent(attachment.id)+'/preview');
   const target=new URL(raw,window.location.href);if(target.origin!==window.location.origin)throw Error('Адрес предпросмотра должен принадлежать AI Hub.');
   if(!token)throw Error('Введите токен доступа для предпросмотра.');
   const response=await fetch(target.pathname+target.search,{headers:{Authorization:'Bearer '+token},cache:'no-store'});
   if(response.status===401){if(typeof window.openGate==='function')window.openGate('Токен не подошёл — введите его снова.');throw Error('Требуется токен доступа.')}
   if(!response.ok)throw Error('Сервер не загрузил предпросмотр ('+response.status+').');
   const type=(response.headers.get('content-type')||'').split(';')[0].toLowerCase();if(!['image/png','image/jpeg'].includes(type))throw Error('Предпросмотр должен быть PNG или JPEG.');
   const url=URL.createObjectURL(await response.blob());
   if(generation!==WS.previewGeneration||!WS.atts.some(item=>item.id===attachment.id)){URL.revokeObjectURL(url);return null}
   WS.previewUrls.set(attachment.id,url);return url;
  })().finally(()=>{if(WS.previewLoads.get(attachment.id)===request)WS.previewLoads.delete(attachment.id)});
  pending=request;WS.previewLoads.set(attachment.id,pending)
 }
 try{const url=await pending;if(url&&image.isConnected){image.src=url;status.textContent=''}}catch(error){if(image.isConnected)status.textContent='Предпросмотр недоступен: '+error.message}
}
function wsBytes(n){const v=Number(n)||0;if(v<1024)return v+' Б';if(v<1048576)return (v/1024).toFixed(v<10240?1:0)+' КБ';return (v/1048576).toFixed(1)+' МБ'}
function wsAttachmentUsed(attachment){if(!attachment||!attachment.id)return false;if(wsLatestConversationImage()?.id===attachment.id)return true;return (WS.messages||[]).some(message=>{const list=message.attachments||message.attachment_metadata||[];return list.some(item=>item&&item.id===attachment.id)})}
function wsAttachmentState(attachment){if(WS.selectedAtts.has(attachment.id))return 'выбрано для следующей задачи';if(wsAttachmentUsed(attachment))return 'использовано в чате';return attachment.origin==='host-file'?'доступно в проекте':'загружено в проект'}
function wsContextSummary(){const total=WS.atts.length,selected=WS.selectedAtts.size,images=WS.atts.filter(wsIsImageAttachment).length;const active=wsLatestConversationImage();const parts=[];parts.push(total?wsFilesCount(total)+' в проекте':'файлов пока нет');parts.push(selected?'выбрано '+selected+' из 5':'ничего не выбрано');if(images)parts.push('изображений: '+images);if(active&&active.id)parts.push('активное изображение: '+(active.filename||'из истории')+((active.reused||active.source==='conversation')?' (из истории)':' (новое)'));const box=$('#context-summary');if(box)box.textContent=parts.join(' · ')}
function wsRenderAttachments(){
 const box=$('#atts-list');box.replaceChildren();const live=new Set(WS.atts.filter(wsIsImageAttachment).map(item=>item.id));for(const id of WS.previewUrls.keys())if(!live.has(id))wsRevokePreview(id);
 const filter=(WS.attFilter||'').trim().toLowerCase();const shown=WS.atts.filter(item=>!filter||(item.filename||'').toLowerCase().includes(filter));
 for(const attachment of shown){
  const label=el('label','attachment'+(wsIsImageAttachment(attachment)?' attachment-image':'')+(WS.selectedAtts.has(attachment.id)?' is-selected':''));const input=document.createElement('input');input.type='checkbox';input.checked=WS.selectedAtts.has(attachment.id);input.setAttribute('aria-label','Включить файл '+attachment.filename);input.onchange=()=>{if(input.checked&&wsIsImageAttachment(attachment)){for(const old of WS.atts)if(old.id!==attachment.id&&wsIsImageAttachment(old))WS.selectedAtts.delete(old.id)}if(input.checked&&!WS.selectedAtts.has(attachment.id)&&WS.selectedAtts.size>=5){input.checked=false;$('#chat-msg').className='msg warn-text';$('#chat-msg').textContent='К задаче можно выбрать не более 5 файлов.';return}input.checked?WS.selectedAtts.add(attachment.id):WS.selectedAtts.delete(attachment.id);wsRenderAttachments()};
  const text=el('span','attachment-name',attachment.filename);text.append(el('small','',wsAttachmentState(attachment)+' · '+(wsIsImageAttachment(attachment)?(attachment.mime||'изображение'):(attachment.mime||'текст'))+(attachment.size_bytes?' · '+wsBytes(attachment.size_bytes):'')));
  label.append(input,text);
  if(wsIsImageAttachment(attachment)){const image=document.createElement('img');image.className='attachment-preview';image.alt='Предпросмотр '+attachment.filename;image.loading='lazy';const status=el('small','preview-status','Загружаю предпросмотр…');label.append(image,status);if(attachment.metadata?.width&&attachment.metadata?.height)label.append(el('small','attachment-dimensions',attachment.metadata.width+' × '+attachment.metadata.height+' px'));wsPreviewImage(attachment,image,status)}
  box.append(label)
 }
 if(!WS.atts.length)box.append(el('p','sidebar-empty','Добавьте текстовый файл или PNG/JPEG.'));
 else if(!shown.length)box.append(el('p','sidebar-empty','Нет файлов, совпадающих с фильтром.'));
 const clear=$('#att-clear');if(clear)clear.hidden=!WS.selectedAtts.size;
 wsContextSummary();wsContextHint()
}
function wsFilesCount(n){const lastTwo=n%100,last=n%10;const word=lastTwo>=11&&lastTwo<=14?'файлов':last===1?'файл':last>=2&&last<=4?'файла':'файлов';return n+' '+word}
function wsContextHint(){const selected=WS.atts.filter(a=>WS.selectedAtts.has(a.id));$('#chat-ctx').textContent=selected.length?'Выбрано: '+wsFilesCount(selected.length):'';const box=$('#selected-context');box.replaceChildren();for(const attachment of selected){const tag=el('span','context-tag');tag.title=attachment.filename;tag.append(el('span','context-tag-name',attachment.filename),el('small','context-tag-meta',(wsIsImageAttachment(attachment)?'изображение':'текст')+(attachment.size_bytes?' · '+wsBytes(attachment.size_bytes):'')));box.append(tag)}if(selected.length){const clear=el('button','clear-context','Снять выбор');clear.type='button';clear.onclick=()=>{WS.selectedAtts.clear();wsRenderAttachments()};box.append(clear)}if(WS.atts.length){const link=el('a','context-link',selected.length?'Открыть контекст ↓':'Контекст и файлы ↓');link.href='#context-panel';link.onclick=event=>{const panel=$('#context-panel');if(!panel)return;event.preventDefault();if(typeof panel.scrollIntoView==='function')panel.scrollIntoView({block:'start',behavior:'smooth'});if(typeof panel.focus==='function')panel.focus({preventScroll:true})};box.append(link)}wsUpdateRecipient()}
function wsModelCapabilities(data,assignment){
 const profile=(data?.profiles||[]).find(item=>item.id===(assignment?.profile||data?.default_profile));
 const model=(profile?.models||[]).find(item=>(typeof item==='string'?item:item?.id)===(assignment?.model||''));
 const modelCapabilities=profile?.model_capabilities?.[assignment?.model]||{};
 const values=[modelCapabilities,model&&typeof model==='object'?model:{},profile||{},data||{}];
 const first=(key,fallback=null)=>{for(const item of values)if(item&&item[key]!==undefined&&item[key]!==null)return item[key];return fallback};
 const required=[...new Set(values.flatMap(item=>Array.isArray(item?.required_modalities)?item.required_modalities:[]))];const input=first('input_modalities',['text']);
 return {requiresImage:values.some(item=>item?.requires_image===true)||required.includes('image'),required,input,contextMode:first('context_mode',''),filesystemAccess:first('filesystem_access',null),supportsWorkdir:first('supports_workdir',null)}
}
function wsLatestConversationImage(){
 const remembered=WS.activeImageByConversation?.get(WS.conv?.id);if(remembered)return remembered;
 for(let i=WS.messages.length-1;i>=0;i--){const message=WS.messages[i];const direct=message.active_image||message.image_attachment;if(direct?.id)return direct;const id=message.active_image_id;if(id)return {id,filename:message.active_image_filename||'изображение из истории',source:'conversation',reused:true};const attachments=message.attachments||message.attachment_metadata||[];for(let j=attachments.length-1;j>=0;j--)if(wsIsImageAttachment(attachments[j]))return {...attachments[j],source:'conversation',reused:true}}
 return null
}
function wsUpdateRoleCapability(agentId,profileId,modelId){
 const data=WS.profiles.get(agentId)||{},agent=agents.find(item=>item.id===agentId)||{};
 if(!(data.model_selection??agent.model_selection)){return data.model_selection_reason||agent.model_selection_reason||'Выбор модели для этого адаптера недоступен.'}
 const caps=wsModelCapabilities(data,{agent_id:agentId,profile:profileId,model:modelId});const notes=[];
 if(caps.requiresImage)notes.push('Обязательно изображение PNG/JPEG.')
 if(caps.contextMode)notes.push(caps.contextMode)
 if(caps.filesystemAccess===false&&!/файл|file/i.test(caps.contextMode))notes.push('Без доступа к файлам проекта и файловым инструментам.')
 if(agent.transport!=='http'&&caps.supportsWorkdir===false)notes.push('Этот канал пока не запускает задачи в папке проекта.')
 return 'Назначение применяется к новым задачам этой роли.'+(notes.length?' '+[...new Set(notes)].join(' '):'')
}
async function wsUpload(files){
 if(!WS.project)throw Error('Сначала выберите проект.');
 const newImageCount=files.filter(file=>['png','jpg','jpeg'].includes((file.name.split('.').pop()||'').toLowerCase())).length;if(newImageCount>1)throw Error('За один шаг можно выбрать только одно новое изображение PNG/JPEG.');
 const oldSelectedImageCount=WS.atts.filter(item=>WS.selectedAtts.has(item.id)&&wsIsImageAttachment(item)).length;if(WS.selectedAtts.size-(newImageCount?oldSelectedImageCount:0)+files.length>5)throw Error('К задаче можно выбрать не более 5 файлов. Снимите выбор перед добавлением новых.');
 const projectId=WS.project.id;const textExtensions=new Set(['txt','md','csv','json','log','yaml','yml','html','py','js','ts','tsx','jsx','css','sql','toml','ini','xml','sh','ps1']);
 for(const file of files){
  if(file.size>2*1024*1024)throw Error(file.name+': размер больше 2 МБ');
  const extension=(file.name.split('.').pop()||'').toLowerCase();const declared=(file.type||'').toLowerCase();
  const imageMime=extension==='png'?'image/png':['jpg','jpeg'].includes(extension)?'image/jpeg':null;
  const isImage=!!imageMime;
  if(declared.startsWith('image/')&&(!isImage||declared!==imageMime))throw Error(file.name+': поддерживаются только PNG и JPEG с подходящим расширением.');
  if(!isImage&&!textExtensions.has(extension))throw Error(file.name+': поддерживаются UTF-8 текстовые файлы, PNG и JPEG.');
  const bytes=new Uint8Array(await file.arrayBuffer());if(!isImage){try{new TextDecoder('utf-8',{fatal:true}).decode(bytes)}catch{throw Error(file.name+': файл должен быть в UTF-8.')}}
  let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
  const attachment=await api('/projects/'+projectId+'/attachments',{method:'POST',body:JSON.stringify({filename:file.name,mime:isImage?imageMime:(declared||'text/plain'),content_b64:btoa(binary)}),timeout:20000});
  if(WS.project?.id===projectId){if(isImage)for(const old of WS.atts)if(wsIsImageAttachment(old))WS.selectedAtts.delete(old.id);WS.atts.unshift(attachment);WS.selectedAtts.add(attachment.id)}
 }
 if(WS.project?.id===projectId){wsRenderAttachments();toast('Файлы добавлены в контекст')}
 }
 function wsSetDropState(active){const panel=$('#context-panel');if(panel)panel.classList.toggle('is-drop-target',active)}

async function wsLoadMessages(){if(!WS.conv){WS.messages=[];renderMessages();return}const id=WS.conv.id,gen=WS.generation;const data=await api('/conversations/'+id+'/messages?limit=200');if(WS.conv?.id!==id||gen!==WS.generation)return;WS.messages=data.messages||[];renderMessages()}
function wsMessageSignature(){const text=JSON.stringify(WS.messages||[]);let hash=2166136261;for(let i=0;i<text.length;i++){hash^=text.charCodeAt(i);hash=Math.imul(hash,16777619)}return (WS.conv?.id||'')+'|'+text.length+'|'+(hash>>>0).toString(36)}
function renderMessages(){
 const box=$('#chat-log');const signature=wsMessageSignature();const nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<100;
 if(signature===WS.msgSignature&&box.childElementCount>0){if(nearBottom)box.scrollTop=box.scrollHeight;return}
 const previousScroll=box.scrollTop||0;WS.msgSignature=signature;box.replaceChildren();$('#chat-title').textContent=WS.conv?.title||'Новый чат';
 if(!WS.messages.length){const welcome=el('div','welcome');welcome.append(el('div','welcome-symbol','AI'),el('h2','','Работа начинается с задачи'),el('p','','Выберите роль, добавьте нужные файлы и расскажите, что сделать. История и настройки останутся в проекте.'));const suggestions=el('div','suggestions');for(const text of ['Разобрать архитектуру проекта','Исправить ошибку','Проверить изменения']){const button=el('button','',text);button.type='button';button.onclick=()=>{$('#chat-input').value=text;$('#chat-input').focus()};suggestions.append(button)}welcome.append(suggestions);box.append(welcome)}
 for(const message of WS.messages){
  const row=el('article','msg-row '+message.author),isUser=message.author==='user';
  const role=roleInfo(message.job_role_id||WS.conv?.role_id||WS.roleId);
  row.append(el('span','message-avatar',isUser?'Вы':role[1]));
  const content=el('div','message-content');
  const heading=el('div','message-heading',isUser?'Вы':role[0]);
  if(message.created_at)heading.append(el('time','',fmtTime(message.created_at)));
  if(!isUser){
   const attribution=[];
   if(message.job_agent_id)attribution.push(agentName(message.job_agent_id));
   if(message.requested_profile)attribution.push(message.requested_profile);
   const model=message.effective_model||(message.requested_model?message.requested_model+' (запрошена)':'');
   if(model)attribution.push(model);
   if(attribution.length)heading.append(el('span','message-attribution',attribution.join(' · ')));
  }
  const state=message.job_status||message.state;
  if(state&&state!=='done')heading.append(el('span','message-state '+state,ruStatus(state)));
  content.append(heading,el('div','msg-body',message.body||''));
  if(!isUser&&message.job_id){const details=el('button','message-run-link','Детали запуска');details.type='button';details.setAttribute('aria-label','Открыть детали запуска '+message.job_id);details.onclick=()=>openJob(message.job_id);content.append(details)}
  row.append(content);box.append(row);
 }
 if(!WS.messages.length)box.scrollTop=0;
 else if(nearBottom)box.scrollTop=box.scrollHeight;
 else box.scrollTop=previousScroll;
 wsUpdateRecipient();
}
const REASONING_LABELS={minimal:'Минимальный',low:'Низкий',medium:'Средний',high:'Высокий',xhigh:'Очень высокий',max:'Максимальный',auto:'Авто',fast:'Быстро',thinking:'Размышление'};
function wsReasoningLabel(level){return REASONING_LABELS[level]||level}
function wsReasoningSpec(data,assignment){
 const catalog=data&&data.reasoning;if(!catalog||!catalog.models)return null;
 const profile=(data.profiles||[]).find(item=>item.id===(assignment?.profile||data.default_profile));
 const models=(profile?.models||[]).map(item=>typeof item==='string'?item:item?.id).filter(Boolean);
 const model=assignment?.model||models[0]||'';
 const spec=catalog.models[model];
 if(!spec||!Array.isArray(spec.levels)||!spec.levels.length)return null;
 return {model,levels:spec.levels.slice(),default:spec.default||''}
}
function wsReasoningText(){return WS.reasoning?'Размышление · '+wsReasoningLabel(WS.reasoning):'Размышление · По умолчанию'}
function wsReasoningMenuOpen(menu){if(!menu)return false;if(menu._reasoningOpen!==undefined)return menu._reasoningOpen;if(typeof menu.matches==='function')return menu.matches(':popover-open');return !menu.hidden}
function wsPositionReasoningMenu(menu,button){if(!menu||!button)return;const rect=button.getBoundingClientRect(),width=Math.min(300,window.innerWidth-24),left=Math.max(12,Math.min(rect.left,window.innerWidth-width-12));menu.style.left=left+'px';menu.style.bottom=Math.max(12,window.innerHeight-rect.top+8)+'px';menu.style.top='auto';menu.style.right='auto'}
function wsShowReasoningMenu(menu,button){
 if(!menu)return;
 wsPositionReasoningMenu(menu,button);
 menu._reasoningOpen=true;
 if(typeof menu.showPopover==='function'){try{menu.showPopover()}catch(error){menu.hidden=false}}
 else menu.hidden=false;
 button?.setAttribute('aria-expanded','true')
}
function wsHideReasoningMenu(menu,button){
 if(!menu)return;
 menu._reasoningOpen=false;
 if(typeof menu.hidePopover==='function'){try{menu.hidePopover()}catch(error){menu.hidden=true}}
 else menu.hidden=true;
 button?.setAttribute('aria-expanded','false')
}
function wsBuildReasoningMenu(){
 const menu=$('#chat-reasoning-menu'),spec=WS.reasoningSpec;if(!menu||!spec)return;
 menu.replaceChildren();const add=(value,label,title)=>{const button=el('button','reasoning-option'+(WS.reasoning===value?' active':''));button.type='button';button.setAttribute('role','option');button.setAttribute('aria-selected',String(WS.reasoning===value));button.textContent=label;if(title)button.title=title;button.onclick=()=>{WS.reasoning=value;wsHideReasoningMenu(menu,$('#chat-reasoning'));wsUpdateRecipient();$('#chat-input')?.focus()};menu.append(button)};
 add(null,'По умолчанию',spec.default?'Уровень модели по умолчанию: '+spec.default:'');
 for(const level of spec.levels)add(level,wsReasoningLabel(level)+' · '+level,level)
}
function wsSyncReasoning(role,data){
 const key=[WS.roleId||'',role?.agent_id||'',role?.profile||'',role?.model||''].join('|');
 if(WS.reasoningKey!==key){WS.reasoningKey=key;WS.reasoning=null}
 const button=$('#chat-reasoning'),menu=$('#chat-reasoning-menu');if(!button||!menu)return;
 if(menu._reasoningOpen===undefined)menu._reasoningOpen=false;
 const spec=wsReasoningSpec(data,role);WS.reasoningSpec=spec;
 if(!spec){button.hidden=true;wsHideReasoningMenu(menu,button);return}
 button.hidden=false;button.textContent=wsReasoningText();button.setAttribute('aria-expanded',String(wsReasoningMenuOpen(menu)));
 if(wsReasoningMenuOpen(menu))wsBuildReasoningMenu()
}
function wsUpdateRecipient(){const saved=selectedAssignment();const role=WS.dirty?{...saved,agent_id:$('#role-agent').value,profile:$('#role-profile').value,model:$('#role-model').value}:saved;$('#chat-role').textContent=roleInfo(WS.roleId)[0]+' · '+agentName(role.agent_id)+(role.model?' · '+role.model:'');$('#chat-target').value=WS.roleId;const data=WS.profiles.get(role.agent_id),caps=wsModelCapabilities(data,role),selectedImage=WS.atts.some(item=>WS.selectedAtts.has(item.id)&&wsIsImageAttachment(item)),latestImage=wsLatestConversationImage(),supportsImage=!!data&&caps.input.includes('image'),requiresImage=!!data&&caps.requiresImage;const routeNotes=[];if(supportsImage&&selectedImage)routeNotes.push('Новое изображение заменит предыдущее в этом чате.');else if(requiresImage&&latestImage)routeNotes.push('Будет повторно использовано изображение из этого чата: '+(latestImage.filename||'предыдущее изображение')+'.');else if(requiresImage)routeNotes.push('Для первого распознавания в этом чате добавьте PNG/JPEG.');if(!supportsImage&&(selectedImage||latestImage))routeNotes.push('Изображения не передаются текстовому маршруту; ответы распознавания остаются в истории.');if(supportsImage&&!requiresImage&&!selectedImage)routeNotes.push('Изображение можно приложить к этой модели по желанию.');if(caps.contextMode)routeNotes.push(caps.contextMode);if(caps.filesystemAccess===false&&!/файл|file/i.test(caps.contextMode))routeNotes.push('Без доступа к файлам проекта и файловых инструментов.');if(agents.find(item=>item.id===role.agent_id)?.transport!=='http'&&caps.supportsWorkdir===false)routeNotes.push('Этот канал пока не запускает задачи в папке проекта.');$('#chat-route-hint').textContent=[...new Set(routeNotes)].join(' ');$('#chat-send').disabled=WS.sending||!WS.project||(requiresImage&&!selectedImage&&!latestImage);wsSyncReasoning(role,data)}
function renderRoles(){const box=$('#roles-list');box.replaceChildren();const target=$('#chat-target');const roleEntries=Object.entries(ROLE_META).sort(([a],[b])=>a==='orchestrator'?-1:b==='orchestrator'?1:0);putOptions(target,roleEntries.map(([id,meta])=>({id,name:meta[0]})),WS.roleId);for(const [id,meta] of roleEntries){const role=WS.roles[id]||{agent_id:'codex'},selected=WS.roleId===id,button=el('button','role-card role-'+id+(selected?' active':''));button.type='button';button.dataset.role=id;button.setAttribute('aria-pressed',String(selected));button.setAttribute('aria-label',meta[0]+' · '+(selected?'выбранный получатель':'назначение'));button.append(el('span','role-avatar',meta[1]));const text=el('span','role-text');text.append(el('span','role-name',meta[0]),el('span','role-meta',agentName(role.agent_id)+(role.model?' · '+role.model:'')));button.append(text,el('span','role-chevron','›'));button.onclick=action(()=>wsEditRole(id));box.append(button)}wsUpdateRecipient()}
async function wsLoadRoleQuota(agentId,profileId,modelId){
 const box=$('#role-quota');if(!box)return;
 if(!WS.project){box.textContent='Проект не выбран — расход не с чем сверять.';return}
 try{
  const data=await api('/projects/'+encodeURIComponent(WS.project.id)+'/usage?days=30');
  const rows=(data.items||[]).filter(item=>item.agent_id===agentId&&(!modelId||item.model===modelId));
  if(!rows.length||!data.has_usage){box.textContent='Токенов за 30 дней нет: канал не сообщает usage.';return}
  const sum=rows.reduce((acc,item)=>{acc.total+=item.tokens_total;acc.input+=item.tokens_in;acc.output+=item.tokens_out;acc.reason+=item.tokens_reasoning;acc.attempts+=item.attempts;return acc},{total:0,input:0,output:0,reason:0,attempts:0});
  const n=value=>Number(value||0).toLocaleString('ru-RU');
  box.textContent='Токены за 30 дней: '+n(sum.total)+' (вход '+n(sum.input)+', выход '+n(sum.output)+', размышление '+n(sum.reason)+') · попыток '+n(sum.attempts);
 }catch(error){box.textContent='Расход недоступен: '+error.message}
}
async function wsLoadJobs(){
 const box=$('#jobs-list');if(!box)return;
 if(!WS.project){box.replaceChildren(el('p','hint','Проект не выбран.'));return}
 try{
  const data=await api('/projects/'+encodeURIComponent(WS.project.id)+'/jobs');
  box.replaceChildren();
  if(!data.jobs.length){box.append(el('p','hint','Задач по расписанию нет. «Добавить» создаст архивариуса на 05:00 МСК.'));return}
  for(const job of data.jobs){
   const card=el('div','job-card');
   card.append(el('strong','',job.name));
   const route=job.fallback_agent_id?job.agent_id+' → '+job.fallback_agent_id+(job.fallback_used?' (резерв уже использован)':''):job.agent_id;
   card.append(el('p','hint',[route,(job.model||'модель по настройкам')+(job.fallback_model?' / '+job.fallback_model:''),job.schedule+' МСК'].join(' · ')));
   const last=job.last_run?('Последний запуск: '+new Date(job.last_run.started_at).toLocaleString('ru-RU')+' · '+job.last_run.status):'Ещё не запускался';
   card.append(el('p','hint',last));
   const details=el('details','role-instructions');details.append(el('summary','','Промпт'));
   const text=el('p','hint');text.textContent=job.prompt;details.append(text);card.append(details);
   const run=el('button','ghost','Запустить сейчас');run.type='button';
   run.onclick=action(async()=>{await api('/projects/'+encodeURIComponent(WS.project.id)+'/jobs/'+job.id+'/run',{method:'POST'});toast('Архивариус поставлен в очередь');await wsLoadJobs()});
   card.append(run);box.append(card)
  }
 }catch(error){box.replaceChildren(el('p','hint','Не удалось получить задачи: '+error.message))}
}
async function wsCreateJob(){
 if(!WS.project)return;
 const body={name:'Архивариус',kind:'archivist',agent_id:'hermes-server',profile:'router-cheap',model:'deepseek-v4.1-flash-1017',schedule:'0 5 * * *',fallback_agent_id:'hermes-server',fallback_profile:'router-cheap',fallback_model:'qwen3.8-27b'};
 await api('/projects/'+encodeURIComponent(WS.project.id)+'/jobs',{method:'POST',body:JSON.stringify(body)});
 toast('Задача архивариуса добавлена: 05:00 МСК');
 await wsLoadJobs()
}
async function wsGetProfiles(agentId){if(!WS.profiles.has(agentId))WS.profiles.set(agentId,await api('/agent-profiles/'+encodeURIComponent(agentId)));return WS.profiles.get(agentId)}
async function wsEditRole(id){WS.roleId=id;WS.dirty=false;renderRoles();const role=selectedAssignment();const meta=roleInfo(id);$('#role-editor-title').textContent=meta[0];$('#role-avatar').textContent=meta[1];$('#role-description').textContent=meta[2];$('#role-instructions').value=role.instructions||'';$('#role-save-status').textContent='';putOptions($('#role-agent'),agents.filter(a=>a.enabled!==false).map(a=>({id:a.id,name:a.name+' · '+(a.host==='server'?'сервер':'ноутбук')})),role.agent_id||'codex');await wsPopulateRoleProfiles($('#role-agent').value,role.profile,role.model);wsUpdateRecipient()}
function wsSetRoleProfilesLoading(loading){WS.editorProfilesLoading=loading;$('#role-save').disabled=loading;$('#role-save-status').textContent=loading?'Загружаю провайдеры и модели…':WS.dirty?'Есть несохранённые настройки':''}
async function wsPopulateRoleProfiles(agentId,profileId,modelId){const gen=++WS.editorGen;wsSetRoleProfilesLoading(true);$('#role-profile').disabled=true;$('#role-model').disabled=true;try{const data=await wsGetProfiles(agentId);if(gen!==WS.editorGen)return;const agent=agents.find(a=>a.id===agentId)||{};const supported=!!(data.model_selection??agent.model_selection);const profiles=data.profiles||[];putOptions($('#role-profile'),profiles,profileId||data.default_profile);wsPopulateRoleModels(data,$('#role-profile').value,modelId);$('#role-profile').disabled=!supported;$('#role-model').disabled=!supported;$('#role-capability').textContent=wsUpdateRoleCapability(agentId,$('#role-profile').value,$('#role-model').value);$('#role-quota').textContent='Считаем токены за 30 дней…';wsUpdateRecipient();wsLoadRoleQuota(agentId,$('#role-profile').value,$('#role-model').value)}catch(error){if(gen===WS.editorGen){putOptions($('#role-profile'),[{id:'',name:'Каталог недоступен'}],'');putOptions($('#role-model'),[{id:'',name:'Каталог недоступен'}],'');$('#role-capability').textContent=error.message;throw error}}finally{if(gen===WS.editorGen)wsSetRoleProfilesLoading(false)}}
function wsPopulateRoleModels(data,profileId,modelId){const profile=(data.profiles||[]).find(p=>p.id===profileId);putOptions($('#role-model'),(profile?.models||[]).map(m=>({id:typeof m==='string'?m:m.id,name:typeof m==='string'?m:m.name||m.id})),modelId);if(!$('#role-model').options.length)putOptions($('#role-model'),[{id:'',name:'По настройкам агента'}],'')}
async function wsSaveRole(snapshot=null){
 if(WS.editorProfilesLoading)throw Error('Дождитесь загрузки провайдеров и моделей перед сохранением.');
 if(!snapshot&&!WS.project)throw Error('Выберите проект.');
 const projectId=snapshot?.projectId||WS.project.id,roleId=snapshot?.roleId||WS.roleId;
 const draft=snapshot?.draft||{agent_id:$('#role-agent').value,instructions:$('#role-instructions').value,profile:$('#role-profile').value,model:$('#role-model').value};
 const agentId=draft.agent_id,instructions=draft.instructions,profile=draft.profile,model=draft.model;
 const data=await wsGetProfiles(agentId),agent=agents.find(a=>a.id===agentId)||{};
 const supported=!!(data.model_selection??agent.model_selection);
 const payload={agent_id:agentId,instructions,profile:supported?profile:null,model:supported?model:null};
 const saved=await api('/projects/'+projectId+'/roles/'+roleId,{method:'PATCH',body:JSON.stringify(payload)});
 if(WS.project?.id===projectId)WS.roles[roleId]=saved;
 if(WS.project?.id===projectId&&WS.roleId===roleId){WS.dirty=false;renderRoles();$('#role-save-status').textContent='Назначение сохранено';$('#role-save').textContent='Сохранить назначение';$('#workspace-error').hidden=true}
 return saved;
}

async function wsSend(text){
 if(!WS.project)throw Error('Создайте или выберите проект.');
 if(WS.selectedAtts.size>5)throw Error('Выберите не более 5 файлов к задаче.');
 const projectId=WS.project.id,roleId=WS.roleId,wasDirty=WS.dirty,selected={...selectedAssignment()};
 const roleDraft={agent_id:$('#role-agent').value,instructions:$('#role-instructions').value,profile:$('#role-profile').value,model:$('#role-model').value};
 const selectedAttachments=WS.atts.filter(item=>WS.selectedAtts.has(item.id));const selectedImages=selectedAttachments.filter(wsIsImageAttachment);if(selectedImages.length>1)throw Error('К задаче можно приложить только одно новое изображение PNG/JPEG.');const previousConversationImage=wsLatestConversationImage();
 let conversationId=WS.conv?.id;
 const savedAssignment=wasDirty?await wsSaveRole({projectId,roleId,draft:roleDraft}):selected;
 const assignment={...savedAssignment};
 const data=await wsGetProfiles(assignment.agent_id),agent=agents.find(a=>a.id===assignment.agent_id)||{};
 const reasoningSpec=wsReasoningSpec(data,assignment),reasoningEffort=reasoningSpec&&WS.reasoning&&reasoningSpec.levels.includes(WS.reasoning)?WS.reasoning:null;
 const capabilities=wsModelCapabilities(data,assignment);
 const supportsImage=capabilities.input.includes('image');
 if(capabilities.requiresImage&&!selectedImages.length&&!previousConversationImage)throw Error('Для этой модели нужно PNG/JPEG. Прикрепите изображение или продолжите чат, где оно уже распознавалось.');
 const omittedImages=supportsImage?[]:selectedImages;let attachments=selectedAttachments.filter(item=>!wsIsImageAttachment(item)||supportsImage).map(item=>item.id);
 if(!conversationId){const conversation=await api('/projects/'+projectId+'/conversations',{method:'POST',body:JSON.stringify({role_id:roleId,title:text.slice(0,65)})});conversationId=conversation.id;if(WS.project?.id===projectId&&!WS.conv){WS.convs.unshift(conversation);WS.conv=conversation;storeSet(conversationStorageKey(),conversation.id);wsRenderConversations()}}
 const fingerprint=JSON.stringify({project:projectId,conversation:conversationId,role:roleId,text,agent:assignment.agent_id,profile:assignment.profile,model:assignment.model,reasoning:reasoningEffort,attachments:[...attachments].sort()});
 const activePending=WS.project?.id===projectId?WS.pending:null;
 const pending=activePending?.fingerprint===fingerprint?activePending:{fingerprint,key:newKey()};
 if(WS.project?.id===projectId)WS.pending=pending;
 const payload={project_id:projectId,text,role_id:roleId,agent_id:assignment.agent_id,timeout_s:assignment.timeout_s||600,attachments,idempotency_key:pending.key};
 if(data.model_selection??agent.model_selection){if(assignment.profile)payload.profile=assignment.profile;if(assignment.model)payload.model=assignment.model}
 if(reasoningEffort)payload.reasoning_effort=reasoningEffort;
 const response=await api('/conversations/'+conversationId+'/send',{method:'POST',body:JSON.stringify(payload),timeout:20000});
 const warnings=Array.isArray(response.context_warnings)?[...response.context_warnings]:[];
 if(omittedImages.length)warnings.push('Выбранное изображение не передано текстовому маршруту; ответы OCR остаются в истории чата.');
 const activeImage=response.active_image||(response.active_image_id?{id:response.active_image_id,filename:response.active_image_filename,source:response.active_image_source,reused:response.active_image_reused}:null);
 if(activeImage?.reused&&!warnings.some(note=>activeImage.filename&&note.includes(activeImage.filename)))warnings.push('Повторно использовано изображение из этого чата: '+(activeImage.filename||'предыдущее изображение')+'.');
 else if(activeImage?.filename&&!warnings.some(note=>note.includes(activeImage.filename)))warnings.push('Использовано новое изображение: '+activeImage.filename+'.');
 if(activeImage){response.active_image=activeImage;if(!WS.activeImageByConversation)WS.activeImageByConversation=new Map();WS.activeImageByConversation.set(conversationId,activeImage)}response.context_warnings=warnings;
 if(WS.project?.id===projectId&&WS.pending?.fingerprint===fingerprint)WS.pending=null;
 if(WS.project?.id===projectId&&typeof response?.conversation_title==='string'&&response.conversation_title.trim()){
  const conversation=WS.convs.find(item=>item.id===conversationId);
  if(conversation)conversation.title=response.conversation_title;
  if(WS.conv?.id===conversationId){WS.conv.title=response.conversation_title;$('#chat-title').textContent=response.conversation_title}
  wsRenderConversations()
 }
 if(WS.project?.id===projectId){for(const item of selectedAttachments)WS.selectedAtts.delete(item.id);wsRenderAttachments();if(WS.conv?.id===conversationId)await wsLoadMessages();wsStartPolling()}
 return response;
}
function wsStartPolling(){clearInterval(WS.pollTimer);WS.pollTimer=setInterval(()=>{if(token&&!document.hidden&&WS.view==='workspace'&&WS.conv)wsLoadMessages().catch(error=>{if(error.message!=='unauthorized')$('#chat-msg').textContent='Не удалось обновить ответ: '+error.message})},2500)}

const WS_ROOT='R:\\AIHub\\WorkSpace';
function wsProjectSlug(name){
 const slug=String(name||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,48);
 return slug||'project'
}
function wsProjectPath(){return WS_ROOT+'\\'+wsProjectSlug($('#project-name')?.value.trim()||'project')}
function wsProjectProgress(on){
 const box=$('#project-progress');if(!box)return;
 box.hidden=!on;
 if(on){box._step=0;wsProjectStep(0);clearInterval(box._timer);box._timer=setInterval(()=>wsProjectStep(Math.min(4,(box._step||0)+1)),900)}
 else clearInterval(box._timer)
}
function wsProjectStep(index){
 const box=$('#project-progress');if(!box)return;
 box._step=index;
 box.querySelectorAll('.ws-step').forEach((item,i)=>{item.dataset.state=i<index?'done':i===index?'active':''});
}
async function wsProjectDialog(create=false){
 WS.newProject=create;
 $('#project-dialog-title').textContent=create?'Новый проект':'Настройки проекта';
 $('#project-name').value=create?'':WS.project?.name||'';
 const path=create?wsProjectPath():(WS.project?.workdir||'');
 $('#project-path').textContent=path||'—';
 $('#project-progress').hidden=true;wsProjectProgress(false);
 $('#project-dialog-error').textContent='';
 $('#project-dialog').showModal();$('#project-name').focus()
}
async function wsSaveProject(event){
 if(event.submitter&&event.submitter.formMethod==='dialog')return;
 event.preventDefault();if(WS.projectSaving)return;
 const name=$('#project-name').value.trim();if(!name)return;
 const button=$('#project-settings-form').querySelector('button[type=submit]'),originalText=button.textContent;
 WS.projectSaving=true;button.disabled=true;button.textContent=WS.newProject?'Создаю…':'Сохраняю…';
 if(WS.newProject)wsProjectProgress(true);
 try{
  const payload={name};
  const project=await api(WS.newProject?'/projects':'/projects/'+WS.project.id,{method:WS.newProject?'POST':'PATCH',body:JSON.stringify(payload)});
  wsProjectStep(4);
  storeSet('aihub.project',project.id);
  await new Promise(resolve=>setTimeout(resolve,600));
  $('#project-dialog').close();WS.conv=null;await wsRefreshAll();
  toast(WS.newProject?'Проект создан: '+(project.workdir||name):'Проект сохранён')
 }catch(error){$('#project-dialog-error').textContent=error.message}
 finally{WS.projectSaving=false;button.disabled=false;button.textContent=originalText;wsProjectProgress(false)}
}
function wsWire(){
 runsWire();
 document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>switchView(tab.dataset.view)));
 $('#workspace-retry').onclick=action(wsRefreshAll);$('#conv-new').onclick=action(wsNewConversation);$('#project-new').onclick=()=>wsProjectDialog(true);$('#project-settings').onclick=()=>wsProjectDialog(!WS.project);$('#project-settings-form').onsubmit=wsSaveProject;
 $('#role-agent').onchange=action(async()=>{const roleId=WS.roleId,agentId=$('#role-agent').value;setDraftDirty();await wsPopulateRoleProfiles(agentId,'','');if(WS.roleId===roleId&&$('#role-agent').value===agentId)setDraftDirty()});$('#role-profile').onchange=action(async()=>{const roleId=WS.roleId,agentId=$('#role-agent').value,profileId=$('#role-profile').value;const data=await wsGetProfiles(agentId);if(WS.roleId!==roleId||$('#role-agent').value!==agentId||$('#role-profile').value!==profileId)return;wsPopulateRoleModels(data,profileId,'');$('#role-capability').textContent=wsUpdateRoleCapability(agentId,profileId,$('#role-model').value);setDraftDirty()});$('#role-model').onchange=()=>{$('#role-capability').textContent=wsUpdateRoleCapability($('#role-agent').value,$('#role-profile').value,$('#role-model').value);wsLoadRoleQuota($('#role-agent').value,$('#role-profile').value,$('#role-model').value);setDraftDirty()};$('#role-instructions').oninput=setDraftDirty;$('#role-save').onclick=action(async()=>{await wsSaveRole();toast('Настройки роли сохранены')});$('#job-add').onclick=action(wsCreateJob);$('#project-name').oninput=()=>{if(WS.newProject)$('#project-path').textContent=wsProjectPath()};$('#chat-target').onchange=action(()=>wsEditRole($('#chat-target').value));
 $('#att-file').onchange=action(async event=>{const files=Array.from(event.target.files||[]);try{await wsUpload(files)}finally{event.target.value=''}});
 const dropPanel=$('#context-panel');if(dropPanel){let dragDepth=0;dropPanel.addEventListener('dragenter',event=>{if(!event.dataTransfer?.types?.includes('Files'))return;event.preventDefault();dragDepth+=1;wsSetDropState(true)});dropPanel.addEventListener('dragover',event=>{if(!event.dataTransfer?.types?.includes('Files'))return;event.preventDefault();event.dataTransfer.dropEffect='copy';wsSetDropState(true)});dropPanel.addEventListener('dragleave',event=>{if(!event.dataTransfer?.types?.includes('Files'))return;event.preventDefault();dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)wsSetDropState(false)});dropPanel.addEventListener('drop',event=>{if(!event.dataTransfer?.files?.length)return;event.preventDefault();dragDepth=0;wsSetDropState(false);action(()=>wsUpload(Array.from(event.dataTransfer.files)))().catch(workspaceError)})}
 const filterBox=$('#att-filter');if(filterBox)filterBox.oninput=event=>{WS.attFilter=event.target.value||'';wsRenderAttachments()}; const clearButton=$('#att-clear');if(clearButton)clearButton.onclick=()=>{WS.selectedAtts.clear();wsRenderAttachments()};
 const convFilter=$('#conv-filter');if(convFilter)convFilter.oninput=event=>{WS.convFilter=event.target.value||'';WS.convSeq++;WS.convCursor='';WS.convHasMore=false;WS.convLoading=true;WS.convs=[];delete WS.convSignature;wsRenderConversations();clearTimeout(WS.convFilterTimer);WS.convFilterTimer=setTimeout(()=>{wsLoadConversations({reset:true}).catch(workspaceError)},260)};const convMore=$('#conv-more');if(convMore)convMore.onclick=()=>wsLoadMoreConversations().catch(workspaceError);
 $('#chat-reasoning').onclick=event=>{event.stopPropagation();const menu=$('#chat-reasoning-menu'),button=$('#chat-reasoning');if(!WS.reasoningSpec){wsHideReasoningMenu(menu,button);return}if(wsReasoningMenuOpen(menu)){wsHideReasoningMenu(menu,button)}else{wsBuildReasoningMenu();wsShowReasoningMenu(menu,button)}};
 window.addEventListener('resize',()=>{const menu=$('#chat-reasoning-menu'),button=$('#chat-reasoning');if(wsReasoningMenuOpen(menu))wsPositionReasoningMenu(menu,button)});
 document.addEventListener('click',event=>{const menu=$('#chat-reasoning-menu'),button=$('#chat-reasoning');if(!wsReasoningMenuOpen(menu))return;if(button&&button.contains(event.target))return;if(menu.contains(event.target))return;wsHideReasoningMenu(menu,button)});
 $('#chat-form').onsubmit=async event=>{event.preventDefault();const input=$('#chat-input'),draft=input.value,text=draft.trim(),submittedProjectId=WS.project?.id,submittedConversationId=WS.conv?.id;if(!text||WS.sending)return;WS.sending=true;wsUpdateRecipient();$('#chat-msg').className='msg';$('#chat-msg').textContent='Передаю задачу…';try{const response=await wsSend(text),stillInSubmittedChat=WS.project?.id===submittedProjectId&&(!submittedConversationId||WS.conv?.id===submittedConversationId);if(stillInSubmittedChat&&input.value===draft)input.value='';if(stillInSubmittedChat){const warnings=response?.context_warnings||[];if(warnings.length){$('#chat-msg').className='msg warn-text';$('#chat-msg').textContent='Задача принята. Контекст: '+warnings.join(' ')}else{$('#chat-msg').className='msg ok';$('#chat-msg').textContent='Задача принята. Ответ появится в этом чате.'}}}catch(error){if(WS.project?.id===submittedProjectId&&(!submittedConversationId||WS.conv?.id===submittedConversationId)){$('#chat-msg').className='msg error';$('#chat-msg').textContent=error.message;workspaceError(error)}}finally{WS.sending=false;wsUpdateRecipient()}};
 $('#chat-input').onkeydown=event=>{if(event.isComposing||event.keyCode===229)return;if(event.key!=='Enter'||event.shiftKey)return;event.preventDefault();if(event.repeat)return;$('#chat-form').requestSubmit()};$('#token-form').onsubmit=event=>{event.preventDefault();saveToken().catch(workspaceError)};
}

function stopPolling(){pollTimers.forEach(clearInterval);pollTimers=[];clearInterval(WS.pollTimer)}
function startPolling(){pollTimers.forEach(clearInterval);pollTimers=[setInterval(()=>health(),15000),setInterval(()=>{if(token&&!document.hidden&&WS.view==='ops')loadJobs()},5000),setInterval(()=>{if(token&&!document.hidden&&WS.view==='ops'){loadRuns();if(currentRunId)renderRunDetail()}},5000),setInterval(()=>{if(token&&!document.hidden&&WS.view==='ops')loadAgents().catch(error=>{if(error.message!=='unauthorized')$('#health-text').textContent='ошибка наблюдений: '+error.message})},30000)];wsStartPolling()}

async function boot(){
 await loadAgents();await Promise.allSettled([health(),loadJobs()]);
 // Панель сверки живёт во вкладке наблюдений, но счётчик открытых исключений
 // нужен сразу: иначе раздел показывает заглушку «загрузка» до переключения вкладки.
 reconLoad(false).catch(()=>{});
 try{await wsRefreshAll()}catch(error){workspaceError(error)}
 startPolling();
}
wireGate();wsWire();wireReconciliation();$('#build').textContent=BUILD;
if(token)boot().catch(workspaceError);else openGate('');
