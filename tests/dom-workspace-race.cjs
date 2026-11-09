const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const sources = [
  path.join(__dirname, '..', 'web', 'app.js'),
];

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

async function testProjectSwitchDuringRoleSave(file) {
  const source = fs.readFileSync(file, 'utf8');
  const sendStart = source.indexOf('async function wsSend(');
  const sendEnd = source.indexOf('\nfunction wsStartPolling', sendStart);
  const wireStart = source.indexOf('function wsWire(){');
  const wireEnd = source.indexOf('\nfunction stopPolling', wireStart);
  assert.ok(sendStart >= 0 && sendEnd > sendStart, `wsSend found in ${file}`);
  assert.ok(wireStart >= 0 && wireEnd > wireStart, `wsWire found in ${file}`);

  const roleSave = deferred();
  const assignmentA = { agent_id: 'codex', profile: 'openai-A', model: 'gpt-A', timeout_s: 41 };
  const assignmentB = { agent_id: 'hermes-laptop', profile: 'claudehub-B', model: 'model-B', timeout_s: 52 };
  let currentAssignment = assignmentA;
  const calls = [];
  let saveCalls = 0;
  const elements = new Map();
  const getElement = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      value: selector === '#chat-input' ? 'A request' : '',
      textContent: '', className: '', disabled: false,
      setAttribute() {}, requestSubmit() {},
    });
    return elements.get(selector);
  };
  const context = {
    WS: {
      project: { id: 'project-A' }, roleId: 'tester', dirty: true,
      roles: { tester: { ...assignmentA } }, conv: { id: 'conversation-A' },
      selectedAtts: new Set(['attachment-A']), atts: [{ id: 'attachment-A', filename: 'context.txt' }], messages: [], pending: null, sending: false,
    },
    $: getElement,
    document: { querySelectorAll: () => [] },
    selectedAssignment: () => currentAssignment,
    wsSaveRole: snapshot => { saveCalls += 1; assert.equal(snapshot.projectId, 'project-A'); assert.equal(snapshot.roleId, 'tester'); return roleSave.promise; },
    wsGetProfiles: async agentId => ({ model_selection: true, agent_id: agentId }),
    agents: [{ id: 'codex', model_selection: true }, { id: 'hermes-laptop', model_selection: true }],
    api: async (path, options) => { calls.push({ path, payload: JSON.parse(options.body) }); return { job_id: 'job-A' }; },
    newKey: () => 'idempotency-A',
    action: fn => fn,
    setDraftDirty() {},
    wsUpdateRecipient() {},
    wsRefreshAll: async () => {}, wsNewConversation: async () => {}, wsProjectDialog() {},
    wsSaveProject: async () => {}, wsPopulateRoleProfiles: async () => {},
    wsUpload: async () => {}, saveToken: async () => {}, switchView() {}, workspaceError() {},
    wsRenderConversations() {}, wsRenderAttachments() {}, wsLoadMessages: async () => {}, wsStartPolling() {},
  };
  vm.createContext(context);
  const sendSource = source.slice(sendStart, sendEnd);
  const modelStart = source.indexOf('function wsModelCapabilities(');
  const latestEnd = source.indexOf('\nfunction wsUpdateRoleCapability(', modelStart);
  const imageStart = source.indexOf('function wsIsImageAttachment(');
  const imageEnd = source.indexOf('\nfunction wsRevokePreview(', imageStart);
  const latestStart = source.indexOf('function wsLatestConversationImage(');
  const latestNext = source.indexOf('\nfunction wsUpdateRoleCapability(', latestStart);
  vm.runInContext(source.slice(imageStart, imageEnd) + '\n' + source.slice(modelStart, latestEnd) + '\n' +
    source.slice(latestStart, latestNext) + '\n' + sendSource + '\n' + source.slice(wireStart, wireEnd), context);
  context.wsWire();

  const input = getElement('#chat-input');
  const submit = context.$('#chat-form').onsubmit({ preventDefault() {} });
  await Promise.resolve();
  assert.equal(saveCalls, 1, `delayed role save began in ${file}`);
  assert.equal(context.WS.project.id, 'project-A');

  // Switch projects while the A role PATCH is still pending and preserve a B draft.
  context.WS.project = { id: 'project-B' };
  context.WS.roleId = 'coder';
  context.WS.conv = { id: 'conversation-B' };
  context.WS.roles = { coder: { ...assignmentB } };
  context.WS.dirty = true;
  context.WS.selectedAtts = new Set(['attachment-B']);
  context.WS.pending = null;
  currentAssignment = assignmentB;
  input.value = 'B draft';
  roleSave.resolve({ ...assignmentA });
  await submit;

  assert.equal(calls.length, 1, `one request was sent in ${file}`);
  assert.equal(calls[0].path, '/conversations/conversation-A/send', `A conversation was captured before await in ${file}`);
  assert.equal(calls[0].payload.project_id, 'project-A', `request stayed on project A in ${file}`);
  assert.equal(calls[0].payload.role_id, 'tester', `role A stayed attached to request in ${file}`);
  assert.equal(calls[0].payload.agent_id, 'codex', `saved A assignment was used in ${file}`);
  assert.equal(calls[0].payload.profile, 'openai-A');
  assert.equal(calls[0].payload.model, 'gpt-A');
  assert.deepEqual(Array.from(calls[0].payload.attachments), ['attachment-A']);
  assert.equal(input.value, 'B draft', `response did not erase project B draft in ${file}`);
  assert.equal(context.WS.conv.id, 'conversation-B', `visible conversation stayed on project B in ${file}`);
}

(async () => {
  for (const file of sources) await testProjectSwitchDuringRoleSave(file);
  console.log('Delayed role-save A→B regression passed for integrated app.js');
})().catch(error => { console.error(error); process.exitCode = 1; });
