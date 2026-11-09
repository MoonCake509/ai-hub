const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.options = []; this.value = ''; this.textContent = ''; this.hidden = false; this.disabled = false; }
  append(...items) { for (const item of items) { this.children.push(item); if (this.tagName === 'select' && item.tagName === 'option') this.options.push(item); } }
  replaceChildren(...items) { this.children = []; this.options = []; this.append(...items); }
  setAttribute() {}
  addEventListener() {}
  focus() {}
}

async function main() {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element(id === 'q-agent' || id === 'q-provider' || id === 'q-model' ? 'select' : 'div')); return elements.get(id); };
  const channels = [
    { channel_id: 'text', agent_id: 'vision', agent_name: 'Vision', profile: 'local', profile_name: 'Local', model: 'text-v1', model_name: 'Text model', enabled: true },
    { channel_id: 'ocr', agent_id: 'vision', agent_name: 'Vision', profile: 'local', profile_name: 'Local', model: 'ocr-v1', model_name: 'OCR model', enabled: true, requires_image: true, required_modalities: ['image'] },
  ];
  const requests = [];
  let auth = '';
  const payloads = {
    '/quality/channels': { channels },
    '/quality/suites': { suites: [{ id: 'suite', version: 1, description: 'Text checks', cases: [{ title: 'Case 1' }, { title: 'Case 2' }, { title: 'Case 3' }, { title: 'Case 4' }] }] },
    '/quality/runs?limit=50': { runs: [] },
  };
  const context = {
    document: { getElementById: get, createElement: tag => new Element(tag), querySelectorAll: () => [] },
    window: { crypto: { randomUUID: () => 'test-key' } },
    localStorage: { getItem: () => auth },
    setInterval() {}, MutationObserver: undefined,
    fetch: async (path, options = {}) => {
      requests.push({ path, method: options.method || 'GET' });
      const data = payloads[path] || {};
      return { status: 200, ok: true, text: async () => JSON.stringify(data) };
    },
    Date, Math, URL, console,
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'web', 'quality.js'), 'utf8'), context);
  auth = 'test-token';
  await get('q-refresh').onclick();

  assert.equal(get('q-channel-count').textContent, '2 / 2', 'manual channel management retains the image-required OCR channel');
  const managed = get('q-channel-list').children.map(card => card.children.map(child => child.textContent).join(' ')).join(' ');
  assert.match(managed, /OCR model/, 'image-required channel remains visible for manual management');
  assert.deepEqual(get('q-agent').options.map(option => option.value), ['', 'vision']);
  get('q-agent').value = 'vision';
  get('q-agent').onchange();
  get('q-provider').value = 'local';
  get('q-provider').onchange();
  assert.deepEqual(get('q-model').options.map(option => option.value), ['', 'text-v1'], 'text benchmark choices omit the image-required model');

  get('q-channel').value = 'ocr';
  get('q-case-count').value = '1';
  get('q-timeout').value = '60';
  assert.equal(get('q-start').disabled, true, 'image-required channel cannot enable the text benchmark start button');
  await get('q-start-form').onsubmit({ preventDefault() {} });
  assert.match(get('q-error').textContent, /не поддерживаются текущим текстовым набором/, 'manual OCR limitation is explained at selection validation');
  assert.equal(requests.some(request => request.path === '/quality/runs' && request.method === 'POST'), false, 'image-required channel cannot start a text suite run');
  console.log('Quality channel DOM regression passed: image routes remain manageable and are excluded from text benchmark selection.');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
