// DIAG-01: проверка бейджа наблюдений в реальном браузере (headless Edge + CDP).
// Запуск на сервере: node tools/agents-badge-check.cjs
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const BASE = process.env.AIHUB_BASE || 'http://127.0.0.1:8100';
const PORT = Number(process.env.AIHUB_CDP_PORT || 9334);
const OUT = process.env.AIHUB_SHOTS || 'C:/Users/user/aihub-shots';

function readToken() {
  const text = fs.readFileSync('R:/AI-Hub/infra/.env', 'utf8');
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(/^HUB_TOKEN=(.*)$/);
    if (match) return match[1].trim().replace(/^"|"$/g, '');
  }
  throw new Error('HUB_TOKEN not found');
}

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map();
    ws.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const slot = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) slot.reject(new Error(JSON.stringify(message.error)));
        else slot.resolve(message.result);
      }
    };
  }
  send(method, params) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id: id, method: method, params: params || {} }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression: expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error('evaluate failed: ' + JSON.stringify(result.exceptionDetails.exception));
    return result.result.value;
  }
}

async function main() {
  const token = readToken();
  const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'diag01-edge-'));
  const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + profileDir, 'about:blank'], { stdio: 'ignore' });

  let page = null;
  for (let attempt = 0; attempt < 40 && !page; attempt += 1) {
    await sleep(300);
    try {
      const targets = await (await fetch('http://127.0.0.1:' + PORT + '/json/list')).json();
      page = targets.find(item => item.type === 'page');
    } catch (error) { /* браузер ещё поднимается */ }
  }
  if (!page) throw new Error('Edge не отдал страницу по CDP');

  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  const cdp = new CDP(socket);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');

  await cdp.send('Page.navigate', { url: BASE + '/ui/' });
  await sleep(1200);
  await cdp.evaluate('localStorage.setItem("aihub_token",' + JSON.stringify(token) + ');');
  await cdp.send('Page.reload');
  await sleep(2000);

  let rows = 0;
  for (let attempt = 0; attempt < 30 && rows < 1; attempt += 1) {
    rows = await cdp.evaluate('document.querySelectorAll("#agents-list .agent").length');
    if (rows < 1) await sleep(500);
  }
  if (rows < 1) throw new Error('реестр агентов не отрисовался');

  fs.mkdirSync(OUT, { recursive: true });
  const report = { rows: rows };

  report.badges = await cdp.evaluate(
    'Array.from(document.querySelectorAll("#agents-list .agent")).map(row => {' +
    ' const badge = row.querySelector(".status.obs-ok, .status.obs-degraded, .status.obs-failed, .status.obs-unknown, .status");' +
    ' return { id: row.dataset.id, text: badge ? badge.textContent.trim() : null,' +
    ' cls: badge ? badge.className : null, title: badge ? badge.title : null }; })');
  report.unknownCount = report.badges.filter(item => item.cls && item.cls.includes('obs-unknown')).length;
  report.okCount = report.badges.filter(item => item.cls && item.cls.includes('obs-ok')).length;
  report.options = await cdp.evaluate(
    'Array.from(document.querySelectorAll("#run-agent option")).slice(0, 3).map(o => o.textContent)');

  const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
  const fileList = path.join(OUT, 'diag01-agents-list.png');
  fs.writeFileSync(fileList, Buffer.from(shot.data, 'base64'));
  report.listShot = fileList;

  await cdp.evaluate('document.querySelector("#agents-list .agent .status").click()');
  await sleep(400);
  report.panel = await cdp.evaluate(
    '(() => { const panel = document.querySelector("#agents-list .obs-panel"); if (!panel) return null;' +
    ' return { rows: panel.querySelectorAll(".obs-row").length,' +
    ' kinds: Array.from(panel.querySelectorAll(".obs-kind")).map(n => n.textContent),' +
    ' states: Array.from(panel.querySelectorAll(".obs-state")).map(n => n.textContent),' +
    ' meta: Array.from(panel.querySelectorAll(".obs-meta")).map(n => n.textContent),' +
    ' reasons: Array.from(panel.querySelectorAll(".obs-reason")).map(n => n.textContent),' +
    ' head: (panel.querySelector(".obs-panel-head") || {}).textContent }; })()');
  report.horizontalOverflow = await cdp.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1');
  report.errors = await cdp.evaluate('Array.from(document.querySelectorAll("#workspace-error:not([hidden])")).map(n => n.textContent.trim())');

  const shot2 = await cdp.send('Page.captureScreenshot', { format: 'png' });
  const filePanel = path.join(OUT, 'diag01-agent-observations.png');
  fs.writeFileSync(filePanel, Buffer.from(shot2.data, 'base64'));
  report.panelShot = filePanel;

  await cdp.evaluate('document.querySelector("#agents-list .agent .status").click()');
  await sleep(300);
  report.panelClosed = await cdp.evaluate('document.querySelectorAll("#agents-list .obs-panel").length');

  console.log(JSON.stringify(report, null, 2));
  socket.close();
  edge.kill();
}

main().catch(error => { console.error('FAIL ' + error.message); process.exit(1); });
