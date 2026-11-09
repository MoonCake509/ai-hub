// AI-Hub UI: реестр агентов, запуск заданий, история, детали, отмена.
// Без сборки и зависимостей — обычные fetch + DOM.

const API = window.location.pathname.startsWith('/ui') ? '' : '';
const TOKEN_KEY = 'aihub_token';
const BUILD = 'ui24';
function storeGet(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } }
function storeSet(k, v) { try { localStorage.setItem(k, v); return true; } catch (e) { return false; } }
function storeDel(k) { try { localStorage.removeItem(k); } catch (e) { /* заблокировано */ } }
let token = storeGet(TOKEN_KEY);
let currentJob = null;
let currentStatus = '';
let agents = [];
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
    // Auth gate closes only after a protected request succeeds.
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
  if (agents.length) {
    select.value = agents[0].id;
    await loadAgentProfiles(agents[0].id);
  }
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
    if (profiles.length) {
      profileSel.value = profiles[0].id;
      await loadProfileModels(agentId, profiles[0].id, profiles);
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
  architect: ['Архитектор','A','План, требования и технические решения'],
  coder: ['Кодер','C','Реализация и изменения в проекте'],
  tester: ['Тестер','T','Проверки и воспроизведение ошибок'],
  reviewer: ['Ревьюер','R','Проверка результата и замечания'],
  archivist: ['Архивариус','D','Документация и сохранение решений'],
};
const WS={projects:[],project:null,convs:[],conv:null,roles:{},atts:[],messages:[],selectedAtts:new Set(),roleId:'architect',profiles:new Map(),generation:0,editorGen:0,editorProfilesLoading:false,pollTimer:null,pending:null,dirty:false,sending:false,view:'workspace',newProject:false,previewUrls:new Map(),previewLoads:new Map(),previewGeneration:0,activeImageByConversation:new Map()};

function toast(message,error=false){const box=$('#toast');box.textContent=message;box.classList.toggle('error',error);box.hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>box.hidden=true,5500)}
function workspaceError(error){const text=String(error?.message||error);if(text==='unauthorized')return;$('#workspace-error-text').textContent=text.includes('404')||text==='Not Found'?'Сервер ещё не обновлён: API проектов недоступен. Обновите backend вместе с интерфейсом.':text;$('#workspace-error').hidden=false}
function action(fn){return async(...args)=>{try{return await fn(...args)}catch(error){workspaceError(error);toast(error.message||String(error),true)}}}
function switchView(name){WS.view=name;document.querySelectorAll('.tab').forEach(b=>{b.classList.toggle('active',b.dataset.view===name);b.setAttribute('aria-pressed',String(b.dataset.view===name))});$('#view-workspace').hidden=name!=='workspace';$('#view-ops').hidden=name!=='ops';$('#detail').hidden=true;currentJob=null;if(name==='workspace'&&token)wsRefreshAll().catch(workspaceError);if(name==='ops'&&token)loadJobs()}
function putOptions(select,items,value){select.replaceChildren();for(const item of items){const option=el('option','',item.name||item.id);option.value=item.id;select.append(option)}if(items.some(x=>x.id===value))select.value=value;else if(items.length)select.value=items[0].id}
function roleInfo(id){return ROLE_META[id]||[id,'?','']}
function selectedAssignment(){return WS.roles[WS.roleId]||{role_id:WS.roleId,agent_id:'codex',profile:'',model:'',instructions:''}}
function setDraftDirty(){WS.dirty=true;$('#role-save-status').textContent=WS.editorProfilesLoading?'Загружаю провайдеры и модели…':'Есть несохранённые настройки';$('#role-save').textContent='Сохранить назначение'}
function conversationStorageKey(){return 'aihub.conv.'+(WS.project?.id||'')}

