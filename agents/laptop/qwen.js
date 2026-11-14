#!/usr/bin/env node
// qwen.js — управление десктоп-приложением Qwen через CDP (порт 9222).
// Команды:
//   ask "<текст>"   — отправить сообщение в текущий чат и дождаться ответа
//   read            — прочитать последний ответ
//   new             — начать новый чат
//   text            — весь текст переписки
//
// Требует: Qwen.exe запущен с --remote-debugging-port=9222
const PORT = Number(process.env.QWEN_CDP_PORT || 9222);
const COMPOSER = 'textarea:not([readonly]):not([aria-hidden="true"])';

async function findTarget(requireChat = false) {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const pages = list.filter(x => x.type === 'page' || x.type === 'webview');
  const chat = pages.find(x => {
    try { return new URL(x.url).origin === 'https://chat.qwen.ai'; }
    catch { return false; }
  });
  const t = chat || (!requireChat && (pages.find(x => (x.url || '').includes('app.asar/out/renderer'))
    || pages.find(x => (x.title || '').includes('Qwen'))));
  if (!t) throw new Error(`Qwen webview не найден на порту ${PORT}. Запусти приложение с --remote-debugging-port=${PORT}`);
  return t;
}

async function connect(requireChat = false) {
  const target = await findTarget(requireChat);
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const command = (method, params = {}) => new Promise(res => {
    const myId = ++id;
    pending.set(myId, m => res(m.error ? { error: m.error.message || String(m.error) } : { result: m.result }));
    ws.send(JSON.stringify({ id: myId, method, params }));
  });
  const evaluate = expr => new Promise(res => {
    const myId = ++id;
    pending.set(myId, m => {
      if (m.result?.exceptionDetails) {
        res({ error: m.result.exceptionDetails.exception?.description || m.result.exceptionDetails.text });
      }
      else res({ value: m.result?.result?.value });
    });
    ws.send(JSON.stringify({ id: myId, method: 'Runtime.evaluate', params: { expression: expr, returnByValue: true, awaitPromise: true, userGesture: true } }));
  });
  // Окно в фоне перестаёт перерисовывать поток: живой DOM отдаёт обрезанный ответ,
  // хотя модель уже договорила. Держим страницу видимой и активной на время прогона.
  await command('Page.bringToFront');
  await command('Emulation.setFocusEmulationEnabled', { enabled: true });
  await command('Page.setWebLifecycleState', { state: 'active' });
  return { ws, evaluate };
}

const READ = `(() => { const m = document.querySelector('main') || document.body; return m.innerText; })()`;

// Извлекает последний ответ ассистента из текста переписки
function extractAnswer(mainText) {
  const lines = mainText.split('\n').map(s => s.trim());
  // Служебные строки: маркер мышления и дисклеймер (встречается в разных формулировках).
  const noise = /^(thinking|thinking completed|thinking\.\.\.|copy|edit|regenerate|share|auto|i prefer this response)$/i;
  const disclaimer = /(ai-generated )?content may not be accurate/i;
  const idx = lines.map(l => l.toLowerCase()).lastIndexOf('thinking completed');
  if (idx < 0) return '';
  const out = [];
  for (const l of lines.slice(idx + 1)) {
    if (!l) continue;
    // Новый блок «Thinking» — начало следующей генерации, текущий ответ закончился.
    if (/^thinking$/i.test(l)) break;
    if (noise.test(l)) continue;
    if (disclaimer.test(l)) continue;
    out.push(l);
  }
  return out.join('\n').trim();
}

const SEND_SET = text => `(() => {
  const ta = document.querySelector(${JSON.stringify(COMPOSER)});
  if (!ta) return 'NO_TEXTAREA';
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  ta.focus();
  setter.call(ta, ${JSON.stringify(text)});
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.dispatchEvent(new Event('change', { bubbles: true }));
  return 'SET';
})()`;

