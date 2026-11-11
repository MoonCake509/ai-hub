'use strict';
/* FLOW-P1: панель процессов — DOM/fake-response регрессии без браузера.
   node tests/dom-flow-runs.cjs */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const root = process.env.AIHUB_UI_SOURCE || path.join(__dirname, '..', 'web');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8').replace(/\r\n/g, '\n');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8').replace(/\r\n/g, '\n');

function extract(source, start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `source slice not found: ${start}`);
  return source.slice(a, b);
}

function fakeNode(tag = 'div', cls = '', text = '') {
  const node = { tag, className: cls, textContent: text == null ? '' : String(text), children: [], dataset: {}, style: {}, hidden: false, disabled: false, value: '', colSpan: 0,
    append(...items) { this.children.push(...items); },
    focus() {}, reset() {}, replaceChildren(...items) { this.children = items; }, setAttribute() {},
    querySelector() { return null; },
    find(test) { for (const child of this.children) { if (test(child)) return child; const found = child.find && child.find(test); if (found) return found; } return null; },
  };
  return node;
}

function collectText(node, out = []) {
  if (!node) return out;
  if (node.textContent) out.push(node.textContent);
  for (const child of node.children || []) collectText(child, out);
  return out;
}

function appContext(extra = {}) {
  const fields = new Map();
  const $ = selector => { if (!fields.has(selector)) fields.set(selector, fakeNode()); return fields.get(selector); };
  const ctx = { $, el: fakeNode, URLSearchParams, encodeURIComponent, Date, Set, Map, Array, String, Number, Object, JSON, Math, NumberIsFinite: Number.isFinite,
    storeGet: () => 'project-1', toast() {}, api: async () => ({}), newKey: () => 'test-key-1',
    ...extra };
  vm.createContext(ctx);
  return { ctx, fields, $ };
}

function runSource() {
  return extract(app, '// FLOW-P1: запуски процесса', '// ADAPTER-01');
}

const RUN_ROW = { id: 'abcd1234ef5678ab', template_id: 'five-roles', state: 'active', goal: 'сделать фичу',
  criteria: ['тесты зелёные'], total: 5, done: 2, current: 'code',
  budgets: { max_dispatches: 10 }, budget_used: { dispatches: 2 },
  candidate_digest: 'sha256:' + 'c'.repeat(64),
  check_receipt: { ok: true, snapshot_id: 'snap-1', checks: [
    { name: 'unit', cmd: ['pytest', '-q'], exit_code: 0, passed: true }] },
  tasks: [
    { key: 'plan', role: 'planner', state: 'done', attempt: 1, agent: 'codex',
      assignment: { agent_id: 'codex', model: null }, context_id: null, error: null,
      result: { tasks: [{ key: 'k1' }] } },
    { key: 'code', role: 'coder', state: 'dispatched', attempt: 1, agent: 'codex',
      assignment: { agent_id: 'codex', model: 'm1' }, context_id: 'packet12345678', packet_revision: 1,
      error: null, result: {} },
    { key: 'test', role: 'tester', state: 'pending', attempt: 0, agent: 'codex',
      assignment: { agent_id: 'codex' }, context_id: null, error: null, result: {} },
  ],
  events: [
    { event: 'run_created', payload: { goal: 'сделать фичу' }, created_at: '2026-11-11T10:00:00' },
    { event: 'candidate_ready', payload: {}, created_at: '2026-11-11T10:02:00' },
    { event: 'checks_done', payload: { ok: true, checks: 1 }, created_at: '2026-11-11T10:03:00' },
  ] };

function testControlsExistOnce() {
  for (const id of ['runs-new', 'flow-create', 'flow-goal', 'flow-criteria', 'flow-entry',
                    'flow-contracts', 'flow-max-cycles', 'flow-max-dispatches', 'flow-max-calls',
                    'flow-max-seconds', 'flow-assignments', 'flow-preflight', 'flow-submit',
                    'flow-create-cancel', 'run-detail', 'runs-list', 'runs-status', 'runs-refresh']) {
    assert.equal((html.match(new RegExp('id="' + id + '"', 'g')) || []).length, 1, id + ' is unique in the UI');
  }
  assert.ok(html.indexOf('id="flow-create"') < html.indexOf('id="runs-list"'), 'creation form precedes the list');
}