async function wsRefreshAll(){
 const gen=++WS.generation;$('#workspace-error').hidden=true;
 const data=await api('/projects');if(gen!==WS.generation)return;
 WS.projects=data.projects||[];const saved=storeGet('aihub.project');const previous=WS.project?.id;
 WS.project=WS.projects.find(p=>p.id===saved)||WS.projects[0]||null;
 wsRenderProjects();
 if(!WS.project){wsClearPreviewUrls();WS.roles={};WS.convs=[];WS.conv=null;WS.messages=[];renderMessages();renderRoles();$('#project-title').textContent='Создайте первый проект';$('#project-location').textContent='Рабочая папка, чаты и назначения в одном месте';$('#conv-new').disabled=true;return}
 $('#conv-new').disabled=false;storeSet('aihub.project',WS.project.id);
 const projectId=WS.project.id;
 const [project,convs,attachments]=await Promise.all([api('/projects/'+projectId),api('/projects/'+projectId+'/conversations'),api('/projects/'+projectId+'/attachments')]);
 if(gen!==WS.generation||WS.project?.id!==projectId)return;
 WS.project=project;WS.roles=Object.fromEntries((project.roles||[]).map(r=>[r.role_id,r]));WS.convs=convs.conversations||[];if(previous!==projectId){wsClearPreviewUrls();WS.selectedAtts.clear()}WS.atts=attachments.attachments||[];
 const savedConv=storeGet(conversationStorageKey());WS.conv=WS.convs.find(c=>c.id===savedConv)||WS.convs[0]||null;
 if(WS.conv?.role_id&&ROLE_META[WS.conv.role_id])WS.roleId=WS.conv.role_id;
 $('#project-title').textContent=project.name;$('#project-location').textContent=(project.host_id==='server'?'Сервер':'Ноутбук')+' · '+(project.workdir||'Рабочая папка не указана');
 wsRenderProjects();wsRenderConversations();wsRenderAttachments();renderRoles();await wsEditRole(WS.roleId);await wsLoadMessages();wsStartPolling();
}