const SEND_SUBMIT = `(() => {
  const ta = document.querySelector(${JSON.stringify(COMPOSER)});
  if (!ta) return 'NO_TEXTAREA';
  if (!ta.value.trim()) return 'ALREADY_EMPTY';
  const btn = [...document.querySelectorAll('button')].find(b => /^send$/i.test(b.getAttribute('aria-label') || '') || /^send$/i.test((b.innerText || '').trim()));
  if (btn && !btn.disabled) { btn.click(); return 'CLICK'; }
  ta.focus();
  ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
  ta.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
  return 'ENTER';
})()`;

async function submit(evaluate, text) {
  if ((await evaluate(SEND_SET(text))).value !== 'SET') return 'FAILED';
  for (let i = 0; i < 6; i++) {
    await sleep(700);
    const r = await evaluate(SEND_SUBMIT);
    await sleep(1200);
    const left = (await evaluate(`document.querySelector(${JSON.stringify(COMPOSER)})?.value ?? null`)).value;
    if (left === '' || left === null) return r.value;
  }
  return 'FAILED';
}

const NEW_CHAT = `(() => {
  const btns = [...document.querySelectorAll('button, a')];
  const b = btns.find(x => /new chat|новый чат/i.test((x.innerText || '') + ' ' + (x.getAttribute('aria-label') || '')));
  if (b) { b.click(); return 'CLICKED'; }
  location.href = 'https://chat.qwen.ai/';
  return 'NAVIGATED';
})()`;

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function waitFresh(evaluate, timeoutMs = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await sleep(2000);
    const { value } = await evaluate(READ);
    const txt = (value || '').trim();
    const ready = (await evaluate(`(() => !!document.querySelector('textarea'))()`)).value;
    if (ready && txt.length < 300 && !/Thinking completed/i.test(txt)) return true;
  }
  return false;
}

async function waitAnswer(evaluate, beforeText, timeoutMs = 240000) {
  const t0 = Date.now();
  let last = '', stable = 0;
  while (Date.now() - t0 < timeoutMs) {
    await sleep(2500);
    const { value } = await evaluate(READ);
    const txt = (value || '').replace(/\r/g, '');
    const done = /Thinking completed/i.test(txt) && txt.length > beforeText.length + 2;
    const stopped = (await evaluate(`(() => [...document.querySelectorAll('button')].some(b => /stop|остановить/i.test((b.getAttribute('aria-label')||'') + (b.innerText||''))))()`)).value;
    if (txt !== last) { last = txt; stable = 0; continue; }
    stable += 1;
    if (stable >= 2 && done && !stopped) return txt;
  }
  return last;
}

function chatState(composerSelector) {
  const answers = document.querySelectorAll('.response-message-content.phase-answer');
  const stop = [...document.querySelectorAll('button')].some(button =>
    /^(stop|остановить)(?:\s|$)/i.test((button.getAttribute('aria-label') || '').trim()));
  return {
    count: answers.length,
    text: answers[answers.length - 1]?.innerText || '',
    model: document.querySelector('[aria-label="Select Model"]')?.innerText.trim() || '',
    generating: Boolean(document.querySelector('.qwen-chat-message-awaiting-response')) || stop,
    stop,
    draft: document.querySelector(composerSelector)?.value || '',
  };
}

// Маркер — единственное доказательство, что ответ дочитан до конца.
function requireEndMarker(text, endMarker) {
  const markerLine = text.lastIndexOf('\n' + endMarker);
  if (markerLine < 0 || text.slice(markerLine + 1).trim() !== endMarker) {
    throw new Error('Qwen Chat answer is incomplete: the response finished without the end marker');
  }
}

