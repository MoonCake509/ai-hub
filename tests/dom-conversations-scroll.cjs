const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');

const app = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');

function slice(start, next) {
  const from = app.indexOf(start);
  const to = app.indexOf(next, from + start.length);
  assert.ok(from >= 0 && to > from, 'source slice ' + start);
  return app.slice(from, to);
}

const helpers = slice('function wsConversationTitle(', '\nasync function wsOpenConversation(');

function node(tag) {
  return {
    tag: tag, className: '', title: '', textContent: '', children: [], scrollTop: 0, disabled: false, hidden: false,
    append: function () { this.children.push.apply(this.children, arguments); },
    setAttribute: function () {},
  };
}

function Box() {
  this.children = [];
  this.scrollTop = 0;
  this.scrollHeight = 1000;
  this.clientHeight = 200;
}
Box.prototype = {
  get childElementCount() { return this.children.length; },
  replaceChildren: function () { this.children = Array.prototype.slice.call(arguments); this.scrollTop = 0; },
  append: function () { this.children.push.apply(this.children, arguments); },
  querySelector: function (selector) {
    var cls = selector.replace('.', '');
    return this.children.find(function (child) { return child.className.split(' ').indexOf(cls) >= 0; }) || null;
  },
};

function makeContext(options) {
  const settings = options || {};
  const box = new Box();
  const fields = new Map();
  const $ = function (selector) {
    if (selector === '#convs-list') return box;
    if (!fields.has(selector)) fields.set(selector, node('div'));
    return fields.get(selector);
  };
  const context = {
    WS: { convs: [], conv: null, convFilter: '', convSignature: '', convShownId: '', convCursor: '',
      convHasMore: false, convTotal: 0, convLoading: false, convSeq: 0, project: { id: 'p1' } },
    $: $, el: function (tag, className, text) { const item = node(tag); item.className = className || ''; if (text != null) item.textContent = text; return item; },
    action: function (handler) { return handler; }, wsOpenConversation: function () {},
    URLSearchParams: URLSearchParams,
    workspaceError: function (error) { context.lastError = String(error && error.message || error); },
    calls: [], box: box, api: async function (path) {
      context.calls.push(path);
      const handler = settings.api;
      return handler ? handler(path, context.calls.length) : { conversations: [], total: 0, has_more: false, next_cursor: '' };
    },
  };
  if (settings.state) Object.assign(context.WS, settings.state);
  vm.createContext(context);
  vm.runInContext(helpers, context);
  return context;
}

function conversations(from, count) {
  return Array.from({ length: count }, function (_, index) {
    const number = from + index;
    return { id: 'conv-' + number, title: 'Разбор архитектуры проекта и длинное название разговора номер ' + number + ' — продолжение проверки' };
  });
}

function titles(box) {
  return box.children.filter(function (child) { return child.className.split(' ')[0] === 'conv'; })
    .map(function (child) { return child.children[1].textContent; });
}

