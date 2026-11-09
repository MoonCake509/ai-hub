const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

async function main() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
  const from = (start, end) => {
    const a = source.indexOf(start); const b = source.indexOf(end, a + start.length);
    assert.ok(a >= 0 && b > a, `source range ${start}`); return source.slice(a, b);
  };
  const elements = new Map();
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, { value: '', textContent: '', disabled: false, hidden: false, options: [],
      replaceChildren() { this.options = []; }, append(option) { this.options.push(option); }, setAttribute() {} });
    return elements.get(selector);
  };
  const profileLoad = deferred();
  const profileData = { model_selection: true, default_profile: 'local', profiles: [{ id: 'local', name: 'Local', models: ['luna-ocr'] }] };
  const context = {
    $, document: { querySelectorAll: () => [] },
    WS: { editorGen: 0, editorProfilesLoading: false, dirty: false, roleId: 'architect', project: { id: 'p1' }, profiles: new Map(),
      roles: { architect: { agent_id: 'codex', profile: 'openai', model: 'gpt-old' } } },
    agents: [{ id: 'codex', model_selection: true }, { id: 'xiaomi', name: 'Xiaomi', model_selection: true }],
    action: fn => (...args) => fn(...args), workspaceError: error => { $('#workspace-error-text').textContent = error.message; $('#workspace-error').hidden = false; },
    toast() {}, selectedAssignment: () => context.WS.roles.architect, roleInfo: () => ['Архитектор', 'A', ''], agentName: id => id,
    wsRefreshAll() {}, wsNewConversation() {}, wsProjectDialog() {}, wsSaveProject() {}, wsUpload() {}, saveToken() {}, switchView() {},
    wsGetProfiles: async agentId => agentId === 'xiaomi' ? profileLoad.promise : profileData,
    putOptions(select, rows, value) { select.replaceChildren(); for (const row of rows) { const option = { value: row.id }; select.append(option); } select.value = rows.some(row => row.id === value) ? value : rows[0]?.id || ''; },
    el: (tag, cls, text) => ({ value: '', textContent: text || '' }), wsUpdateRoleCapability: () => 'capability',
    wsUpdateRecipient() {}, wsPopulateRoleModels(data, profileId, modelId) {
      const profile = data.profiles.find(item => item.id === profileId);
      context.putOptions($('#role-model'), (profile?.models || []).map(model => ({ id: model })), modelId);
    }, renderRoles() {},
    api: async (url, options) => options?.method === 'PATCH' ? { agent_id: 'xiaomi', profile: 'local', model: 'luna-ocr' } : {},
  };
  vm.createContext(context);
  vm.runInContext(from('function setDraftDirty()', '\nfunction conversationStorageKey') +
    from('function wsSetRoleProfilesLoading(', '\nasync function wsSaveRole') +
    from('async function wsSaveRole(', '\n\nasync function wsSend') +
    from('function wsWire(){', '\nfunction stopPolling'), context);
  context.wsWire();

  $('#role-agent').value = 'xiaomi';
  const change = $('#role-agent').onchange();
  assert.equal($('#role-save').disabled, true, 'Save is disabled synchronously when a new agent starts loading');
  assert.match($('#role-save-status').textContent, /Загружаю провайдеры и модели/);
  await assert.rejects(() => context.wsSaveRole(), /Дождитесь загрузки/, 'saving is guarded while the newly selected provider profile is pending');

  profileLoad.resolve(profileData);
  await change;
  assert.equal($('#role-save').disabled, false, 'Save is restored after profiles and models load');
  assert.equal($('#role-profile').value, 'local');
  assert.equal($('#role-model').value, 'luna-ocr');
  assert.match($('#role-save-status').textContent, /Есть несохранённые настройки/);

  $('#workspace-error').hidden = false;
  await context.wsSaveRole();
  assert.equal($('#workspace-error').hidden, true, 'successful role save clears the stale workspace error');
  console.log('Role profile DOM race passed: Save waits for the selected agent catalog and clears prior errors on success.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