function wsRenderProjects(){const box=$('#projects-list');box.replaceChildren();for(const project of WS.projects){const button=el('button','proj'+(project.id===WS.project?.id?' active':''));button.type='button';button.setAttribute('aria-pressed',String(project.id===WS.project?.id));button.append(el('span','project-icon',project.name.slice(0,1).toUpperCase()),el('span','project-text',project.name));button.onclick=action(async()=>{if(WS.project?.id===project.id)return;storeSet('aihub.project',project.id);WS.conv=null;WS.messages=[];WS.selectedAtts.clear();WS.pending=null;await wsRefreshAll()});box.append(button)}if(!WS.projects.length)box.append(el('p','sidebar-empty','Добавьте проект, чтобы начать.'))}
function wsRenderConversations(){const box=$('#convs-list');box.replaceChildren();for(const conversation of WS.convs){const button=el('button','conv'+(conversation.id===WS.conv?.id?' active':''));button.type='button';button.append(el('span','chat-glyph','◇'),el('span','conv-text',conversation.title||'Новый чат'));button.onclick=action(()=>wsOpenConversation(conversation));box.append(button)}if(!WS.convs.length)box.append(el('p','sidebar-empty','Здесь появятся разговоры проекта.'))}
async function wsOpenConversation(conversation){WS.conv=conversation;storeSet(conversationStorageKey(),conversation.id);WS.messages=[];WS.pending=null;if(conversation.role_id&&ROLE_META[conversation.role_id])WS.roleId=conversation.role_id;wsRenderConversations();renderRoles();await wsEditRole(WS.roleId);await wsLoadMessages();$('#chat-input').focus()}
async function wsNewConversation(){if(!WS.project)throw Error('Сначала создайте или выберите проект.');const button=$('#conv-new');button.disabled=true;try{const conversation=await api('/projects/'+WS.project.id+'/conversations',{method:'POST',body:JSON.stringify({role_id:WS.roleId,title:'Новый чат'})});WS.convs.unshift(conversation);await wsOpenConversation(conversation);toast('Новый чат создан')}finally{button.disabled=false}}

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
function wsRenderAttachments(){
 const box=$('#atts-list');box.replaceChildren();const live=new Set(WS.atts.filter(wsIsImageAttachment).map(item=>item.id));for(const id of WS.previewUrls.keys())if(!live.has(id))wsRevokePreview(id);
 for(const attachment of WS.atts){
  const label=el('label','attachment'+(wsIsImageAttachment(attachment)?' attachment-image':''));const input=document.createElement('input');input.type='checkbox';input.checked=WS.selectedAtts.has(attachment.id);input.setAttribute('aria-label','Включить файл '+attachment.filename);input.onchange=()=>{if(input.checked&&wsIsImageAttachment(attachment)){for(const old of WS.atts)if(old.id!==attachment.id&&wsIsImageAttachment(old))WS.selectedAtts.delete(old.id)}if(input.checked&&!WS.selectedAtts.has(attachment.id)&&WS.selectedAtts.size>=5){input.checked=false;$('#chat-msg').className='msg warn-text';$('#chat-msg').textContent='К задаче можно выбрать не более 5 файлов.';return}input.checked?WS.selectedAtts.add(attachment.id):WS.selectedAtts.delete(attachment.id);wsRenderAttachments()};
  const text=el('span','attachment-name',attachment.filename);text.append(el('small','',attachment.origin==='host-file'?'Файл проекта':wsIsImageAttachment(attachment)?'Загруженное изображение':'Текстовый контекст'));
  label.append(input,text);
  if(wsIsImageAttachment(attachment)){const image=document.createElement('img');image.className='attachment-preview';image.alt='Предпросмотр '+attachment.filename;image.loading='lazy';const status=el('small','preview-status','Загружаю предпросмотр…');label.append(image,status);if(attachment.metadata?.width&&attachment.metadata?.height)label.append(el('small','attachment-dimensions',attachment.metadata.width+' × '+attachment.metadata.height+' px'));wsPreviewImage(attachment,image,status)}
  box.append(label)
 }
 if(!WS.atts.length)box.append(el('p','sidebar-empty','Добавьте текстовый файл или PNG/JPEG.'));wsContextHint()
}
function wsFilesCount(n){const lastTwo=n%100,last=n%10;const word=lastTwo>=11&&lastTwo<=14?'файлов':last===1?'файл':last>=2&&last<=4?'файла':'файлов';return n+' '+word}
function wsContextHint(){const selected=WS.atts.filter(a=>WS.selectedAtts.has(a.id));$('#chat-ctx').textContent=selected.length?wsFilesCount(selected.length):'';const box=$('#selected-context');box.replaceChildren();for(const attachment of selected)box.append(el('span','context-tag',attachment.filename));if(selected.length){const clear=el('button','clear-context','Снять выбор файлов');clear.type='button';clear.onclick=()=>{WS.selectedAtts.clear();wsRenderAttachments()};box.append(clear)}wsUpdateRecipient()}
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

