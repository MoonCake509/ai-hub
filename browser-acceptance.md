# UI24 browser acceptance

Target: private integrated preview on port 8201 only. Use a throwaway project/chat; do not use production data or the separate 8100 test container. Do not launch a quality run or a paid Codex/Hermes benchmark. The OCR fixture path below is for local validation only; skip submitting it if the backend has already completed its OCR smoke.

## Workspace and image context

1. Open the workspace tab, select or create a throwaway project, and click **+ Новый чат**. Confirm the chat becomes visible in the sidebar.
2. Select the Architect role and inspect **Агент → Провайдер → Модель** in the role editor. Select the Xiaomi OCR route. Confirm its capability hint says PNG/JPEG is required and it works from supplied context without project file tools. Select a text-only route such as Dolphin; confirm the hint says text only/context only and the conversation remains sendable.
   For native routes with `supports_workdir=false`, confirm the hint says the route cannot run in the project folder. HTTP context-only routes remain selectable and sendable.
3. Click **+ Файл или изображение** and choose `tests/fixtures/synthetic_ocr.png`. Confirm the PNG appears in the context list with dimensions and a preview. In browser network details, verify preview uses `GET /attachments/{id}/preview` with Authorization header and that the URL has no token query parameter.
4. With Xiaomi selected and the image checked, verify the send control is enabled and its hint says a new image replaces the previous image. If a local OCR send is still needed and separately approved, send “Прочитай надпись на изображении и ответь только распознанным текстом.” Expected fixture text: `LUNA OCR 73`. Confirm the accepted response names the image. Open **Детали запуска** and confirm the snapshot records the requested route and active image source.
5. Continue in the same chat without selecting another image. Confirm the hint names the previously used image and the response says it was reused from this conversation. Upload/select one new PNG/JPEG and verify it becomes the one selected OCR image. Selecting more than one new image must produce a clear validation error before a send.
6. Switch the same chat to a text-only role. Confirm the UI warns that image bytes will be omitted while OCR text history remains. Verify the earlier OCR text is still readable and the switch does not block the conversation. Do not submit a new model job during this check; backend route tests cover this payload behavior.
7. Refresh the page. Confirm chat title/history and attachment preview return, preview is fetched again through the authenticated endpoint, and no access token appears in the URL.

## Quality UI, read-only

1. Open **Качество каналов** and exercise the three selectors in order: **Агент → Провайдер → Модель**. Confirm there is no automatic fallback, disabled routes show their reason, and image-required OCR routes are excluded from the text-only suite while remaining visible in **Каналы** for manual enable/disable management. Read the **OCR вручную** card: it explains that this suite does not send images or invoke OCR and directs image checks to the workspace.
2. Change **Количество задач** to 1, 2, then 3. Confirm only that many cases are highlighted and the scope copy says the result is not a general model score. Do not click **Запустить проверку**.
3. Open an existing saved run and verify Russian labels for **Среднее ожидание**, **Суммарное выполнение**, and **Время прогона**; missing usage, cost, or first-token timing stays unavailable. Technical IDs remain under details. Click **Детали задания** on an existing result and confirm it opens the saved job details.
4. Click **Обновить данные** and confirm the saved runs remain listed. Leave channel enable/disable controls unchanged during this read-only pass.

## Frontend checks

Run from the workspace root:

```powershell
node --check work\workspace-fix\integrated-release\web\app.js
node --check work\workspace-fix\integrated-release\web\quality.js
node work\workspace-fix\integrated-release\tests\dom-image-flow.cjs
node work\workspace-fix\integrated-release\tests\dom-conversation-title.cjs
node work\workspace-fix\integrated-release\tests\dom-workspace-race.cjs
node work\workspace-fix\integrated-release\tests\dom-role-profile-race.cjs
node work\workspace-fix\integrated-release\tests\dom-quality-channel-filter.cjs
node work\workspace-fix\dom-regression.cjs
```

The backend context API tests are separate and do not invoke paid models:

```powershell
work\workspace-fix\.venv\Scripts\python.exe -m unittest discover -s work\workspace-fix\integrated-release\tests -p 'test_*context*.py'
```

## Frozen UI hashes

These hashes identify the current UI24 candidate. The targeted 1280 px overflow fix and file-count declension still need a browser spot check after the private preview database recovers. Recompute and review if any web file changes.

| File | SHA256 |
| --- | --- |
| `web/index.html` | `f536427b47fe4e9702dcd97e09d5349bbd92d4e667d7b62ed05d18fdd159a411` |
| `web/app.js` | `5359a1fb7a172724343c95942147bf86479751cd75bfe42b0ed35ff5cd9eab33` |
| `web/styles.css` | `5f6c52bcc6be766ea93eca3190f0a222e5c168a83c4af8809a3071b78c7c20ed` |
| `web/quality.js` | `2e2756e7fd0bcf30eb51cdc4061897862440523bf7733a99c8c91d5913888c46` |
| `web/quality.css` | `a14257b8ffceb2e1786afcc8dec9c6235769d9cf3a09219d20b24ff4f2f0cb55` |

The build label is `ui24`; app/styles use cache suffix `v=24`, quality assets `v=2`.
