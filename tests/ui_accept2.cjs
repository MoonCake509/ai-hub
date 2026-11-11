'use strict';
// Real-browser acceptance for the hub UI (CORE-03): boot with token, projects, conversations, reconciliation.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const CHROME = process.env.CHROME_BIN || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = process.env.AIHUB_BASE || 'http://127.0.0.1:8000';
const TOKEN = (() => {
  if (process.env.AIHUB_TOKEN_FILE) return fs.readFileSync(process.env.AIHUB_TOKEN_FILE, 'utf8').trim();
  const envFile = process.env.AIHUB_ENV_FILE || 'R:\AI-Hub\infra\.env';
  const line = fs.readFileSync(envFile, 'utf8').split(/?
/).find(l => l.startsWith('HUB_TOKEN='));
  if (!line) throw new Error('HUB_TOKEN not found');
  return line.slice('HUB_TOKEN='.length).trim().replace(/^["']|["']$/g, '');
})();
const PORT = 9400 + Math.floor(Math.random() * 300);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'hubacc-'));
const shots = process.env.SHOT_DIR || path.join(os.tmpdir(), 'hubacc-shots');
fs.mkdirSync(shots, { recursive: true });

const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, '--window-size=1440,960', 'about:blank'],
  { stdio: 'ignore' });

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function json(pathname) {
  for (let i = 0; i < 100; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}${pathname}`);
      if (res.ok) return await res.json();
    } catch (e) {}
    await sleep(250);
  }
  throw new Error('devtools not ready');
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
    ws.onerror = () => reject(new Error('ws error'));
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
  const res = r.result || {};
  if (res.exceptionDetails) return { __error: JSON.stringify(res.exceptionDetails).slice(0, 300) };
  return res.result ? res.result.value : undefined;
}

(async () => {
  const out = { ok: false };
  try {
    const list = await json('/json/list');
    let page = list.find(t => t.type === 'page');
    if (!page) page = await json('/json/new?about:blank');
    const cli = await connect(page.webSocketDebuggerUrl);
    await cli.send('Page.enable');
    await cli.send('Runtime.enable');
    await cli.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 960, deviceScaleFactor: 1, mobile: false });

    await cli.send('Page.navigate', { url: BASE + '/' });
    await sleep(2000);
    await evaluate(cli, `localStorage.setItem('aihub_token', ${JSON.stringify(TOKEN)}); 'ok'`);
    await cli.send('Page.navigate', { url: BASE + '/' });
    await sleep(4500);

    out.projectOptions = await evaluate(cli, `document.querySelectorAll('#projects option, #proj-list .item, .project').length`);
    out.convRows = await evaluate(cli, `document.querySelectorAll('#convs-list .conv, #convs-list > *').length`);
    out.convSummary = await evaluate(cli, `(document.querySelector('#conv-summary')||{}).textContent || ''`);
    out.reconCount = await evaluate(cli, `document.querySelectorAll('#recon-list > *').length`);
    out.reconStatus = await evaluate(cli, `(document.querySelector('#recon-status')||{}).textContent || ''`);
    out.pageText = await evaluate(cli, `document.body.innerText.slice(0, 300)`);

    // open first reconciliation case if present
    out.caseOpened = await evaluate(cli, `(() => {
      const first = document.querySelector('#recon-list > *');
      if (!first) return null;
      const btn = first.querySelector('button') || first;
      btn.click();
      return (first.innerText || '').slice(0, 80);
    })()`);
    await sleep(2000);
    out.detailVisible = await evaluate(cli, `(() => {
      const d = document.querySelector('#recon-detail');
      if (!d) return false;
      return !d.hidden && (d.innerText || '').trim().length > 0;
    })()`);

    const shot = await cli.send('Page.captureScreenshot', { format: 'png' });
    const file = path.join(shots, 'core03-recon.png');
    fs.writeFileSync(file, Buffer.from(shot.result.data, 'base64'));
    out.screenshot = file;

    out.ok = out.convRows > 0 && out.reconCount > 0 && out.detailVisible === true;
    cli.close();
  } catch (e) {
    out.error = String((e && e.message) || e);
  } finally {
    try { chrome.kill(); } catch (e) {}
  }
  console.log(JSON.stringify(out, null, 2));
  process.exit(out.ok ? 0 : 1);
})();
