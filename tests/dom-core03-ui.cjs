'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const root = process.env.AIHUB_UI_SOURCE || path.join(__dirname, '..', 'web');
const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8').replace(/\r\n/g, '\n');
const quality = fs.readFileSync(path.join(root, 'quality.js'), 'utf8').replace(/\r\n/g, '\n');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8').replace(/\r\n/g, '\n');
function extract(source, start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `source slice not found: ${start}`);
  return source.slice(a, b);
}
function fakeNode(tag = 'div', cls = '', text = '') {
  const node = { tag, className: cls, textContent: text == null ? '' : String(text), children: [], dataset: {}, hidden: false, disabled: false, value: '',
    append(...items) { this.children.push(...items); }, focus() {}, replaceChildren(...items) { this.children = items; }, setAttribute() {},
    querySelector(selector) { const matches = child => selector.startsWith('.') ? child.className.split(/\s+/).includes(selector.slice(1)) : child.tag === selector; return this.find(matches) || null; },
    querySelectorAll(selector) { const out = []; const matches = child => selector.startsWith('.') ? child.className.split(/\s+/).includes(selector.slice(1)) : child.tag === selector; const visit = current => { for (const child of current.children || []) { if (matches(child)) out.push(child); visit(child); } }; visit(this); return out; },
    find(test) { for (const child of this.children) { if (test(child)) return child; const found = child.find && child.find(test); if (found) return found; } return null; },
  };
  return node;
}
function appContext(extra = {}) {
  const fields = new Map();
  const $ = selector => { if (!fields.has(selector)) fields.set(selector, fakeNode()); return fields.get(selector); };
  const ctx = { $, el: fakeNode, URLSearchParams, encodeURIComponent, Date, Set, Map, Array, String, Number,
    ROLE_META: { architect: ['Архитектор'] }, workspaceError() {}, toast() {}, storeSet() {}, storeGet: () => 'project-new',
    conversationStorageKey: () => 'conv-key', wsRenderConversations() {}, renderRoles() {}, wsEditRole: async () => {},
    wsLoadMessages: async () => {}, wsStartPolling() {}, wsClearPreviewUrls() {}, wsRenderProjects() {}, wsRenderAttachments() {},
    renderMessages() {}, agents: [], OBS_EFFECTIVE_RU: { unknown: 'нет свежих данных' }, agentNames: new Map(),
    wsRefreshAll: async () => {}, wsProjectDialog() {}, wsSaveProject: async () => {}, wsPopulateRoleProfiles: async () => {}, wsUpload: async () => {}, saveToken: async () => {}, switchView() {},
    ...extra };
  vm.createContext(ctx);
  return { ctx, fields, $ };
}
function testNewControlsExistOnce() {
  for (const id of ['q-quick', 'recon-title', 'recon-list', 'recon-detail', 'recon-refresh']) {
    assert.equal((html.match(new RegExp('id=\"'+id+'\"', 'g')) || []).length, 1, id+' is unique in the UI');
  }
  assert.match(html, /value="unresolved"/); assert.match(html, /value="accepted_historical"/);
  assert.match(html, /до 45 с/);
  const quick = html.indexOf('id="q-quick"'), advanced = html.indexOf('id="q-advanced"');
  assert.ok(quick >= 0 && advanced > quick, 'quick action precedes extended disclosure');
  assert.ok(html.indexOf('id="q-case-count"') > advanced && html.indexOf('id="q-timeout"') > advanced, 'extended controls are inside disclosure');
  const opsStart = html.indexOf('id="view-ops"'), opsEnd = html.indexOf('</main>', opsStart), qualityStart = html.indexOf('id="view-quality"');
  const recon = html.indexOf('class="panel recon-panel"');
  assert.ok(recon > opsStart && recon < opsEnd && recon < qualityStart, 'reconciliation panel belongs to Operations');
}
async function testConversationMoreReturnsToReadyAfterRequest() {
  const { ctx, $ } = appContext();
  let release, calls = 0;
  ctx.WS = { project: { id: 'p1' }, convFilter: 'Проверка поиска', convCursor: 'cursor-24', convHasMore: true,
    convTotal: 55, convs: Array.from({ length: 24 }, (_, i) => ({ id: 'c' + i, title: 'chat ' + i })), convSeq: 0,
    convLoading: false, convSignature: '', conv: null, convShownId: '' };
  ctx.api = () => { calls++; return new Promise(resolve => { release = resolve; }); };
  const code = extract(app, 'function wsConversationParams(', '\nfunction wsConversationSummary(') + '\n' +
    extract(app, 'function wsConversationSummary(', '\nasync function wsOpenConversation(') + '\n' +
    extract(app, 'async function wsOpenConversation(', '\nfunction wsIsImageAttachment(');
  vm.runInContext(code, ctx);
  ctx.wsRenderConversations = ctx.wsRenderConversations || (() => {});
  vm.runInContext(extract(app, 'function wsRenderConversations(', '\nasync function wsOpenConversation(') + '\n' +
    extract(app, 'async function wsLoadConversations(', '\nfunction wsConversationSummary(') + '\n' +
    extract(app, 'function wsLoadMoreConversations(', '\nfunction wsConversationSummary('), ctx);
  // Bind the real render function after replacing its external action helpers with safe test stubs.
  ctx.action = fn => fn; ctx.wsOpenConversation = async () => {}; ctx.wsConversationTitle = c => c.title;
  ctx.wsChatCount = value => String(value);
  vm.runInContext(extract(app, 'function wsRenderConversations(', '\nasync function wsOpenConversation('), ctx);
  const more = $('#conv-more');
  ctx.wsRenderConversations();
  const first = ctx.wsLoadMoreConversations();
  assert.equal(more.disabled, true); assert.equal(more.textContent, 'Загружаю…');
  const second = ctx.wsLoadMoreConversations();
  assert.equal(calls, 1, 'repeated clicks share the in-flight request');
  release({ conversations: Array.from({ length: 24 }, (_, i) => ({ id: 'x' + i, title: 'next ' + i })), total: 55, has_more: true, next_cursor: 'cursor-48' });
  await Promise.all([first, second]);
  assert.equal(more.disabled, false); assert.equal(more.textContent, 'Показать ещё');
  assert.equal(ctx.WS.convs.length, 48);
  assert.equal(ctx.WS.convCursor, 'cursor-48');
}
async function testProjectSaveIsSingleFlightAndRestoresButton() {
  const { ctx, $ } = appContext();
  const button = fakeNode('button'); button.textContent = 'Сохранить';
  $('#project-settings-form').querySelector = () => button;
  $('#project-name').value = 'Приёмка'; $('#project-host').value = 'server'; $('#project-workdir').value = '/srv/project';
  ctx.WS = { project: { id: 'p1' }, newProject: false, projectSaving: false };
  let resolve, calls = 0;
  ctx.api = () => { calls++; return new Promise(done => { resolve = done; }); };
  ctx.wsRefreshAll = async () => {}; ctx.storeSet = () => {}; ctx.toast = () => {};
  vm.runInContext(extract(app, 'async function wsSaveProject(', '\nfunction wsWire('), ctx);
  const event = { preventDefault() {} };
  const first = ctx.wsSaveProject(event);
  assert.equal(button.disabled, true); assert.equal(button.textContent, 'Сохраняю…');
  const second = ctx.wsSaveProject(event);
  assert.equal(calls, 1, 'double submit cannot create a second project request');
  resolve({ id: 'p1' }); await Promise.all([first, second]);
  assert.equal(button.disabled, false); assert.equal(button.textContent, 'Сохранить');
  assert.equal(ctx.WS.projectSaving, false);
}
async function testNewChatResetsSearchCursor() {
  const requests = [];
  const { ctx, $ } = appContext();
  ctx.WS = { project: { id: 'p1' }, roleId: 'architect', convFilter: 'needle', convCursor: 'filtered-anchor', convHasMore: true,
    convTotal: 44, convs: [{ id: 'old', title: 'matching old chat' }], convSeq: 0, convLoading: false, convSignature: 'old',
    conv: { id: 'old' }, messages: [], pending: null };
  ctx.api = async (path, options = {}) => {
    requests.push({ path, options });
    if (options.method === 'POST') return { id: 'new-chat', project_id: 'p1', role_id: 'architect', title: 'Новый чат' };
    return { conversations: [{ id: 'new-chat', title: 'Новый чат' }, { id: 'fresh-1', title: 'Fresh page' }], total: 45, has_more: true, next_cursor: 'fresh-anchor' };
  };
  const source = extract(app, 'function wsConversationParams(', '\nfunction wsConversationSummary(') + '\n' + extract(app, 'async function wsNewConversation()', '\nfunction wsIsImageAttachment(');
  vm.runInContext(source, ctx);
  await ctx.wsNewConversation();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].path, '/projects/p1/conversations');
  assert.match(requests[1].path, /limit=24/);
  assert.doesNotMatch(requests[1].path, /cursor=|q=/, 'new chat reload starts an unfiltered first page');
  assert.equal(ctx.WS.convCursor, 'fresh-anchor');
  assert.equal(ctx.WS.convFilter, '');
  assert.equal(ctx.WS.conv.id, 'new-chat');
  assert.equal(ctx.WS.convs[0].id, 'new-chat');
  assert.equal($('#conv-filter').value, '');
}
async function testResetSearchAndProjectCursor() {
  const requests = [];
  const { ctx } = appContext();
  ctx.WS = { project: { id: 'p1' }, convFilter: 'needle', convCursor: 'old-anchor', convHasMore: true, convTotal: 20,
    convs: [], convSeq: 0, convLoading: false };
  ctx.api = async path => { requests.push(path); return { conversations: [], total: 0, has_more: false, next_cursor: null }; };
  vm.runInContext(extract(app, 'function wsConversationParams(', '\nfunction wsConversationSummary('), ctx);
  await ctx.wsLoadConversations({ reset: true });
  assert.match(requests[0], /q=needle/);
  assert.doesNotMatch(requests[0], /cursor=/, 'search reset starts without the previous cursor');
  assert.equal(ctx.WS.convCursor, '');

  const project = appContext();
  project.ctx.WS = { generation: 0, project: { id: 'old-project' }, projects: [], convCursor: 'old-anchor', convHasMore: true, convTotal: 55,
    convs: [], conv: null, messages: [], selectedAtts: new Set(), roles: {}, previewUrls: new Map(), previewLoads: new Map() };
  let cursorAtFirstRequest = 'not-read';
  project.ctx.api = async apiPath => {
    if (apiPath === '/projects') { cursorAtFirstRequest = project.ctx.WS.convCursor; return { projects: [{ id: 'project-new', name: 'New', host_id: 'laptop', workdir: '' }] }; }
    if (apiPath === '/projects/project-new') return { id: 'project-new', name: 'New', roles: [], host_id: 'laptop', workdir: '' };
    if (apiPath.endsWith('/attachments')) return { attachments: [] };
    throw new Error('unexpected API '+apiPath);
  };
  project.ctx.wsLoadConversations = async options => { project.ctx.resetWasRequested = options.reset; project.ctx.WS.convs = []; };
  vm.runInContext(extract(app, 'async function wsRefreshAll()', '\nfunction wsRenderProjects('), project.ctx);
  await project.ctx.wsRefreshAll();
  assert.equal(cursorAtFirstRequest, '', 'project refresh clears prior project cursor before request');
  assert.equal(project.ctx.resetWasRequested, true);
}
async function testAgentsRefreshDoesNotOverlap() {
  let resolve;
  const first = new Promise(done => { resolve = done; });
  let calls = 0, profileLoads = 0;
  const list = fakeNode(); list.querySelector = () => null; list.querySelectorAll = () => []; list.replaceChildren = function (...items) { this.children = items; };
  const select = fakeNode(); select.value = 'agent-a'; select.append = function (item) { this.children.push(item); };
  const fields = new Map([['#agents-list', list], ['#run-agent', select], ['#agents-count', fakeNode()]]);
  const ctx = { $: selector => fields.get(selector) || fakeNode(), el: fakeNode, agentsLoading: false, agents: [], agentNames: new Map(),
    OBS_EFFECTIVE_RU: { unknown: 'нет свежих данных' }, obsBadge: () => fakeNode('button'), obsPanel: () => fakeNode(),
    document: { querySelectorAll: () => [] }, loadAgentProfiles: async () => { profileLoads++; },
    api: async () => { calls++; return first; }, Array, Set, Map };
  vm.createContext(ctx);
  vm.runInContext(extract(app, 'async function loadAgents()', '\n// Профили и модели'), ctx);
  const a = ctx.loadAgents();
  const b = ctx.loadAgents();
  assert.equal(calls, 1, 'overlapping refresh is skipped while request is pending');
  resolve({ agents: [{ id: 'agent-a', name: 'A', bridge_agent: 'a', effective_state: 'unknown', transport: 'cli' }] });
  await Promise.all([a, b]);
  assert.equal(calls, 1);
  assert.equal(select.value, 'agent-a', 'refresh preserves selected agent');
  assert.equal(profileLoads, 0, 'refresh does not reset or reload the selected profile');
  assert.equal(ctx.agentsLoading, false);
}
function testObservationAgeUsesServerClock() {
  const fn = extract(app, 'function obsAge(iso, ageSeconds) {', '\n\nfunction obsTtl(');
  const ctx = { Date: { now: () => Date.parse('2035-01-01T00:00:00Z') }, Number, Math };
  vm.createContext(ctx); vm.runInContext(fn, ctx);
  assert.equal(ctx.obsAge('2026-11-10T00:00:00+00:00', 240), '4 мин назад', 'display uses API age_s instead of client clock');
}
async function testQualityDetailLatestRequestWins() {
  const pending = new Map(); const rendered = [];
  const ctx = { state: { selectedRunId: null, detailRequest: 0 }, api: path => new Promise(resolve => pending.set(path, resolve)),
    renderRunList() {}, renderRunDetail: run => rendered.push(run.id) };
  vm.createContext(ctx);
  vm.runInContext(extract(quality, '  async function loadRunDetail(id) {', '\n  async function refreshActiveRuns()'), ctx);
  const a = ctx.loadRunDetail('A');
  const b = ctx.loadRunDetail('B');
  pending.get('/quality/runs/B')({ id: 'B' }); await b;
  pending.get('/quality/runs/A')({ id: 'A' }); await a;
  assert.deepEqual(rendered, ['B'], 'late response for earlier selected run is ignored');
  assert.equal(ctx.state.selectedRunId, 'B');
}
async function testQuickRunPayloadIsBoundedAndDeduplicated() {
  let resolve, calls = 0; const request = new Promise(done => { resolve = done; }); const payloads = [];
  const fields = new Map();
  const q = selector => { const key = selector.startsWith('#') ? selector : '#'+selector; if (!fields.has(key)) fields.set(key, fakeNode()); return fields.get(key); };
  q('#q-case-count').value = '4'; q('#q-timeout').value = '120';
  const ctx = { q, state: { suite: { id: 'extended', version: '1' }, starting: false, loading: false, selectedRunId: null },
    chosenChannel: () => ({ channel_id: 'ch', agent_id: 'agent', profile: 'profile', model: 'model', enabled: true }), requiresImage: () => false,
    updateStartEnabled() {}, showError() {}, errText: e => String(e), loadRuns: async () => {}, loadRunDetail: async () => {},
    api: async (path, options) => { calls++; payloads.push({ path, body: JSON.parse(options.body) }); return request; },
    window: { crypto: null }, Date, Number, Math, JSON };
  vm.createContext(ctx);
  vm.runInContext(extract(quality, "  q('q-start-form').onsubmit = async event => {", '\n\n  document.querySelectorAll'), ctx);
  const submitter = { dataset: { mode: 'quick' } };
  const event = { submitter, preventDefault() {} };
  const first = q('#q-start-form').onsubmit(event);
  const doubleClick = q('#q-start-form').onsubmit(event);
  assert.equal(calls, 1, 'second click while request pending is ignored');
  assert.equal(payloads[0].path, '/quality/runs');
  assert.equal(payloads[0].body.mode, 'quick');
  assert.equal(payloads[0].body.budget_s, 45);
  assert.ok(payloads[0].body.budget_s <= 120);
  assert.equal(payloads[0].body.agent_id, 'agent');
  assert.equal(payloads[0].body.profile, 'profile');
  assert.equal(payloads[0].body.model, 'model');
  assert.equal(payloads[0].body.max_calls, undefined, 'server supplies the enforced modality-specific cap');
  assert.equal(payloads[0].body.case_count, undefined);
  assert.equal(payloads[0].body.timeout_s, undefined);
  resolve({ id: 'quick-run', mode: 'quick' });
  await Promise.all([first, doubleClick]);
  assert.equal(ctx.state.starting, false);
}
function testQuickPartialVerdictText() {
  const fn = extract(quality, '  function quickVerdictText(run) {', '\n\n  function renderRunDetail');
  const ctx = { number: value => value === undefined || value === null ? 'недоступно' : String(value), statusLabel: status => ({ completed: 'завершён' }[status] || status) };
  vm.createContext(ctx); vm.runInContext(fn, ctx);
  const text = ctx.quickVerdictText({ budget_s: 45, max_calls: 3, status: 'completed', verdict: { state: 'insufficient', label: 'данных недостаточно', graded_count: 1, skipped_count: 2 } });
  assert.match(text, /данных недостаточно/); assert.match(text, /Завершено проверок: 1; пропущено: 2/);
  assert.match(text, /пропущенные или ожидающие пробы не считаются успешными/i);
  assert.doesNotMatch(text, /вердикт: без замечаний/i);
}
function reconciliationContext() {
  const fields = new Map(); const made = [];
  const $ = selector => { if (!fields.has(selector)) fields.set(selector, fakeNode()); return fields.get(selector); };
  const ctx = { $, el: (tag, cls, text) => { const item = fakeNode(tag, cls, text); made.push(item); return item; }, URLSearchParams, encodeURIComponent, Date, Set, Map, Array, String, Number,
    api: async () => { throw new Error('unexpected API'); } };
  vm.createContext(ctx);
  vm.runInContext(extract(app, 'const Recon={decisionState:', '\n// ---------- токен ----------'), ctx);
  return { ctx, fields, made, $ };
}
function walk(node, out = []) { out.push(node); for (const child of node.children || []) walk(child, out); return out; }
async function testReconciliationRenderAndDecision() {
  const { ctx, fields, made, $ } = reconciliationContext();
  const untrusted = '<img src=x onerror=alert(1)>';
  const item = { case_id: 'late:late-id', kind: 'late', job_id: 'job-1', job_status: 'unknown', execution_state: 'unknown', decision_state: 'unresolved', attempt_status: 'unknown', attempt_no: 1, revision: 0,
    operation: { operation: 'apply_patch', operation_key: 'op-key' }, candidate_digest: 'candidate', artifact_summary: untrusted,
    evidence: { provenance: 'insufficient', attempt_id: 'attempt-1', job_id: 'job-1', operation_key: 'op-key', candidate_digest: 'candidate' }, allowed_actions: ['reject'], disabled_reason: 'Недостаточно подтверждённой связи.', audit: [] };
  ctx.testCase = item;
  vm.runInContext('reconRenderDetail(testCase)', ctx);
  const detail = $('#recon-detail');
  const nodes = walk(detail);
  const shown = nodes.map(node => node.textContent).join(' ');
  assert.match(shown, /Исходный статус задания/);
  assert.match(shown, /Состояние исполнения/);
  assert.match(shown, /Решение сверки/);
  assert.match(shown, /unknown/);
  assert.match(shown, /insufficient/);
  assert.match(shown, /исторический артефакт/);
  assert.match(shown, /Недостаточно подтверждённой связи/);
  assert.ok(nodes.some(node => node.textContent === untrusted), 'untrusted artifact text is inserted as plain text');
  assert.equal(nodes.some(node => node.tag === 'img'), false, 'no HTML element is created from artifact text');
  const form = detail.find(node => node.className.includes('recon-decision'));
  assert.ok(form, 'server allowed rejection action is displayed');
  const reason = form.find(node => node.tag === 'textarea');
  assert.equal(reason.required, true);
  assert.equal(reason.minLength, 3);
  const buttons = form.querySelectorAll('button');
  assert.equal(buttons.length, 1, 'only the server-allowed reject action is shown');
  assert.equal(buttons[0].dataset.action, 'reject');
  assert.doesNotMatch(buttons.map(button => button.textContent).join(' '), /Принять/);

  let resolvePost; const posts = []; ctx.detailReloads = 0; ctx.listReloads = 0;
  ctx.api = (apiPath, options) => { posts.push({ apiPath, body: JSON.parse(options.body) }); return new Promise(resolve => { resolvePost = resolve; }); };
  vm.runInContext('reconOpenCase = async function(){ detailReloads++; }; reconLoad = async function(){ listReloads++; };', ctx);
  const first = ctx.reconDecide(item, 'reject', ' Проверены журнал и receipt ', form);
  const duplicate = ctx.reconDecide(item, 'reject', 'Проверено дважды', form);
  assert.equal(posts.length, 1, 'double click makes only one decision request');
  assert.equal(posts[0].apiPath, '/workspace/reconciliation/cases/late%3Alate-id/decision');
  assert.deepEqual(posts[0].body, { action: 'reject', expected_revision: 0, reason: 'Проверены журнал и receipt' });
  resolvePost({ case: {}, decision: {} });
  await Promise.all([first, duplicate]);
  assert.equal(ctx.detailReloads, 1);
  assert.equal(ctx.listReloads, 1);
  assert.equal(vm.runInContext('Recon.notice', ctx), 'Решение сохранено в истории. Исходный статус задания не изменён.');
  assert.equal(item.job_status, 'unknown', 'manual decision does not rewrite original execution state');

  const countBefore = posts.length;
  await ctx.reconDecide(item, 'reject', '  ', form);
  assert.equal(posts.length, countBefore, 'blank/too short reason is rejected client-side');
  assert.match(form.querySelector('.recon-decision-msg').textContent, /основание/);

  ctx.api = async () => { throw new Error('409 stale revision'); };
  await ctx.reconDecide(item, 'reject', 'Проверено повторно', form);
  assert.equal(ctx.detailReloads, 2, '409 triggers a detail reload');
  assert.match(vm.runInContext('Recon.notice', ctx), /Ревизия изменилась/);
}

(async () => {
  testNewControlsExistOnce();
  await testNewChatResetsSearchCursor();
  await testResetSearchAndProjectCursor();
    await testConversationMoreReturnsToReadyAfterRequest();
    await testProjectSaveIsSingleFlightAndRestoresButton();
  await testAgentsRefreshDoesNotOverlap();
  testObservationAgeUsesServerClock();
  await testQualityDetailLatestRequestWins();
  await testQuickRunPayloadIsBoundedAndDeduplicated();
  testQuickPartialVerdictText();
  await testReconciliationRenderAndDecision();
  console.log('CORE-03 UI DOM/fake-response regressions passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
