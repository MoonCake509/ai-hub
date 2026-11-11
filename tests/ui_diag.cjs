'use strict';
// Diagnose why the reconciliation panel stays in "loading" in a real browser.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const CHROME = process.env.CHROME_BIN || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = process.env.AIHUB_BASE || 'http://127.0.0.1:8000';
const NL = String.fromCharCode(10);
const envFile = 'R:\\AI-Hub\\infra\\.env';
const line = fs.readFileSync(envFile, 'utf8').split(NL).find(l => l.startsWith('HUB_TOKEN='));
const TOKEN = line.slice('HUB_TOKEN='.length).trim().replace(/^"|"$/g, '');
const PORT = 9800 + Math.floor(Math.random() * 150);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'hubdbg-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-debugging-port=' + PORT,
  '--user-data-dir=' + profile, 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function json(p) {
  for (let i = 0; i < 100; i++) {
    try { const r = await fetch('http://127.0.0.1:' + PORT + p); if (r.ok) return await r.json(); } catch (e) {}
    await sleep(250);
  }
  throw new Error('no devtools');
}
function connect(url) {
  return new Promise((res, rej) => {
    const ws = new WebSocket(url); let id = 0; const pend = new Map(); const events = [];
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); }
      else if (m.method) events.push(m);
    };
    ws.onopen = () => res({ send: (method, params = {}) => { const mid = ++id; ws.send(JSON.stringify({ id: mid, method, params })); return new Promise(r => pend.set(mid, r)); }, events, close: () => ws.close() });
    ws.onerror = () => rej(new Error('ws'));
  });
}
async function ev(cli, expr) {
  const r = await cli.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  const res = r.result || {};
  if (res.exceptionDetails) return { error: JSON.stringify(res.exceptionDetails).slice(0, 400) };
  return res.result ? res.result.value : undefined;
}
(async () => {
  const out = {};
  let cli = null;
  try {
    const list = await json('/json/list');
    const page = list.find(t => t.type === 'page') || await json('/json/new?about:blank');
    cli = await connect(page.webSocketDebuggerUrl);
    await cli.send('Page.enable'); await cli.send('Runtime.enable'); await cli.send('Network.enable');
    await cli.send('Page.navigate', { url: BASE + '/' }); await sleep(1500);
    await ev(cli, 'localStorage.setItem("aihub_token", ' + JSON.stringify(TOKEN) + '); 1');
    await cli.send('Page.navigate', { url: BASE + '/' }); await sleep(4000);

    out.apiFromPage = await ev(cli, `(async () => {
      try {
        const r = await fetch('/workspace/reconciliation/cases?limit=3', { headers: { Authorization: 'Bearer ' + localStorage.getItem('aihub_token') } });
        const t = await r.text();
        return { status: r.status, body: t.slice(0, 200) };
      } catch (e) { return { error: String(e) }; }
    })()`);
    out.reconStatus = await ev(cli, '(document.querySelector("#recon-status")||{}).textContent');
    out.hasToken = await ev(cli, '!!localStorage.getItem("aihub_token")');
    await sleep(3000);
    out.reconStatus2 = await ev(cli, '(document.querySelector("#recon-status")||{}).textContent');
    out.reconCount2 = await ev(cli, 'document.querySelectorAll("#recon-list > *").length');
    out.consoleErrors = cli.events.filter(e => e.method === 'Runtime.consoleAPICalled' && e.params.type === 'error')
      .map(e => (e.params.args || []).map(a => a.value || a.description || '').join(' ').slice(0, 200)).slice(0, 5);
    out.failedRequests = cli.events.filter(e => e.method === 'Network.loadingFailed').map(e => (e.params.errorText || '') + ' ' + (e.params.blockedReason || '')).slice(0, 5);
    out.responses = cli.events.filter(e => e.method === 'Network.responseReceived')
      .map(e => e.params.response.status + ' ' + e.params.response.url.replace(BASE, '')).filter(s => !s.includes('.png')).slice(0, 15);
  } catch (e) { out.error = String(e && e.message || e); }
  finally { try { cli && cli.close(); } catch (e) {} try { chrome.kill(); } catch (e) {} }
  console.log(JSON.stringify(out, null, 2));
})();