async function readStructuredAnswer(expectedCount, expectedText, endMarker) {
  const answers = document.querySelectorAll('.response-message-content.phase-answer');
  const answer = answers[answers.length - 1];
  if (answers.length !== expectedCount || answer?.innerText !== expectedText) {
    throw new Error('Qwen Chat answer changed while being read');
  }

  let output = answer.innerText;
  for (const block of answer.querySelectorAll('pre.qwen-markdown-code')) {
    if (block.innerText.includes(endMarker)) throw new Error('Qwen Chat put the end marker inside a code block');
    const button = block.querySelector('button[aria-label="Copy"], button[aria-label="Copy code"]');
    const clipboard = navigator.clipboard;
    if (!button || !clipboard) throw new Error('Qwen Chat code copy control is unavailable');
    const original = Object.getOwnPropertyDescriptor(clipboard, 'writeText');
    let copied;
    // Capture the app's original code, including lines Monaco has not rendered.
    // Intercept this button's clipboard write without replacing the system clipboard.
    Object.defineProperty(clipboard, 'writeText', {
      configurable: true,
      value: async text => { copied = String(text); },
    });
    try {
      button.click();
      for (let i = 0; i < 10 && copied === undefined; i++) {
        await new Promise(resolve => setTimeout(resolve, 50));
      }
    } finally {
      if (original) Object.defineProperty(clipboard, 'writeText', original);
      else delete clipboard.writeText;
    }
    if (copied === undefined || !block.innerText || !output.includes(block.innerText)) {
      throw new Error('Could not read original code from Qwen Chat');
    }
    output = output.replace(block.innerText, () => copied);
  }
  const markerLine = output.lastIndexOf('\n' + endMarker);
  if (markerLine < 0 || output.slice(markerLine + 1).trim() !== endMarker) {
    throw new Error('Qwen Chat answer has no separate end marker');
  }
  // Патч отдаём в LF и с завершающим переводом строки: без него git apply зовёт его битым.
  return output.slice(0, markerLine).replace(/\r\n?/g, '\n').replace(/\n+$/, '') + '\n';
}

// Qwen Desktop имеет собственную шкалу режима: Auto, Thinking, Fast.
// Уровни AI Hub преобразуются в неё до отправки, а выбранный пункт перечитывается.
const QWEN_THINKING_MODES = new Set(['auto', 'thinking', 'fast']);

async function switchThinking(read, requestedMode = 'auto') {
  const want = String(requestedMode || 'auto').trim().toLowerCase();
  if (!QWEN_THINKING_MODES.has(want)) throw new Error(`Unsupported Qwen Desktop mode: ${requestedMode}`);
  for (let round = 0; round < 4; round++) {
    const current = (await read(`(document.querySelector('.qwen-thinking-selector .qwen-chat-v2-dropdown-menu-select-label')?.innerText || '').trim()`)).toLowerCase();
    if (current === want) return current;
    const opened = await read(`(() => {
      const b = document.querySelector('.qwen-thinking-selector .qwen-chat-v2-dropdown-menu-select');
      if (!b) return 'NO_SELECTOR';
      b.click();
      return 'OPEN';
    })()`);
    if (opened === 'NO_SELECTOR') throw new Error('Qwen Desktop thinking selector is unavailable');
    await sleep(250);
    let clicked = 'NOT_FOUND';
    for (let wait = 0; wait < 8 && clicked === 'NOT_FOUND'; wait++) {
      clicked = await read(`(() => {
        const w = ${JSON.stringify(want)};
        const items = [...document.querySelectorAll('.qwen-chat-v2-dropdown-menu-popup [role="menuitem"]')];
        const item = items.find(e => (e.innerText || '').trim().toLowerCase() === w);
        if (!item) return 'NOT_FOUND';
        item.click();
        return 'CLICKED';
      })()`);
      if (clicked === 'NOT_FOUND') await sleep(150);
    }
    if (clicked === 'NOT_FOUND') throw new Error(`Qwen Desktop mode ${requestedMode} is not available`);
    await sleep(350);
  }
  const final = (await read(`(document.querySelector('.qwen-thinking-selector .qwen-chat-v2-dropdown-menu-select-label')?.innerText || '').trim()`)).toLowerCase();
  if (final !== want) throw new Error(`Qwen Desktop mode mismatch: expected ${want}, selected ${final || '(unknown)'}`);
  return final;
}

