const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

function slice(start, next) {
  const from = app.indexOf(start);
  const to = app.indexOf(next, from + start.length);
  assert.ok(from >= 0 && to > from, `source slice ${start}`);
  return app.slice(from, to);
}

const helpers = [
  slice('function wsIsImageAttachment(', 'function wsRevokePreview('),
  slice('function wsRevokePreview(', 'function wsClearPreviewUrls('),
  slice('function wsClearPreviewUrls(', 'async function wsPreviewImage('),
  slice('async function wsPreviewImage(', 'function wsRenderAttachments('),
  slice('function wsModelCapabilities(', 'function wsLatestConversationImage('),
  slice('function wsLatestConversationImage(', 'function wsUpdateRoleCapability('),
  slice('function wsUpdateRoleCapability(', 'async function wsUpload('),
  slice('async function wsUpload(', 'async function wsLoadMessages('),
  slice('function wsUpdateRecipient(', 'function renderRoles('),
  slice('async function wsSend(', 'function wsStartPolling('),
];

const profile = {
  model_selection: true,
  default_profile: 'local',
  profiles: [{
    id: 'local', input_modalities: ['text', 'image'], required_modalities: ['image'],
    requires_image: true, context_mode: 'Один PNG/JPEG + текстовый вопрос', filesystem_access: false,
    models: ['xiaomi-ocr-q8'],
    model_capabilities: { 'xiaomi-ocr-q8': { input_modalities: ['text', 'image'], requires_image: true,
      required_modalities: ['image'], context_mode: 'Один PNG/JPEG + текстовый вопрос', filesystem_access: false } },
  }],
};
const textProfile = { model_selection: true, default_profile: 'local', profiles: [{
  id: 'local', input_modalities: ['text'], supports_workdir: false, models: ['dolphin'], model_capabilities: {
    dolphin: { input_modalities: ['text'], context_mode: 'Только текст prompt', filesystem_access: false },
  },
}] };
const optionalImageProfile = { model_selection: true, default_profile: 'local', profiles: [{
  id: 'local', input_modalities: ['text', 'image'], models: ['native-chat'], model_capabilities: {
    'native-chat': { input_modalities: ['text', 'image'], requires_image: false, filesystem_access: false },
  },
}] };

function makeElements() {
  const values = new Map();
  return selector => {
    if (!values.has(selector)) values.set(selector, { textContent: '', value: '', disabled: false, className: '',
      replaceChildren() {}, append() {}, setAttribute() {}, focus() {} });
    return values.get(selector);
  };
}

async function testCapabilityAndReuseHints() {
  const $ = makeElements();
  const context = {
    WS: { project: { id: 'p1' }, roleId: 'architect', dirty: false, roles: { architect: { agent_id: 'xiaomi', profile: 'local', model: 'xiaomi-ocr-q8' } },
      profiles: new Map([['xiaomi', profile]]), atts: [], selectedAtts: new Set(),
      messages: [{ attachments: [{ id: 'old-image', filename: 'invoice.png', kind: 'image', mime: 'image/png' }] }] },
    $, agents: [{ id: 'xiaomi', name: 'Xiaomi OCR', model_selection: true }],
    selectedAssignment: () => ({ agent_id: 'xiaomi', profile: 'local', model: 'xiaomi-ocr-q8' }),
    roleInfo: () => ['Архитектор', 'A'], agentName: id => id, wsIsImageAttachment: undefined,
  };
  vm.createContext(context);
  vm.runInContext(helpers.join('\n'), context);
  context.wsUpdateRecipient();
  assert.equal($('#chat-send').disabled, false, 'previous conversation image allows an OCR continuation');
  assert.match($('#chat-route-hint').textContent, /invoice\.png/);
  const ocrCapability = context.wsUpdateRoleCapability('xiaomi', 'local', 'xiaomi-ocr-q8');
  assert.match(ocrCapability, /Обязательно изображение PNG\/JPEG/);
  assert.match(ocrCapability, /Без доступа к файлам проекта/);

  context.WS.profiles.set('xiaomi', textProfile);
  context.WS.roles.architect = { agent_id: 'xiaomi', profile: 'local', model: 'dolphin' };
  context.selectedAssignment = () => context.WS.roles.architect;
  context.wsUpdateRecipient();
  assert.equal($('#chat-send').disabled, false, 'switching to a text-only role does not block the conversation');
  assert.match($('#chat-route-hint').textContent, /не передаются текстовому маршруту/);
  assert.match($('#chat-route-hint').textContent, /ответы распознавания остаются в истории/);
  assert.match(context.wsUpdateRoleCapability('xiaomi', 'local', 'dolphin'), /Только текст prompt/);
  assert.match($('#chat-route-hint').textContent, /не запускает задачи в папке проекта/);
  context.agents[0].transport = 'http';
  context.wsUpdateRecipient();
  assert.doesNotMatch($('#chat-route-hint').textContent, /не запускает задачи в папке проекта/, 'HTTP context-only routes remain usable without project workdir');

  context.WS.profiles.set('xiaomi', optionalImageProfile);
  context.WS.roles.architect = { agent_id: 'xiaomi', profile: 'local', model: 'native-chat' };
  context.selectedAssignment = () => context.WS.roles.architect;
  context.wsUpdateRecipient();
  assert.equal($('#chat-send').disabled, false, 'optional multimodal route does not require an image');
  assert.doesNotMatch($('#chat-route-hint').textContent, /обязательно выберите изображение/i);
  assert.match(context.wsUpdateRoleCapability('xiaomi', 'local', 'native-chat'), /Назначение применяется/);
}