async function main() {
// 1. первая страница: 24 строки, общее число известно, есть следующий шаг
{
  const context = makeContext();
  Object.assign(context.WS, { convs: conversations(0, 24), convTotal: 55, convHasMore: true, conv: conversations(0, 1)[0] });
  context.wsRenderConversations();
  assert.equal(titles(context.box).length, 24);
  assert.match(context.$('#conv-summary').textContent, /55 чатов · показаны 24/);
  assert.equal(context.$('#conv-more').hidden, false);
  assert.match(context.$('#conv-more').textContent, /Показать ещё/);
}

// 2. polling с неизменными данными не перестраивает строки и не сбрасывает прокрутку
{
  const context = makeContext();
  Object.assign(context.WS, { convs: conversations(0, 24), convTotal: 55, convHasMore: true, conv: conversations(0, 1)[0] });
  context.wsRenderConversations();
  const before = context.box.children.slice();
  context.box.scrollTop = 180;
  context.wsRenderConversations();
  context.wsRenderConversations();
  assert.ok(context.box.children.every(function (child, index) { return child === before[index]; }), 'одинаковые данные сохраняют узлы');
  assert.equal(context.box.scrollTop, 180, 'позиция читателя переживает повторную отрисовку');
}

// 3. вторая страница добавляется по курсору, дубликаты не появляются
{
  let step = 0;
  const context = makeContext({ api: function (path) {
    step += 1;
    if (step === 1) return { conversations: conversations(0, 24), total: 55, has_more: true, next_cursor: 'cursor-1' };
    return { conversations: conversations(22, 24), total: 55, has_more: true, next_cursor: 'cursor-2' };
  } });
  await context.wsLoadConversations({ reset: true });
  assert.equal(context.WS.convs.length, 24);
  assert.equal(context.WS.convCursor, 'cursor-1');
  assert.match(context.calls[0], /limit=24/);
  assert.doesNotMatch(context.calls[0], /cursor=/);
  await context.wsLoadConversations({});
  assert.equal(context.WS.convs.length, 46, 'пересечение страниц схлопывается по id');
  assert.equal(new Set(context.WS.convs.map(function (item) { return item.id; })).size, 46);
  assert.match(context.calls[1], /cursor=cursor-1/);
  assert.equal(context.WS.convCursor, 'cursor-2');
}

// 4. гонка: устаревший ответ после сброса не подменяет новый список
{
  const pending = [];
  const context = makeContext({ api: function (path) {
    return new Promise(function (resolve) { pending.push({ path: path, resolve: resolve }); });
  } });
  const first = context.wsLoadConversations({ reset: true });
  context.WS.convFilter = 'свежий';
  const second = context.wsLoadConversations({ reset: true });
  pending[1].resolve({ conversations: [{ id: 'fresh', title: 'Свежий разговор' }], total: 1, has_more: false, next_cursor: '' });
  pending[0].resolve({ conversations: conversations(0, 24), total: 55, has_more: true, next_cursor: 'old' });
  await Promise.all([first, second]);
  assert.equal(context.WS.convs.map(function (item) { return item.id; }).join(','), 'fresh', 'побеждает последний запрос');
  assert.equal(context.WS.convTotal, 1);
  assert.match(context.calls[1], /q=/);
}

// 5. поиск идёт на сервер; открытый чат остаётся доступным, даже если не попал в ответ
{
  const context = makeContext({ api: function () {
    return { conversations: [{ id: 'match', title: 'Отчёт по качеству' }], total: 1, has_more: false, next_cursor: '' };
  } });
  Object.assign(context.WS, { conv: { id: 'open', title: 'Длинный открытый разговор' }, convFilter: 'отчёт' });
  await context.wsLoadConversations({ reset: true });
  const shown = titles(context.box);
  assert.equal(shown.length, 2, 'совпадение плюс закреплённый открытый чат');
  assert.match(shown[0], /Длинный открытый разговор/);
  assert.match(context.$('#conv-summary').textContent, /найдено 1 чат/);
  assert.equal(context.$('#conv-more').hidden, true);
}

// 6. разметка, на которую опирается код
{
  ['conv-filter', 'conv-summary', 'conv-more', 'convs-list'].forEach(function (id) {
    const found = (html.match(new RegExp('id="' + id + '"', 'g')) || []).length;
    assert.equal(found, 1, id + ' встречается ровно один раз');
  });
}

// 7. чтение истории не прыгает при повторном polling с теми же сообщениями
{
  const from = app.indexOf('function wsMessageSignature(');
  const body = app.indexOf('\n}\n', app.indexOf('function renderMessages(', from)) + 3;
  const chatLog = new Box();
  chatLog.scrollTop = 120;
  const context = {
    WS: { conv: { id: 'conv-1', title: 'Чат' }, messages: [{ id: 'm1', author: 'user', body: 'старый вопрос' }], msgSignature: '' },
    $: function (selector) { return selector === '#chat-log' ? chatLog : node('div'); },
    el: function (tag, className, text) { const item = node(tag); item.className = className || ''; if (text != null) item.textContent = text; return item; },
    roleInfo: function () { return ['Архитектор', 'A']; }, agentName: function (id) { return id; },
    fmtTime: function () { return ''; }, ruStatus: function (state) { return state; },
    wsUpdateRecipient: function () {}, wsLatestConversationImage: function () { return null; },
  };
  vm.createContext(context);
  vm.runInContext(app.slice(from, body), context);
  context.renderMessages();
  assert.equal(chatLog.scrollTop, 120);
  const first = chatLog.children.slice();
  context.renderMessages();
  assert.ok(chatLog.children.every(function (child, index) { return child === first[index]; }));
  context.WS.messages = context.WS.messages.concat([{ id: 'm2', author: 'assistant', body: 'ответ' }]);
  chatLog.scrollTop = 900;
  context.renderMessages();
  assert.equal(chatLog.scrollTop, chatLog.scrollHeight);
}

console.log('Conversation list DOM regression passed: страницы по курсору, дедупликация, гонки, поиск, стабильный polling.');
}

main().catch(function (error) { console.error(error); process.exit(1); });
