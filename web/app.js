// AI-Hub UI: реестр агентов, запуск заданий, история, детали, отмена.
// Без сборки и зависимостей — обычные fetch + DOM.

const API = window.location.pathname.startsWith('/ui') ? '' : '';
const TOKEN_KEY = 'aihub_token';
let token = localStorage.getItem(TOKEN_KEY) || '';
let currentJob = null;
let currentStatus = '';
let agents = [];

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

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers.Authorization = 'Bearer ' + token;
  const resp = await fetch(API + path, { ...opts, headers });
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
function openGate(msg) {
  $('#gate').hidden = false;
  $('#token-err').textContent = msg || '';
  $('#token-input').value = '';
  $('#token-input').focus();
}
function closeGate() { $('#gate').hidden = true; }

$('#token-btn').onclick = () => openGate('');
$('#token-cancel').onclick = () => { if (token) closeGate(); };
$('#token-save').onclick = async () => {
  const val = $('#token-input').value.trim();
  if (!val) return;
  token = val;
  try {
    await api('/health');
    localStorage.setItem(TOKEN_KEY, token);
    closeGate();
    boot();
  } catch (e) {
    $('#token-err').textContent = e.message === 'unauthorized' ? 'Неверный токен.' : e.message;
  }
};
$('#token-input').addEventListener('keydown', e => { if (e.key === 'Enter') $('#token-save').click(); });

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
    row.append(el('span', 'status ' + (a.status || ''), a.status || '—'));
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
    const body = {
      prompt: $('#run-prompt').value.trim(),
      timeout: Number($('#run-timeout').value) || 300,
      idempotency_key: newKey(),
    };
    const r = await api('/agents/' + encodeURIComponent($('#run-agent').value) + '/run', {
      method: 'POST', body: JSON.stringify(body),
    });
    msg.className = 'msg ok';
    msg.textContent = 'Задание ' + r.job_id.slice(0, 8) + ' принято (' + r.status + ')';
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
    loadJobs();
  };
});

async function loadJobs() {
  try {
    const q = '/jobs?limit=100' + (currentStatus ? '&status=' + encodeURIComponent(currentStatus) : '');
    const data = await api(q);
    const jobs = data.jobs || [];
    const body = $('#jobs-body');
    body.innerHTML = '';
    $('#jobs-empty').hidden = jobs.length > 0;
    for (const j of jobs) {
      const tr = el('tr');
      tr.dataset.id = j.id;
      if (currentJob && currentJob.id === j.id) tr.classList.add('selected');
      tr.append(el('td', '', fmtTime(j.created_at)));
      tr.append(el('td', 'mono', j.agent_id || '—'));
      const st = el('td');
      st.append(el('span', 'badge ' + j.status, j.status));
      tr.append(st);
      tr.append(el('td', '', fmtDur(j.created_at, j.finished_at, j.status === 'running')));
      tr.append(el('td', '', String(j.attempts ?? 0)));
      tr.append(el('td', 'prompt', j.prompt_preview || ''));
      tr.onclick = () => openJob(j.id);
      body.append(tr);
    }
  } catch (e) {
    if (e.message !== 'unauthorized') $('#health-text').textContent = 'ошибка истории: ' + e.message;
  }
}

// ---------- детали ----------
function openJob(id) {
  currentJob = { id };
  $('#d-note').textContent = '';
  $('#detail').hidden = false;
  document.querySelectorAll('#jobs-body tr').forEach(tr => tr.classList.toggle('selected', tr.dataset.id === id));
  renderJob(true);
}

$('#d-close').onclick = () => { currentJob = null; $('#detail').hidden = true; document.querySelectorAll('#jobs-body tr').forEach(tr => tr.classList.remove('selected')); };

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
  try {
    const [job, ev] = await Promise.all([
      api('/jobs/' + id),
      api('/jobs/' + id + '/events').catch(() => ({ events: [] })),
    ]);
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
    add('Агент', job.agent_id || '—');
    add('Статус', job.status);
    add('Создано', fmtTime(job.created_at));
    add('Завершено', job.finished_at ? fmtTime(job.finished_at) : '—');
    add('Длительность', fmtDur(job.created_at, job.finished_at, job.status === 'running'));
    add('Попыток', String(job.attempts ?? 0));
    add('Таймаут', (job.timeout_s ?? '—') + ' с');
    add('Сверка', job.needs_reconcile ? 'нужна (результат неизвестен)' : 'не требуется');

    $('#d-output').textContent = job.result_output || (job.result_error ? 'Ошибка: ' + job.result_error : '—');

    const att = $('#d-attempts');
    att.innerHTML = '';
    const list = job.attempts_detail || [];
    if (!list.length) att.append(el('p', 'muted', 'Попыток не было — задание ещё в очереди.'));
    for (const a of list) {
      const box = el('div', 'attempt');
      const row = el('div', 'row');
      row.append(el('span', 'badge ' + a.status, a.status));
      row.append(el('span', 'mono', '#' + a.attempt_no));
      row.append(el('span', 'muted', a.worker || '—'));
      row.append(el('span', 'muted', fmtDur(a.started_at, a.finished_at, a.status === 'running')));
      if (a.usage && Object.keys(a.usage).length) {
        row.append(el('span', 'muted', 'tok ' + (a.usage.input_tokens ?? '?') + '/' + (a.usage.output_tokens ?? '?')));
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
      li.innerHTML = '<b>' + esc(e.event) + '</b> — ' + esc(fmtTime(e.at)) +
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
async function boot() {
  await health();
  try {
    await loadAgents();
  } catch (e) {
    if (e.message !== 'unauthorized') $('#agents-count').textContent = 'ошибка: ' + e.message;
  }
  await loadJobs();
}

(async () => {
  if (!token) { openGate(''); return; }
  await boot();
  setInterval(health, 15000);
  setInterval(() => { if (!document.hidden) loadJobs(); }, 5000);
})();