async function wsLoadMessages(){if(!WS.conv){WS.messages=[];renderMessages();return}const id=WS.conv.id,gen=WS.generation;const data=await api('/conversations/'+id+'/messages?limit=200');if(WS.conv?.id!==id||gen!==WS.generation)return;WS.messages=data.messages||[];renderMessages()}
function renderMessages(){
 const box=$('#chat-log');const nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<100;box.replaceChildren();$('#chat-title').textContent=WS.conv?.title||'Новый чат';
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
 wsUpdateRecipient();
}
function wsUpdateRecipient(){const saved=selectedAssignment();const role=WS.dirty?{...saved,agent_id:$('#role-agent').value,profile:$('#role-profile').value,model:$('#role-model').value}:saved;$('#chat-role').textContent=roleInfo(WS.roleId)[0]+' · '+agentName(role.agent_id)+(role.model?' · '+role.model:'');$('#chat-target').value=WS.roleId;const data=WS.profiles.get(role.agent_id),caps=wsModelCapabilities(data,role),selectedImage=WS.atts.some(item=>WS.selectedAtts.has(item.id)&&wsIsImageAttachment(item)),latestImage=wsLatestConversationImage(),supportsImage=!!data&&caps.input.includes('image'),requiresImage=!!data&&caps.requiresImage;const routeNotes=[];if(supportsImage&&selectedImage)routeNotes.push('Новое изображение заменит предыдущее в этом чате.');else if(requiresImage&&latestImage)routeNotes.push('Будет повторно использовано изображение из этого чата: '+(latestImage.filename||'предыдущее изображение')+'.');else if(requiresImage)routeNotes.push('Для первого распознавания в этом чате добавьте PNG/JPEG.');if(!supportsImage&&(selectedImage||latestImage))routeNotes.push('Изображения не передаются текстовому маршруту; ответы распознавания остаются в истории.');if(supportsImage&&!requiresImage&&!selectedImage)routeNotes.push('Изображение можно приложить к этой модели по желанию.');if(caps.contextMode)routeNotes.push(caps.contextMode);if(caps.filesystemAccess===false&&!/файл|file/i.test(caps.contextMode))routeNotes.push('Без доступа к файлам проекта и файловых инструментов.');if(agents.find(item=>item.id===role.agent_id)?.transport!=='http'&&caps.supportsWorkdir===false)routeNotes.push('Этот канал пока не запускает задачи в папке проекта.');$('#chat-route-hint').textContent=[...new Set(routeNotes)].join(' ');$('#chat-send').disabled=WS.sending||!WS.project||(requiresImage&&!selectedImage&&!latestImage)}
function renderRoles(){const box=$('#roles-list');box.replaceChildren();const target=$('#chat-target');putOptions(target,Object.entries(ROLE_META).map(([id,meta])=>({id,name:meta[0]})),WS.roleId);for(const [id,meta] of Object.entries(ROLE_META)){const role=WS.roles[id]||{agent_id:'codex'},button=el('button','role-card'+(WS.roleId===id?' active':''));button.type='button';button.setAttribute('aria-pressed',String(WS.roleId===id));button.append(el('span','role-avatar',meta[1]));const text=el('span','role-text');text.append(el('span','role-name',meta[0]),el('span','role-meta',agentName(role.agent_id)+(role.model?' · '+role.model:'')));button.append(text,el('span','role-chevron','›'));button.onclick=action(()=>wsEditRole(id));box.append(button)}wsUpdateRecipient()}
async function wsGetProfiles(agentId){if(!WS.profiles.has(agentId))WS.profiles.set(agentId,await api('/agent-profiles/'+encodeURIComponent(agentId)));return WS.profiles.get(agentId)}
async function wsEditRole(id){WS.roleId=id;WS.dirty=false;renderRoles();const role=selectedAssignment();const meta=roleInfo(id);$('#role-editor-title').textContent=meta[0];$('#role-avatar').textContent=meta[1];$('#role-description').textContent=meta[2];$('#role-instructions').value=role.instructions||'';$('#role-save-status').textContent='';putOptions($('#role-agent'),agents.map(a=>({id:a.id,name:a.name+' · '+(a.host==='server'?'сервер':'ноутбук')})),role.agent_id||'codex');await wsPopulateRoleProfiles($('#role-agent').value,role.profile,role.model);wsUpdateRecipient()}
function wsSetRoleProfilesLoading(loading){WS.editorProfilesLoading=loading;$('#role-save').disabled=loading;$('#role-save-status').textContent=loading?'Загружаю провайдеры и модели…':WS.dirty?'Есть несохранённые настройки':''}
async function wsPopulateRoleProfiles(agentId,profileId,modelId){const gen=++WS.editorGen;wsSetRoleProfilesLoading(true);$('#role-profile').disabled=true;$('#role-model').disabled=true;try{const data=await wsGetProfiles(agentId);if(gen!==WS.editorGen)return;const agent=agents.find(a=>a.id===agentId)||{};const supported=!!(data.model_selection??agent.model_selection);const profiles=data.profiles||[];putOptions($('#role-profile'),profiles,profileId||data.default_profile);wsPopulateRoleModels(data,$('#role-profile').value,modelId);$('#role-profile').disabled=!supported;$('#role-model').disabled=!supported;$('#role-capability').textContent=wsUpdateRoleCapability(agentId,$('#role-profile').value,$('#role-model').value);$('#role-quota').textContent=agent.transport==='http'?'Локальный HTTP-маршрут · денежный баланс не сообщается.':'Провайдер не сообщает данные баланса или лимитов.';wsUpdateRecipient()}catch(error){if(gen===WS.editorGen){putOptions($('#role-profile'),[{id:'',name:'Каталог недоступен'}],'');putOptions($('#role-model'),[{id:'',name:'Каталог недоступен'}],'');$('#role-capability').textContent=error.message;throw error}}finally{if(gen===WS.editorGen)wsSetRoleProfilesLoading(false)}}
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
 const capabilities=wsModelCapabilities(data,assignment);
 const supportsImage=capabilities.input.includes('image');
 if(capabilities.requiresImage&&!selectedImages.length&&!previousConversationImage)throw Error('Для этой модели нужно PNG/JPEG. Прикрепите изображение или продолжите чат, где оно уже распознавалось.');
 const omittedImages=supportsImage?[]:selectedImages;let attachments=selectedAttachments.filter(item=>!wsIsImageAttachment(item)||supportsImage).map(item=>item.id);
 if(!conversationId){const conversation=await api('/projects/'+projectId+'/conversations',{method:'POST',body:JSON.stringify({role_id:roleId,title:text.slice(0,65)})});conversationId=conversation.id;if(WS.project?.id===projectId&&!WS.conv){WS.convs.unshift(conversation);WS.conv=conversation;storeSet(conversationStorageKey(),conversation.id);wsRenderConversations()}}
 const fingerprint=JSON.stringify({project:projectId,conversation:conversationId,role:roleId,text,agent:assignment.agent_id,profile:assignment.profile,model:assignment.model,attachments:[...attachments].sort()});
 const activePending=WS.project?.id===projectId?WS.pending:null;
 const pending=activePending?.fingerprint===fingerprint?activePending:{fingerprint,key:newKey()};
 if(WS.project?.id===projectId)WS.pending=pending;
 const payload={project_id:projectId,text,role_id:roleId,agent_id:assignment.agent_id,timeout_s:assignment.timeout_s||600,attachments,idempotency_key:pending.key};
 if(data.model_selection??agent.model_selection){if(assignment.profile)payload.profile=assignment.profile;if(assignment.model)payload.model=assignment.model}
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

