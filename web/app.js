// AI-Hub UI: реестр агентов, запуск заданий, история, детали, отмена.
// Без сборки и зависимостей — обычные fetch + DOM.

const API = window.location.pathname.startsWith('/ui') ? '' : '';
const TOKEN_KEY = 'aihub_token';
const BUILD = 'ui14';
function storeGet(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } }
function storeSet(k, v) { try { localStorage.setItem(k, v); return true; } catch (e) { return false; } }
function storeDel(k) { try { localStorage.removeItem(k); } catch (e) { /* заблокировано */ } }
let token = storeGet(TOKEN_KEY);
let currentJob = null;
let currentStatus = '';
let agents = [];
let agentNames = new Map();
let pollTimers = [];
let detailGen = 0;
let pendingRun = null;
let jobsLoaded = 0;
let jobsTotal = 0;

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
  if (resp.status === 401) { openGate('Токен не подошёл — введи заново.'); throw new Error('unauthorized'); }
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!resp.ok) {
    const detail = data && (data.detail || data.error) ? (data.detail || data.error) : resp.status;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

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
  if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); saveToken(); } });
}

// ---------- здоровье ----------
async function health() {
  try {
    const h = await api('/health');
    $('#health-dot').className = 'dot ' + (h.db === true ? 'ok' : 'bad');
    $('#health-text').textContent = h.db === true ? 'хаб и БД в норме' : 'БД: ' + h.db;
    $('#version').textContent = 'v' + h.version;
    if ($('#gate').hidden === false && token) closeGate();
  } catch (e) {
    $('#health-dot').className = 'dot bad';
    $('#health-text').textContent = 'хаб недоступен';
  }
}

// ---------- агенты ----------
async function loadAgents() {
  const data = await api('/agents');
  agents = data.agents || [];
  const list = $('#agents-list');
  const select = $('#run-agent');
  list.innerHTML = '';
  select.innerHTML = '';
  $('#agents-count').textContent = agents.length + ' в реестре';

  for (const a of agents) {
    const row = el('div', 'agent');
    row.dataset.id = a.id;
    row.append(el('span', 'id', a.id));
    row.append(el('span', 'status ' + (a.status || ''), AGENT_STATUS_RU[a.status] || a.status || '—'));
    row.append(el('span', 'transport', a.transport || ''));
    row.onclick = () => {
      document.querySelectorAll('.agent').forEach(n => n.classList.remove('active'));
      row.classList.add('active');
      select.value = a.id;
      if (!$('#run-prompt').value) $('#run-prompt').focus();
    };
    list.append(row);

    const opt = el('option', '', a.id + (a.status === 'verified' ? '' : ' (не проверен)'));
    opt.value = a.id;
    if (!a.bridge_agent) opt.disabled = true;
    select.append(opt);
  }
  agentNames = new Map();
  for (const a of agents) {
    agentNames.set(a.id, a.name || a.id);
    if (a.bridge_agent) agentNames.set(a.bridge_agent, a.name || a.bridge_agent);
  }
  if (agents.length) select.value = agents[0].id;
}

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
    const reuse = pendingRun && pendingRun.agent === agent
      && pendingRun.prompt === prompt && pendingRun.timeout === timeout;
    const key = reuse ? pendingRun.key : newKey();
    pendingRun = { agent, prompt, timeout, key };
    const body = { prompt, timeout, idempotency_key: key };
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
    loadJobs();
  };
});

const moreBtn = $('#more');
if (moreBtn) moreBtn.onclick = () => loadJobs(true);

async function loadJobs(more) {
  try {
    // Первая страница — 50; «показать ещё» догружает следующую пачку.
    // Обычный опрос перезапрашивает столько, сколько уже показано, чтобы список не схлопывался.
    const limit = more ? 50 : Math.min(200, Math.max(50, jobsLoaded));
    const offset = more ? jobsLoaded : 0;
    const q = '/jobs?limit=' + limit + '&offset=' + offset
      + (currentStatus ? '&status=' + encodeURIComponent(currentStatus) : '');
    const data = await api(q);
    const jobs = data.jobs || [];
    const body = $('#jobs-body');
    if (!more) body.innerHTML = '';
    $('#jobs-empty').hidden = jobs.length > 0;
    for (const j of jobs) {
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
      tr.append(el('td', 'prompt', j.prompt_preview || ''));
      tr.onclick = () => openJob(j.id);
      body.append(tr);
    }
    jobsLoaded = offset + jobs.length;
    jobsTotal = data.total ?? jobsLoaded;
    const moreBtnEl = $('#more');
    if (moreBtnEl) moreBtnEl.hidden = !(data.has_more || jobsLoaded < jobsTotal);
  } catch (e) {
    if (e.message !== 'unauthorized') $('#health-text').textContent = 'ошибка истории: ' + e.message;
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
    add('Агент', agentName(job.agent_id));
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

// ---------- старт ----------
function stopPolling() {
  pollTimers.forEach(t => clearInterval(t));
  pollTimers = [];
}

// Вызывается и при старте, и после входа по токену. Повторный вызов не плодит таймеры.
function startPolling() {
  stopPolling();
  pollTimers.push(setInterval(health, 15000));
  pollTimers.push(setInterval(() => { if (!document.hidden) loadJobs(); }, 5000));
}

async function boot() {
  // Секции грузим параллельно и не блокируем друг друга: зависший запрос не должен
  // оставлять страницу пустой.
  const names = ['/health', '/agents', '/jobs'];
  const results = await Promise.allSettled([health(), loadAgents(), loadJobs()]);
  const problems = results
    .map((r, i) => (r.status === 'rejected' && r.reason && r.reason.message !== 'unauthorized')
      ? names[i] + ': ' + r.reason.message : null)
    .filter(Boolean);
  if (problems.length) {
    const msg = problems.join('; ');
    $('#health-text').textContent = 'проблема с API — ' + msg;
    const st = $('#token-status');
    if (st) { st.className = 'msg error'; st.textContent = msg; }
  }
  startPolling();
}

(async () => {
  wireGate();
  const b = document.getElementById('build');
  if (b) b.textContent = BUILD;
  if (!token) { openGate(''); return; }
  await boot();
})();
