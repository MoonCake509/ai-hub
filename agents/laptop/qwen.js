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

async function findTarget() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  const pages = list.filter(x => x.type === 'page' || x.type === 'webview');
  const t = pages.find(x => (x.url || '').includes('chat.qwen.ai'))
    || pages.find(x => (x.url || '').includes('app.asar/out/renderer'))
    || pages.find(x => (x.title || '').includes('Qwen'));
  if (!t) throw new Error(`Qwen webview не найден на порту ${PORT}. Запусти приложение с --remote-debugging-port=${PORT}`);
  return t;
}

async function connect() {
  const target = await findTarget();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const evaluate = expr => new Promise(res => {
    const myId = ++id;
    pending.set(myId, m => {
      if (m.result?.exceptionDetails) res({ error: m.result.exceptionDetails.text || 'js error' });
      else res({ value: m.result?.result?.value });
    });
    ws.send(JSON.stringify({ id: myId, method: 'Runtime.evaluate', params: { expression: expr, returnByValue: true, awaitPromise: true, userGesture: true } }));
  });
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
  const ta = document.querySelector('textarea');
  if (!ta) return 'NO_TEXTAREA';
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  ta.focus();
  setter.call(ta, ${JSON.stringify(text)});
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.dispatchEvent(new Event('change', { bubbles: true }));
  return 'SET';
})()`;

const SEND_SUBMIT = `(() => {
  const ta = document.querySelector('textarea');
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
  await evaluate(SEND_SET(text));
  for (let i = 0; i < 6; i++) {
    await sleep(700);
    const r = await evaluate(SEND_SUBMIT);
    await sleep(1200);
    const left = (await evaluate(`(() => { const t = document.querySelector('textarea'); return t ? t.value : null; })()`)).value;
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
  const { ws, evaluate } = await connect();
  try {
    if (cmd === 'ask') {
      const text = rest.join(' ');
      const before = (await evaluate(READ)).value || '';
      const sent = await submit(evaluate, text);
      if (sent === 'FAILED') throw new Error('Сообщение не отправилось (textarea не очистилась)');
      const after = await waitAnswer(evaluate, before);
      const ans = extractAnswer(after.slice(before.length)) || extractAnswer(after);
      console.log(ans || '(пусто)');
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