function wsProjectDialog(create=false){WS.newProject=create;$('#project-dialog-title').textContent=create?'Новый проект':'Настройки проекта';$('#project-name').value=create?'':WS.project?.name||'';$('#project-host').value=create?'laptop':WS.project?.host_id||'laptop';$('#project-workdir').value=create?'':WS.project?.workdir||'';$('#project-dialog-error').textContent='';$('#project-dialog').showModal();$('#project-name').focus()}
async function wsSaveProject(event){event.preventDefault();const payload={name:$('#project-name').value.trim(),host_id:$('#project-host').value,workdir:$('#project-workdir').value.trim()};if(!payload.name)return;try{const project=await api(WS.newProject?'/projects':'/projects/'+WS.project.id,{method:WS.newProject?'POST':'PATCH',body:JSON.stringify(payload)});storeSet('aihub.project',project.id);$('#project-dialog').close();WS.conv=null;await wsRefreshAll();toast('Проект сохранён')}catch(error){$('#project-dialog-error').textContent=error.message}}
function wsWire(){
 document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>switchView(tab.dataset.view)));
 $('#workspace-retry').onclick=action(wsRefreshAll);$('#conv-new').onclick=action(wsNewConversation);$('#project-new').onclick=()=>wsProjectDialog(true);$('#project-settings').onclick=()=>wsProjectDialog(!WS.project);$('#project-dialog-close').onclick=()=>$('#project-dialog').close();$('#project-settings-form').onsubmit=wsSaveProject;
 $('#role-agent').onchange=action(async()=>{const roleId=WS.roleId,agentId=$('#role-agent').value;setDraftDirty();await wsPopulateRoleProfiles(agentId,'','');if(WS.roleId===roleId&&$('#role-agent').value===agentId)setDraftDirty()});$('#role-profile').onchange=action(async()=>{const roleId=WS.roleId,agentId=$('#role-agent').value,profileId=$('#role-profile').value;const data=await wsGetProfiles(agentId);if(WS.roleId!==roleId||$('#role-agent').value!==agentId||$('#role-profile').value!==profileId)return;wsPopulateRoleModels(data,profileId,'');$('#role-capability').textContent=wsUpdateRoleCapability(agentId,profileId,$('#role-model').value);setDraftDirty()});$('#role-model').onchange=()=>{$('#role-capability').textContent=wsUpdateRoleCapability($('#role-agent').value,$('#role-profile').value,$('#role-model').value);setDraftDirty()};$('#role-instructions').oninput=setDraftDirty;$('#role-save').onclick=action(async()=>{await wsSaveRole();toast('Настройки роли сохранены')});$('#chat-target').onchange=action(()=>wsEditRole($('#chat-target').value));
 $('#att-file').onchange=action(async event=>{const files=Array.from(event.target.files||[]);try{await wsUpload(files)}finally{event.target.value=''}});
 $('#chat-form').onsubmit=async event=>{event.preventDefault();const input=$('#chat-input'),draft=input.value,text=draft.trim(),submittedProjectId=WS.project?.id,submittedConversationId=WS.conv?.id;if(!text||WS.sending)return;WS.sending=true;wsUpdateRecipient();$('#chat-msg').className='msg';$('#chat-msg').textContent='Передаю задачу…';try{const response=await wsSend(text),stillInSubmittedChat=WS.project?.id===submittedProjectId&&(!submittedConversationId||WS.conv?.id===submittedConversationId);if(stillInSubmittedChat&&input.value===draft)input.value='';if(stillInSubmittedChat){const warnings=response?.context_warnings||[];if(warnings.length){$('#chat-msg').className='msg warn-text';$('#chat-msg').textContent='Задача принята. Контекст: '+warnings.join(' ')}else{$('#chat-msg').className='msg ok';$('#chat-msg').textContent='Задача принята. Ответ появится в этом чате.'}}}catch(error){if(WS.project?.id===submittedProjectId&&(!submittedConversationId||WS.conv?.id===submittedConversationId)){$('#chat-msg').className='msg error';$('#chat-msg').textContent=error.message;workspaceError(error)}}finally{WS.sending=false;wsUpdateRecipient()}};
 $('#chat-input').onkeydown=event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();$('#chat-form').requestSubmit()}};$('#token-form').onsubmit=event=>{event.preventDefault();saveToken().catch(workspaceError)};
}

function stopPolling(){pollTimers.forEach(clearInterval);pollTimers=[];clearInterval(WS.pollTimer)}
function startPolling(){pollTimers.forEach(clearInterval);pollTimers=[setInterval(()=>health(),15000),setInterval(()=>{if(token&&!document.hidden&&WS.view==='ops')loadJobs()},5000)];wsStartPolling()}

async function boot(){
 await loadAgents();await Promise.allSettled([health(),loadJobs()]);
 try{await wsRefreshAll()}catch(error){workspaceError(error)}
 startPolling();
}
wireGate();wsWire();$('#build').textContent=BUILD;
if(token)boot().catch(workspaceError);else openGate('');
