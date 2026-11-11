'use strict';
// Real-browser acceptance for the hub UI: token boot, projects, conversations, reconciliation panel.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const CHROME = process.env.CHROME_BIN || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = process.env.AIHUB_BASE || 'http://127.0.0.1:8000';
const TOKEN = process.env.AIHUB_TOKEN || '';
const PORT = 9333 + Math.floor(Math.random() * 200);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'hubacc-'));
const shots = process.env.SHOT_DIR || path.join(os.tmpdir(), 'hubacc-shots');
fs.mkdirSync(shots, { recursive: true });

const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, '--window-size=1366,900', 'about:blank'],
  { stdio: 'ignore' });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function json(pathname) {
  for (let i = 0; i < 80; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}${pathname}`);
      if (res.ok) return await res.json();
    } catch (e) { /* not up yet */ }
    await sleep(250);
  }
  throw new Error('chrome devtools not ready');
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    let id = 0;
    const pending = new Map();
    ws.onmessage = ev => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    };
    ws.onerror = e => reject(new Error('ws error'));
    ws.onopen = () => resolve({
      send(method, params = {}) {
        const mid = ++id;
        ws.send(JSON.stringify({ id: mid, method, params }));
        return new Promise(res => pending.set(mid, res));
      },
      close() { try { ws.close(); } catch (e) {} },
    });
  });
}

async function evaluate(cli, expr) {
  const r = await cli.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 300));
  return r.result && r.result.result ? r.result.result.value : undefined;
}

(async () => {
  const out = { ok: false };
  try {
    const list = await json('/json/list');
    let page = list.find(t => t.type === 'page');
    if (!page) page = await json('/json/new?' + encodeURIComponent('about:blank'));
    const cli = await connect(page.webSocketDebuggerUrl);
    await cli.send('Page.enable');
    await cli.send('Runtime.enable');
    await cli.send('Emulation.setDeviceMetricsOverride', { width: 1366, height: 900, deviceScaleFactor: 1, mobile: false });

    await cli.send('Page.navigate', { url: BASE + '/' });
    await sleep(2500);
    await evaluate(cli, `localStorage.setItem('aihub_token', ${JSON.stringify(TOKEN)}); 'ok'`);
    await cli.send('Page.navigate', { url: BASE + '/' });
    await sleep(3500);

    out.projects = await evaluate(cli, `document.querySelectorAll('#projects .project, .project-item, [data-project]').length`);
    out.bootOk = await evaluate(cli, `!document.body.innerText.includes('Введите токен') || document.body.innerText.length > 400`);
    out.convRows = await evaluate(cli, `document.querySelectorAll('#conv-list .conv, .conv-item, [data-conv]').length`);
    out.hasRecon = await evaluate(cli, `!!document.querySelector('#recon-panel, [data-panel="reconciliation"]')`);
    out.textLen = await evaluate(cli, `document.body.innerText.length`);

    // open reconciliation panel if it is a hidden section
    const opened = await evaluate(cli, `(() => {
      const b = [...document.querySelectorAll('button, a')].find(x => /сверк|расхожден|recon/i.test(x.textContent||''));
      if (b) { b.click(); return b.textContent.trim().slice(0,40); } return null;
    })()`);
    await sleep(2500);
    out.reconButton = opened;
    out.reconCases = await evaluate(cli, `document.querySelectorAll('#recon-list .recon-case, .recon-case, [data-case]').length`);
    out.reconText = await evaluate(cli, `(document.querySelector('#recon-panel')||{}).innerText ? document.querySelector('#recon-panel').innerText.slice(0,200) : ''`);

    const shot = await cli.send('Page.captureScreenshot', { format: 'png' });
    const file = path.join(shots, 'core03-ui.png');
    fs.writeFileSync(file, Buffer.from(shot.result.data, 'base64'));
    out.screenshot = file;
    out.ok = out.textLen > 400 && out.projects >= 3;
    cli.close();
  } catch (e) {
    out.error = String(e && e.message || e);
  } finally {
    try { chrome.kill(); } catch (e) {}
  }
  console.log(JSON.stringify(out, null, 2));
  process.exit(out.ok ? 0 : 1);
})();
