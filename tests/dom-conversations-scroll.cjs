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
    tag: tag, className: '', title: '', textContent: '', children: [], scrollTop: 0,
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

function makeContext(list, activeIndex, scrollTop) {
  const box = new Box();
  if (scrollTop != null) box.scrollTop = scrollTop;
  const fields = new Map();
  const $ = function (selector) {
    if (selector === '#convs-list') return box;
    if (!fields.has(selector)) fields.set(selector, node('div'));
    return fields.get(selector);
  };
  const context = {
    WS: { convs: list, conv: list[activeIndex] || null, convFilter: '', convLimit: 24,
      convSignature: '', convShownId: list[activeIndex] ? list[activeIndex].id : '' },
    $: $,
    el: function (tag, className, text) { const item = node(tag); item.className = className || ''; if (text != null) item.textContent = text; return item; },
    action: function (handler) { return handler; },
    wsOpenConversation: function () {},
    box: box,
  };
  vm.createContext(context);
  vm.runInContext(helpers, context);
  return context;
}

function conversations(count) {
  return Array.from({ length: count }, function (_, index) {
    return { id: 'conv-' + index, title: 'Разбор архитектуры проекта и длинное название разговора номер ' + index + ' — продолжение проверки' };
  });
}

function rows(box) {
  return box.children.filter(function (child) { return child.className.split(' ')[0] === 'conv'; });
}
function titles(box) {
  return rows(box).map(function (child) { return child.children[1].textContent; });
}

{
  const list = conversations(60);
  const context = makeContext(list, 57);
  context.wsRenderConversations();
  const buttons = rows(context.box);
  assert.equal(buttons.length, 25, '24 видимых строки плюс закреплённый открытый чат');
  assert.match(buttons[0].className, /active/, 'открытый чат закреплён первым и доступен');
  assert.equal(buttons[0].title, list[57].title, 'в подсказке закреплённой строки полное название');
  assert.match(context.$('#conv-summary').textContent, /60 чатов/);
  assert.match(context.$('#conv-summary').textContent, /показаны первые 24/);
  assert.equal(context.$('#conv-more').hidden, false);
  assert.match(context.$('#conv-more').textContent, /Показать ещё/);
  assert.equal(buttons[0].children[1].className, 'conv-text', 'обрезается ячейка названия, а не строка');
}

{
  const list = conversations(60);
  const context = makeContext(list, 57, 180);
  context.wsRenderConversations();
  const before = context.box.children.slice();
  context.box.scrollTop = 180;
  context.wsRenderConversations();
  context.wsRenderConversations();
  assert.equal(context.box.children.length, before.length);
  assert.ok(context.box.children.every(function (child, index) { return child === before[index]; }), 'одинаковый результат polling сохраняет те же DOM-узлы');
  assert.equal(context.box.scrollTop, 180, 'позиция читателя переживает polling');
}

{
  const list = conversations(60);
  list[3].title = 'Отчёт по качеству канала';
  list[8].title = 'Отчёт по оплате';
  const context = makeContext(list, 57);
  context.WS.convFilter = 'отчёт';
  context.wsRenderConversations();
  const shown = titles(context.box);
  assert.equal(shown.length, 3, 'два совпадения плюс закреплённый открытый чат');
  assert.match(shown[0], /номер 57/, 'открытый чат остаётся в списке во время поиска');
  assert.ok(shown.some(function (title) { return title.indexOf('качеству') >= 0; }));
  assert.match(context.$('#conv-summary').textContent, /найдено 2 чата из 60 чатов/);
  context.WS.convFilter = 'ничего-нет';
  context.wsRenderConversations();
  assert.equal(titles(context.box).length, 1, 'без совпадений остаётся только закреплённый открытый чат');
  assert.match(context.$('#conv-summary').textContent, /найдено 0 чатов из 60 чатов/);
}

{
  const list = conversations(60);
  const context = makeContext(list, 0);
  context.wsRenderConversations();
  context.WS.convLimit = 48;
  context.wsRenderConversations();
  assert.equal(titles(context.box).length, 48);
  context.WS.convLimit = 96;
  context.wsRenderConversations();
  assert.equal(titles(context.box).length, 60);
  assert.equal(context.$('#conv-more').hidden, true, 'кнопка исчезает, когда показано всё');
}

{
  ['conv-filter', 'conv-summary', 'conv-more', 'convs-list'].forEach(function (id) {
    const found = (html.match(new RegExp('id="' + id + '"', 'g')) || []).length;
    assert.equal(found, 1, id + ' встречается ровно один раз');
  });
  assert.match(html, /class="convs-list"/);
}

{
  const from = app.indexOf('function wsMessageSignature(');
  const body = app.indexOf('\n}\n', app.indexOf('function renderMessages(', from)) + 3;
  assert.ok(from >= 0 && body > from, 'функции сообщений найдены');
  const chatLog = new Box();
  chatLog.scrollHeight = 1000;
  chatLog.clientHeight = 200;
  chatLog.scrollTop = 120;
  const context = {
    WS: { conv: { id: 'conv-1', title: 'Чат' }, messages: [{ id: 'm1', author: 'user', body: 'старый вопрос' }], msgSignature: '' },
    $: function (selector) { return selector === '#chat-log' ? chatLog : node('div'); },
    el: function (tag, className, text) { const item = node(tag); item.className = className || ''; if (text != null) item.textContent = text; return item; },
    roleInfo: function () { return ['Архитектор', 'A']; },
    agentName: function (id) { return id; },
    fmtTime: function () { return ''; },
    ruStatus: function (state) { return state; },
    wsUpdateRecipient: function () {},
    wsLatestConversationImage: function () { return null; },
  };
  vm.createContext(context);
  vm.runInContext(app.slice(from, body), context);
  context.renderMessages();
  assert.equal(chatLog.scrollTop, 120, 'читатель не внизу не отбрасывается ни в начало, ни в конец');
  const first = chatLog.children.slice();
  context.renderMessages();
  assert.ok(chatLog.children.every(function (child, index) { return child === first[index]; }), 'повтор с теми же сообщениями не перестраивает лог');
  context.WS.messages = context.WS.messages.concat([{ id: 'm2', author: 'assistant', body: 'ответ' }]);
  chatLog.scrollTop = 900;
  context.renderMessages();
  assert.equal(chatLog.scrollTop, chatLog.scrollHeight, 'у нижней кромки новый ответ прокручивает вниз');
}

console.log('Conversation list DOM regression passed: 60 чатов, свой scroll, стабильный polling, поиск и закреплённый открытый чат.');