function testRussianFileCountLabel() {
  const from = app.indexOf('function wsFilesCount(');
  const to = app.indexOf('\nfunction wsContextHint(', from);
  assert.ok(from >= 0 && to > from, 'visible context count formatter exists');
  const context = {};
  vm.createContext(context);
  vm.runInContext(app.slice(from, to), context);
  assert.equal(context.wsFilesCount(1), '1 файл');
  assert.equal(context.wsFilesCount(2), '2 файла');
  assert.equal(context.wsFilesCount(5), '5 файлов');
  assert.equal(context.wsFilesCount(21), '21 файл');
}

async function testUploadAndImageReplacement() {
  const $ = makeElements();
  let payloads = [];
  let renders = 0;
  const context = {
    WS: { project: { id: 'project-image' }, selectedAtts: new Set(['old-image']),
      atts: [{ id: 'old-image', kind: 'image', mime: 'image/jpeg', filename: 'old.jpg' }] },
    $, token: 'test-token', btoa: value => Buffer.from(value, 'binary').toString('base64'), TextDecoder,
    api: async (path, options) => { payloads.push({ path, payload: JSON.parse(options.body) }); return {
      id: 'new-image', filename: 'new.png', kind: 'image', mime: 'image/png', metadata: { width: 2, height: 3 },
      preview_url: '/attachments/new-image/preview',
    }; },
    wsIsImageAttachment: undefined, wsRenderAttachments: () => { renders += 1; }, toast() {},
  };
  vm.createContext(context);
  vm.runInContext(helpers.slice(0, 1).concat(helpers[7]).join('\n'), context);
  const file = { name: 'new.png', type: 'image/png', size: 4, arrayBuffer: async () => Uint8Array.from([1, 2, 3, 4]).buffer };
  await context.wsUpload([file]);
  assert.equal(payloads.length, 1);
  assert.equal(payloads[0].path, '/projects/project-image/attachments');
  assert.equal(payloads[0].payload.mime, 'image/png');
  assert.equal(context.WS.selectedAtts.has('old-image'), false, 'new image replaces prior selected OCR image');
  assert.equal(context.WS.selectedAtts.has('new-image'), true);
  assert.equal(renders, 1);
  await assert.rejects(() => context.wsUpload([file, { ...file, name: 'second.jpg', type: 'image/jpeg' }]), /только одно новое изображение/);
  assert.equal(payloads.length, 1, 'multiple new images are rejected before upload');
  await assert.rejects(() => context.wsUpload([{ ...file, size: 2 * 1024 * 1024 + 1 }]), /больше 2 МБ/);
  await assert.rejects(() => context.wsUpload([{ name: 'binary.txt', type: 'text/plain', size: 1,
    arrayBuffer: async () => Uint8Array.from([255]).buffer }]), /должен быть в UTF-8/);
  assert.equal(payloads.length, 1, 'invalid files are rejected before upload');
  assert.match(html, /accept="[^"]*image\/png[^"]*image\/jpeg/);
}

async function testAuthenticatedPreviewAndRevoke() {
  const headers = [];
  const revoked = [];
  class URLMock extends URL {}
  URLMock.createObjectURL = () => 'blob:preview-1';
  URLMock.revokeObjectURL = value => revoked.push(value);
  const status = { textContent: 'Загрузка' };
  const image = { isConnected: true, src: '' };
  const context = {
    WS: { atts: [{ id: 'image-1', kind: 'image' }], previewUrls: new Map(), previewLoads: new Map(), previewGeneration: 0 },
    URL: URLMock, token: 'private-token', window: { location: { href: 'http://hub.local/ui/', origin: 'http://hub.local' } },
    fetch: async (path, options) => { headers.push({ path, authorization: options.headers.Authorization }); return {
      ok: true, status: 200, headers: { get: name => name === 'content-type' ? 'image/png' : null }, blob: async () => ({ type: 'image/png' }),
    }; },
  };
  vm.createContext(context);
  vm.runInContext([helpers[0], helpers[1], helpers[2], helpers[3]].join('\n'), context);
  await context.wsPreviewImage({ id: 'image-1', preview_url: '/attachments/image-1/preview' }, image, status);
  assert.equal(image.src, 'blob:preview-1');
  assert.equal(status.textContent, '');
  assert.equal(headers[0].path, '/attachments/image-1/preview');
  assert.equal(headers[0].authorization, 'Bearer private-token');
  assert.equal(headers[0].path.includes('token'), false, 'token is not added to the URL');
  context.wsClearPreviewUrls();
  assert.deepEqual(revoked, ['blob:preview-1']);
}

async function testSendUsesNewImageOrReusesConversationImageAndFiltersTextRoute() {
  async function sendWith({ assignment, data, selected, response }) {
    const calls = [];
    const $ = makeElements();
    const atts = [
      { id: 'old-image', filename: 'old.png', kind: 'image', mime: 'image/png' },
      { id: 'notes', filename: 'notes.txt', mime: 'text/plain' },
      { id: 'new-image', filename: 'new.jpg', kind: 'image', mime: 'image/jpeg' },
    ];
    const context = {
      WS: { project: { id: 'p1' }, roleId: 'architect', dirty: false, roles: { architect: assignment }, conv: { id: 'c1' },
        convs: [], atts, selectedAtts: new Set(selected), pending: null, profiles: new Map(),
        messages: [{ attachments: [atts[0]] }], previewUrls: new Map(), previewLoads: new Map(), previewGeneration: 0 },
      $, token: 't', agents: [{ id: assignment.agent_id, model_selection: true }],
      selectedAssignment: () => assignment, wsSaveRole: async () => assignment,
      wsGetProfiles: async () => data,
      api: async (path, options) => { calls.push({ path, payload: JSON.parse(options.body) }); return response; },
      newKey: () => 'idem', wsRenderAttachments() {}, wsLoadMessages: async () => {}, wsStartPolling() {},
      wsRenderConversations() {}, conversationStorageKey: () => 'conv', storeSet() {},
    };
    vm.createContext(context);
    vm.runInContext(helpers.join('\n'), context);
    const result = await context.wsSend('read this');
    return { calls, result };
  }

  const oldImageReply = { id: 'old-image', filename: 'old.png', source: 'conversation', reused: true };
  const ocr = await sendWith({ assignment: { agent_id: 'xiaomi', profile: 'local', model: 'xiaomi-ocr-q8' },
    data: profile, selected: ['notes'], response: { job_id: 'j1', active_image: oldImageReply, context_warnings: [] } });
  assert.deepEqual(ocr.calls[0].payload.attachments, ['notes']);
  assert.equal(ocr.result.active_image.reused, true);
  assert.match(ocr.result.context_warnings.join(' '), /old\.png/);

  const text = await sendWith({ assignment: { agent_id: 'dolphin', profile: 'local', model: 'dolphin' },
    data: textProfile, selected: ['old-image', 'notes'], response: { job_id: 'j2', context_warnings: [] } });
  assert.deepEqual(text.calls[0].payload.attachments, ['notes'], 'text-only role omits image bytes');
  assert.match(text.result.context_warnings.join(' '), /не передано текстовому маршруту/);

  await assert.rejects(() => sendWith({ assignment: { agent_id: 'xiaomi', profile: 'local', model: 'xiaomi-ocr-q8' },
    data: profile, selected: ['old-image', 'new-image'], response: { job_id: 'j3' } }), /только одно новое изображение/);
}

(async () => {
  testRussianFileCountLabel();
  await testCapabilityAndReuseHints();
  await testUploadAndImageReplacement();
  await testAuthenticatedPreviewAndRevoke();
  await testSendUsesNewImageOrReusesConversationImageAndFiltersTextRoute();
  console.log('Image chooser, preview, OCR reuse and text-only context regressions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
