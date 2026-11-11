const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../agents/laptop/qwen.js'), 'utf8');
const marker = 'AIHUB-END-test';
const rawPatch = '--- a/sample.py\n+++ b/sample.py\n@@ -1,2 +1,2 @@\n def label(name):\n-    return name\n+    return name.strip()';
const rendered = 'diff\n1\n2\n3\n4\n5\n6\n' + rawPatch.replace(/ /g, '\u00a0');
const clipboard = { writeText: async () => { throw new Error('System clipboard changed'); } };
const original = clipboard.writeText;
const block = { innerText: rendered, querySelector: () => ({ click: () => clipboard.writeText(rawPatch) }) };
const answer = { innerText: rendered + '\n\n' + marker, querySelectorAll: () => [block] };
const context = vm.createContext({
  document: { querySelectorAll: () => [answer] }, navigator: { clipboard },
  process: { env: {} }, setTimeout,
});
vm.runInContext(source.slice(0, source.lastIndexOf('\n(async () => {')), context);

(async () => {
  assert.equal(await context.readStructuredAnswer(1, answer.innerText, marker), rawPatch + '\n');
  assert.equal(clipboard.writeText, original);
  await assert.rejects(() => context.readStructuredAnswer(0, answer.innerText, marker), /answer changed/);
  answer.innerText = rendered;
  await assert.rejects(() => context.readStructuredAnswer(1, rendered, marker), /no separate end marker/);
  assert.equal(clipboard.writeText, original);
  // Буфер приложения может отдать CRLF: патч должен вернуться в LF, иначе git apply зовёт его битым.
  const crlfBlock = { innerText: 'diff\r\n1\r\n' + rawPatch.replace(/\n/g, '\r\n'), querySelector: () => ({ click: () => clipboard.writeText(rawPatch.replace(/\n/g, '\r\n')) }) };
  const crlfAnswer = { innerText: crlfBlock.innerText + '\r\n\r\n' + marker, querySelectorAll: () => [crlfBlock] };
  context.document.querySelectorAll = () => [crlfAnswer];
  assert.equal(await context.readStructuredAnswer(1, crlfAnswer.innerText, marker), rawPatch + '\n');
  context.document.querySelectorAll = () => [answer];
  assert.equal(clipboard.writeText, original);
  // Живой DOM может оборвать последнюю строку: обрезанный маркер — это не «готово».
  context.requireEndMarker('diff\n+line\n' + marker, marker);
  await assert.rejects(async () => context.requireEndMarker('diff\n+line\nAIHUB-END', marker), /answer is incomplete/);
  await assert.rejects(async () => context.requireEndMarker('diff\n+line\n' + marker + '-extra', marker), /answer is incomplete/);
  console.log('Qwen Chat preserved raw diff, rejected a missing end marker and a truncated terminator.');
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