// Переключение модели через интерфейс. Воркер не полагается на то, что приложение
// запомнило прошлый выбор: кликает селектор, затем перечитывает название из триггера.
async function switchModel(read, want) {
  const wantText = want.toLowerCase();
  const items = `[...document.querySelectorAll('.wms-list__name-text, [role="menuitem"], [role="option"], li')]`;
  for (let round = 0; round < 4; round++) {
    const current = (await read(`(document.querySelector('[aria-label="Select Model"]')?.innerText || '').trim()`)).toLowerCase();
    if (current === wantText) return true;
    for (let open = 0; open < 3; open++) {
      const found = await read(`(() => {
        const w = ${JSON.stringify(wantText)};
        return ${items}.filter(e => { const t = (e.innerText || '').trim().toLowerCase(); return t === w || t.split('\\n')[0].trim().toLowerCase() === w; }).length;
      })()`);
      if (found) break;
      await read(`(() => { const b = document.querySelector('[aria-label="Select Model"]'); if (b) b.click(); return 'OPEN'; })()`);
      await sleep(1200);
    }
    const clicked = await read(`(() => {
      const w = ${JSON.stringify(wantText)};
      const list = ${items}
        .filter(e => { const t = (e.innerText || '').trim().toLowerCase(); return t === w || t.split('\\n')[0].trim().toLowerCase() === w; })
        .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
      if (!list.length) return 'NOT_FOUND';
      (list[0].closest('li, [role="option"], [role="menuitem"]') || list[0]).click();
      return 'CLICKED';
    })()`);
    if (clicked === 'NOT_FOUND') { await sleep(800); continue; }
    await sleep(1500);
  }
  const final = (await read(`(document.querySelector('[aria-label="Select Model"]')?.innerText || '').trim()`)).toLowerCase();
  return final === wantText;
}

async function askStructured(evaluate, expectedModel, prompt, requestedMode = 'auto') {
  async function read(expression) {
    const result = await evaluate(expression);
    if (result.error) throw new Error(result.error);
    return result.value;
  }

  const stateExpression = `(${chatState})(${JSON.stringify(COMPOSER)})`;
  let before = await read(stateExpression);
  // Новый чат сбрасывает модель на дефолтную, поэтому модель выставляем сами,
  // а не надеемся на состояние приложения.
  if (expectedModel && before.model.toLowerCase() !== expectedModel.toLowerCase()) {
    const switched = await switchModel(read, expectedModel);
    if (!switched) throw new Error(`Qwen Chat could not select model ${expectedModel}: selected ${before.model || '(unknown)'}`);
    before = await read(stateExpression);
  }
  const model = before.model.toLowerCase();
  if (!model || (expectedModel && model !== expectedModel.toLowerCase())) {
    throw new Error(`Qwen Chat model mismatch: expected ${expectedModel || 'a named model'}, selected ${before.model || '(unknown)'}`);
  }
  if (before.draft || before.stop) throw new Error('Qwen Chat is busy or contains an unsent draft');
  const mode = await switchThinking(read, requestedMode);
  const endMarker = `AIHUB-END-${crypto.randomUUID().slice(0, 8)}`;
  const framedPrompt = `${prompt}\n\nAfter the complete answer, close any code fence and put this marker on its own final line: ${endMarker}`;
  const sent = await submit(evaluate, framedPrompt);
  if (!['CLICK', 'ENTER', 'ALREADY_EMPTY'].includes(sent)) throw new Error('Qwen Chat message was not submitted');

  // Окно в фоне не дорисовывает поток: в живом DOM ответ обрывается на середине,
  // а в истории чата он уже целый. Перечитываем историю, но требуем тот же маркер.
  async function rereadFromHistory() {
    await read(`(() => { setTimeout(() => location.reload(), 50); return 'RELOADING'; })()`);
    let loaded = false;
    let idle = 0;
    for (let i = 0; i < 30; i++) {
      await sleep(1000);
      const s = await read(`(() => ({ ready: document.readyState === 'complete', count: document.querySelectorAll('.response-message-content.phase-answer').length }))()`);
      if (s.count > before.count) { loaded = true; break; }
      // Страница загрузилась, но нового ответа в истории нет — значит генерацию
      // прервали (Stop) до того, как появился ответ. Ждать дальше незачем.
      if (s.ready && ++idle >= 4) break;
    }
    if (!loaded) throw new Error('Qwen Chat answer is incomplete: the generation stopped before the answer was complete');
    const state = await read(stateExpression);
    if (!state.model || state.model.toLowerCase() !== model) {
      throw new Error(`Qwen Chat model mismatch after reload: expected ${model}, selected ${state.model || '(unknown)'}`);
    }
    requireEndMarker(state.text, endMarker);
    const output = await read(`(${readStructuredAnswer})(${state.count}, ${JSON.stringify(state.text)}, ${JSON.stringify(endMarker)})`);
    return { output, model, model_label: state.model, thinking_mode: mode };
  }

  const sentAt = Date.now();
  let last = '';
  let stable = 0;
  for (;;) {
    await sleep(2000);
    const state = await read(stateExpression);
    if (state.model.toLowerCase() !== model) throw new Error('Qwen Chat model changed during the run');
    if (state.count > before.count + 1) throw new Error('Qwen Chat received another turn during the run');
    const finished = state.text.trimEnd().endsWith('\n' + endMarker);
    if (state.text !== last) {
      last = state.text;
      stable = 0;
      continue;
    }
    stable += 1;
    if (finished && stable >= 4 && state.count === before.count + 1) {
      const output = await read(`(${readStructuredAnswer})(${state.count}, ${JSON.stringify(state.text)}, ${JSON.stringify(endMarker)})`);
      return { output, model, model_label: before.model, thinking_mode: mode };
    }
    // Ответ не растёт, «стоп» нет, маркера нет. Так выглядит и оборванная генерация,
    // и ответ, который окно не дорисовало: и то и другое перечитываем из истории.
    // Порог по времени — чтобы не принять за обрыв самое начало ответа.
    if (!finished && !state.stop && stable >= 3 && Date.now() - sentAt > 20000) {
      return await rereadFromHistory();
    }
  }
}