async function testListRendersRowsAndOpensDetail() {
  const { ctx, $ } = appContext();
  const calls = [];
  ctx.api = async (path) => {
    calls.push(path);
    if (path.startsWith('/runs?')) return { items: [RUN_ROW], count: 1 };
    if (path === '/runs/abcd1234ef5678ab') return RUN_ROW;
    return {};
  };
  vm.runInContext(runSource(), ctx);
  await ctx.loadRuns();
  const list = $('#runs-list');
  assert.equal(list.children.length, 1, 'one run row rendered');
  const texts = collectText(list).join(' | ');
  assert.match(texts, /five-roles/, 'row shows template');
  assert.match(texts, /сделать фичу/, 'row shows the goal');
  assert.match($('#runs-status').textContent, /Запусков: 1/);
  // Клик по строке открывает detail.
  list.children[0].onclick();
  await new Promise(resolve => setImmediate(resolve));
  const detail = $('#run-detail');
  assert.equal(detail.hidden, false, 'detail is visible after click');
  const detailText = collectText(detail).join(' | ');
  assert.match(detailText, /Цель: сделать фичу/);
  assert.match(detailText, /Критерии: тесты зелёные/);
  assert.match(detailText, /Кандидат: sha256:/);
  assert.match(detailText, /Receipt проверок: пройдены/);
  assert.match(detailText, /unit — exit 0/);
  assert.match(detailText, /packet12@р1/, 'packet id with revision shown');
  assert.match(detailText, /Журнал событий/);
  assert.match(detailText, /кандидат зафиксирован/);
  assert.match(detailText, /m1/, 'assignment model shown for coder');
}

async function testDetailRestoresFromHashAfterReload() {
  const windowFake = { location: { hash: '#run=abcd1234ef5678ab', pathname: '/ui' },
    history: { replaceState() {} } };
  const { ctx, $ } = appContext({ window: windowFake });
  ctx.api = async (path) => {
    if (path.startsWith('/runs?')) return { items: [RUN_ROW], count: 1 };
    if (path === '/runs/abcd1234ef5678ab') return RUN_ROW;
    return {};
  };
  vm.runInContext(runSource(), ctx);
  await ctx.loadRuns();
  const detail = $('#run-detail');
  assert.equal(detail.hidden, false, 'detail restored after reload without any click');
  assert.match(collectText(detail).join(' | '), /Бюджет: шаги 2\/10/);
}

async function testCreateFormSubmitsGoalBudgetsAndRequestKey() {
  const { ctx, $ } = appContext();
  const posts = [];
  const hashes = [];
  ctx.window = { location: { hash: '', pathname: '/ui' },
    history: { replaceState(state, title, url) { hashes.push(url); } } };
  ctx.api = async (path, options = {}) => {
    if (path === '/projects/project-1') return { id: 'project-1', name: 'Проверка', workdir: '/srv/app',
      settings: { checks: { unit: { cmd: ['pytest'] } } }, roles: [{ role_id: 'coder', agent_id: 'codex', model: 'm2' }] };
    if (path === '/agents') return { agents: [{ id: 'codex' }] };
    if (path === '/runs' && options.method === 'POST') { posts.push(JSON.parse(options.body)); return { run_id: 'newrun1234' }; }
    if (path === '/runs/newrun1234') return { id: 'newrun1234', state: 'active', tasks: [], events: [] };
    return {};
  };
  vm.runInContext(runSource(), ctx);
  await ctx.runShowCreate();
  assert.equal($('#flow-create').hidden, false, 'form opened');
  assert.match($('#flow-preflight').textContent, /unit/, 'preflight lists project checks');
  assert.match(collectText($('#flow-assignments')).join(' '), /coder → codex · m2/);
  $('#flow-goal').value = 'новая цель';
  $('#flow-criteria').value = 'тесты зелёные; без регрессий';
  $('#flow-max-cycles').value = '3';
  $('#flow-max-dispatches').value = '12';
  $('#flow-max-calls').value = '';
  $('#flow-max-seconds').value = '600';
  await ctx.runSubmitCreate({ preventDefault() {} });
  assert.equal(posts.length, 1);
  const body = posts[0];
  assert.equal(body.goal, 'новая цель');
  assert.deepEqual(body.criteria, ['тесты зелёные', 'без регрессий']);
  assert.equal(body.policy.max_cycles, 3);
  assert.deepEqual(body.policy.budgets, { max_dispatches: 12, max_seconds: 600 });
  assert.equal(body.request_key, 'test-key-1', 'idempotency request key sent');
  assert.equal(body.template_id, 'five-roles');
  assert.equal($('#flow-create').hidden, true, 'form closed after create');
  assert.equal($('#run-detail').hidden, false, 'detail opened for the new run');
  assert.ok(hashes.includes('#run=newrun1234'), 'hash set to the new run');
}

async function testCreateConflictShows409Message() {
  const { ctx, $ } = appContext();
  ctx.api = async (path) => {
    if (path === '/projects/project-1') return { id: 'project-1', roles: [], settings: {} };
    if (path === '/agents') return { agents: [] };
    if (path === '/runs') { const error = new Error('409 request_key'); error.message = '409'; throw error; }
    return {};
  };
  vm.runInContext(runSource(), ctx);
  await ctx.runShowCreate();
  $('#flow-goal').value = 'цель';
  await ctx.runSubmitCreate({ preventDefault() {} });
  assert.match($('#flow-preflight').textContent, /409/, 'conflict message shown');
  assert.equal($('#flow-create').hidden, false, 'form stays open on conflict');
}

(async () => {
  testControlsExistOnce();
  await testListRendersRowsAndOpensDetail();
  await testDetailRestoresFromHashAfterReload();
  await testCreateFormSubmitsGoalBudgetsAndRequestKey();
  await testCreateConflictShows409Message();
  console.log('FLOW-P1 runs panel DOM regressions passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
