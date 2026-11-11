'use strict';
// End-to-end browser check: token boot, pick a project, read its conversations, open a reconciliation case.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const CHROME = process.env.CHROME_BIN || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const BASE = process.env.AIHUB_BASE || 'http://127.0.0.1:8000';
const NL = String.fromCharCode(10);
const line = fs.readFileSync('R:\\AI-Hub\\infra\\.env', 'utf8').split(NL).find(l => l.startsWith('HUB_TOKEN='));
const TOKEN = line.slice('HUB_TOKEN='.length).trim().replace(/^"|"$/g, '');
const PROJECT = process.env.PROJECT_NAME || 'UI-приёмка раскладки';
const PORT = 9600 + Math.floor(Math.random() * 200);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'hubacc-'));
const shots = process.env.SHOT_DIR || path.join(os.tmpdir(), 'hubacc-shots');
fs.mkdirSync(shots, { recursive: true });
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run', '--remote-debugging-port=' + PORT,
  '--user-data-dir=' + profile, '--window-size=1440,960', 'about:blank'], { stdio: 'ignore' });
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
    const ws = new WebSocket(url); let id = 0; const pend = new Map();
    ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } };
    ws.onopen = () => res({ send: (method, params = {}) => { const mid = ++id; ws.send(JSON.stringify({ id: mid, method, params })); return new Promise(r => pend.set(mid, r)); }, close: () => ws.close() });
    ws.onerror = () => rej(new Error('ws'));
  });
}
async function ev(cli, expr) {
  const r = await cli.send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  const res = r.result || {};
  if (res.exceptionDetails) return { error: JSON.stringify(res.exceptionDetails).slice(0, 300) };
  return res.result ? res.result.value : undefined;
}

(async () => {
  const out = { ok: false };
  let cli = null;
  try {
    const list = await json('/json/list');
    const page = list.find(t => t.type === 'page') || await json('/json/new?about:blank');
    cli = await connect(page.webSocketDebuggerUrl);
    await cli.send('Page.enable'); await cli.send('Runtime.enable');
    await cli.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 960, deviceScaleFactor: 1, mobile: false });
    await cli.send('Page.navigate', { url: BASE + '/' }); await sleep(1500);
    await ev(cli, 'localStorage.setItem("aihub_token", ' + JSON.stringify(TOKEN) + '); 1');
    await cli.send('Page.navigate', { url: BASE + '/' }); await sleep(4000);

    out.projects = await ev(cli, 'document.querySelectorAll("#projects-list > *").length');
    out.picked = await ev(cli, `(() => {
      const target = [...document.querySelectorAll('#projects-list *')].find(n => (n.textContent || '').trim().includes(${JSON.stringify(PROJECT)}));
      if (!target) return null;
      (target.closest('button, [role=button], li') || target).click();
      return (target.textContent || '').trim().slice(0, 40);
    })()`);
    await sleep(4000);
    out.convCount = await ev(cli, 'document.querySelectorAll("#convs-list > *").length');
    out.convSummary = await ev(cli, '(document.querySelector("#conv-summary")||{}).textContent || ""');
    out.title = await ev(cli, '(document.querySelector("#project-title")||{}).textContent || ""');
    out.reconCount = await ev(cli, 'document.querySelectorAll("#recon-list > *").length');
    out.reconStatus = await ev(cli, '(document.querySelector("#recon-status")||{}).textContent || ""');

    const shot = await cli.send('Page.captureScreenshot', { format: 'png' });
    const file = path.join(shots, 'core03-project.png');
    fs.writeFileSync(file, Buffer.from(shot.result.data, 'base64'));
    out.screenshot = file;
    out.ok = out.convCount > 10 && out.reconCount > 0;
  } catch (e) { out.error = String((e && e.message) || e); }
  finally { try { cli && cli.close(); } catch (e) {} try { chrome.kill(); } catch (e) {} }
  console.log(JSON.stringify(out, null, 2));
  process.exit(out.ok ? 0 : 1);
})();
