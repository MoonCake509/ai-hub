const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');

const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const BASE = 'http://127.0.0.1:8100';
const PORT = 9333;
const PROJECT = process.argv[2];
const OUT = 'C:/Users/user/aihub-shots';

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
    return new Promise((resolve, reject) => this.pending.set(id, { resolve: resolve, reject: reject }));
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression: expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error('evaluate failed: ' + JSON.stringify(result.exceptionDetails.exception));
    return result.result.value;
  }
}

const GEOMETRY = [
  '(() => {',
  '  const rect = selector => { const node = document.querySelector(selector); if (!node) return null; const box = node.getBoundingClientRect(); return { top: Math.round(box.top), left: Math.round(box.left), right: Math.round(box.right), bottom: Math.round(box.bottom), width: Math.round(box.width), height: Math.round(box.height) }; };',
  '  const list = document.querySelector("#convs-list");',
  '  const title = document.querySelector("#convs-list .conv-text");',
  '  const row = document.querySelector("#convs-list .conv");',
  '  const sidebar = rect(".workspace-sidebar"); const main = rect(".workspace-main");',
  '  const filter = rect("#conv-filter"); const log = rect("#chat-log"); const composer = rect(".composer-area");',
  '  return {',
  '    viewport: window.innerWidth + "x" + window.innerHeight,',
  '    rows: document.querySelectorAll("#convs-list .conv").length,',
  '    listClient: list ? list.clientHeight : -1, listScroll: list ? list.scrollHeight : -1,',
  '    listOwnScroll: !!list && getComputedStyle(list).overflowY === "auto" && list.scrollHeight > list.clientHeight,',
  '    sidebar: sidebar, main: main, filter: filter, log: log, composer: composer,',
  '    summary: (document.querySelector("#conv-summary") || {}).textContent || "",',
  '    moreHidden: document.querySelector("#conv-more") ? document.querySelector("#conv-more").hidden : null,',
  '    moreText: (document.querySelector("#conv-more") || {}).textContent || "",',
  '    titleClipped: !!title && title.scrollWidth > title.clientWidth + 1,',
  '    titleWhiteSpace: title ? getComputedStyle(title).whiteSpace : "",',
  '    listHorizontalOverflow: !!list && list.scrollWidth > list.clientWidth + 1,',
  '    rowHeight: row ? Math.round(row.getBoundingClientRect().height) : -1,',
  '    horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,',
  '    sidebarMainOverlap: !!sidebar && !!main ? (window.innerWidth > 900 ? sidebar.right > main.left + 1 : sidebar.bottom > main.top + 1 && main.top < sidebar.top) : null,',
  '    filterInsideSidebar: !!filter && !!sidebar ? filter.top >= sidebar.top - 1 && filter.bottom <= sidebar.bottom + 1 && filter.width > 40 : false,',
  '    composerBelowLog: !!log && !!composer ? composer.top >= log.bottom - 2 : null,',
  '    offenders: Array.from(document.querySelectorAll("body *")).filter(node => { const box = node.getBoundingClientRect(); return box.width > 0 && box.right > window.innerWidth + 1; }).slice(0, 12).map(node => node.tagName + "." + (typeof node.className === "string" ? node.className : "") + " right=" + Math.round(node.getBoundingClientRect().right) + " w=" + Math.round(node.getBoundingClientRect().width)),',
'    errors: Array.from(document.querySelectorAll("#workspace-error:not([hidden])")).map(node => node.textContent.trim()),',
  '  };',
  '})()',
].join('\n');

async function main() {
  const token = readToken();
  const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ux02-edge-'));
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
  await sleep(1500);
  await cdp.evaluate('localStorage.setItem("aihub_token",' + JSON.stringify(token) + ');' +
    'localStorage.setItem("aihub.project",' + JSON.stringify(PROJECT || '') + ');');
  await cdp.send('Page.reload');
  await sleep(2500);

  let rows = 0;
  for (let attempt = 0; attempt < 30 && rows < 1; attempt += 1) {
    rows = await cdp.evaluate('document.querySelectorAll("#convs-list .conv").length');
    if (rows < 1) await sleep(500);
  }
  if (rows < 1) throw new Error('список чатов не отрисовался: токен, проект или API недоступны');

  fs.mkdirSync(OUT, { recursive: true });
  const results = [];
  for (const viewport of [[1440, 900], [1024, 768], [390, 844]]) {
    await cdp.send('Emulation.setDeviceMetricsOverride', { width: viewport[0], height: viewport[1], deviceScaleFactor: 1, mobile: false });
    await sleep(700);
    const geometry = await cdp.evaluate(GEOMETRY);
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' });
    const file = path.join(OUT, 'ux02-' + viewport[0] + 'x' + viewport[1] + '.png');
    fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
    results.push({ size: viewport[0] + 'x' + viewport[1], file: file, geometry: geometry });
  }
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await sleep(400);
  const interactions = {};
  interactions.beforeMore = await cdp.evaluate('document.querySelectorAll("#convs-list .conv").length');
  await cdp.evaluate('document.querySelector("#conv-more").click()');
  await sleep(400);
  interactions.afterMore = await cdp.evaluate('document.querySelectorAll("#convs-list .conv").length');
  interactions.summaryAfterMore = await cdp.evaluate('document.querySelector("#conv-summary").textContent');
  await cdp.evaluate('(() => { const field = document.querySelector("#conv-filter"); field.value = "OCR"; field.dispatchEvent(new Event("input", { bubbles: true })); })()');
  await sleep(400);
  interactions.searchRows = await cdp.evaluate('document.querySelectorAll("#convs-list .conv").length');
  interactions.searchSummary = await cdp.evaluate('document.querySelector("#conv-summary").textContent');
  interactions.searchTitles = await cdp.evaluate('Array.from(document.querySelectorAll("#convs-list .conv-text")).map(node => node.textContent)');
  await cdp.evaluate('(() => { const field = document.querySelector("#conv-filter"); field.value = ""; field.dispatchEvent(new Event("input", { bubbles: true })); })()');
  await sleep(300);
  interactions.afterClear = await cdp.evaluate('document.querySelectorAll("#convs-list .conv").length');
  interactions.pollStable = await cdp.evaluate('(() => { const first = document.querySelector("#convs-list .conv"); return new Promise(resolve => setTimeout(() => resolve(document.querySelector("#convs-list .conv") === first), 6000)); })()');
  console.log(JSON.stringify({ viewports: results, interactions: interactions }, null, 2));

  socket.close();
  edge.kill();
}

main().catch(error => { console.error('FAIL ' + error.message); process.exit(1); });
