const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
function extract(start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `expected ${start}`);
  return source.slice(a, b);
}
const elements = new Map();
const $ = selector => {
  if (!elements.has(selector)) elements.set(selector, { textContent: '', value: '', disabled: false, className: '',
    replaceChildren() {}, append() {}, setAttribute() {} });
  return elements.get(selector);
};
const role = { agent_id: 'codex', profile: 'openai', model: 'gpt-6-luna' };
let renderCount = 0;
const context = {
  WS: { project: { id: 'project-A' }, roleId: 'architect', dirty: false, roles: { architect: role },
    conv: { id: 'conversation-A', title: 'Новый чат' }, convs: [{ id: 'conversation-A', title: 'Новый чат' }],
    selectedAtts: new Set(), atts: [], messages: [], profiles: new Map(), pending: null },
  $, token: 'token', agents: [{ id: 'codex', model_selection: true }],
  selectedAssignment: () => role, wsGetProfiles: async () => ({ model_selection: true, profiles: [] }),
  api: async path => ({ job_id: 'job-A', conversation_title: 'Проверка новых правил' }),
  newKey: () => 'idem', wsRenderConversations: () => { renderCount += 1; }, wsRenderAttachments() {},
  wsLoadMessages: async () => {}, wsStartPolling() {}, workspaceError() {},
};
vm.createContext(context);
const selectedImage = extract('function wsIsImageAttachment(', 'function wsRevokePreview(');
const modelCaps = extract('function wsModelCapabilities(', 'function wsLatestConversationImage(');
const latestImage = extract('function wsLatestConversationImage(', 'function wsUpdateRoleCapability(');
const send = extract('async function wsSend(', 'function wsStartPolling(');
vm.runInContext([selectedImage, modelCaps, latestImage, send].join('\n'), context);

(async () => {
  await context.wsSend('Назови код');
  assert.equal(context.WS.conv.title, 'Проверка новых правил');
  assert.equal(context.WS.convs[0].title, 'Проверка новых правил');
  assert.equal(renderCount, 1, 'conversation sidebar refreshes immediately');
  console.log('Optional conversation_title updates active chat and list immediately');
})().catch(error => { console.error(error); process.exitCode = 1; });