function tail(mainText, n = 1500) {
  const lines = mainText.split('\n').map(s => s.trim()).filter(Boolean);
  return lines.slice(-n).join('\n');
}

(async () => {
  const [cmd, ...rest] = process.argv.slice(2);
  // Жёсткий предел: node не должен висеть из-за незакрытого WebSocket или зависшего UI.
  const hardMs = Number(process.env.QWEN_HARD_TIMEOUT || 200000);
  const watchdog = setTimeout(() => {
    console.error(`ERR hard timeout ${hardMs}ms`);
    process.exit(3);
  }, hardMs);
  const { ws, evaluate } = await connect(cmd === 'ask-json');
  try {
    if (cmd === 'ask') {
      const text = rest.join(' ');
      const before = (await evaluate(READ)).value || '';
      const sent = await submit(evaluate, text);
      if (sent === 'FAILED') throw new Error('Сообщение не отправилось (textarea не очистилась)');
      const after = await waitAnswer(evaluate, before);
      const ans = extractAnswer(after.slice(before.length)) || extractAnswer(after);
      console.log(ans || '(пусто)');
    } else if (cmd === 'ask-json') {
      const [expectedModel, ...tail] = rest;
      let requestedMode = 'auto';
      // Старый вызов (модель, промпт) должен остаться рабочим: режим передаём
      // только если вторым аргументом идёт известное значение.
      if (tail.length && QWEN_THINKING_MODES.has(String(tail[0]).trim().toLowerCase())) {
        requestedMode = String(tail.shift()).trim().toLowerCase();
      }
      const result = await askStructured(evaluate, expectedModel, tail.join(' '), requestedMode);
      console.log(JSON.stringify(result));
    } else if (cmd === 'read') {
      const t = (await evaluate(READ)).value || '';
      console.log(extractAnswer(t) || '(пусто)');
    } else if (cmd === 'new') {
      const r = await evaluate(NEW_CHAT);
      await waitFresh(evaluate);
      console.log(r.value + ' / чат очищен');
    } else if (cmd === 'text') {
      console.log((await evaluate(READ)).value || '');
    } else {
      console.log('usage: qwen.js ask "<текст>" | read | new | text');
    }
    clearTimeout(watchdog);
  } finally {
    ws.close();
  }
  process.exit(0);
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
