Архитектурный разбор AI Hub по 6 претензиям
Что изучено: README.md, config/agents.yaml, config/hub.yaml, web/index.html, web/styles.css, web/app.js, hub/app/main.py, hub/app/capabilities.py, hub/app/db.py, hub/app/workspace.py, hub/app/security_policy.py, hub/app/templates.py, agents/laptop/qwen.js (ветка master, HEAD e0c1504, 111 коммитов).
Оговорка о полноте: main.py, db.py, app.js, workspace.py вернулись фрагментами (с сокращениями). Утверждения «есть» — подтверждены дословно; утверждения «нет» — это «не найдено в прочитанных фрагментах», финально проверяется grep'ом. Код не менял, PR не создавал.
1. Что подтверждено по коду, а что предположение
#
	
Подтверждено дословно
	
Где


1
	
Токены есть: --bg:#10141c, --panel:#171d28, --surface:#1d2532, --line:#2a3546, --accent:#83c8d3; кнопка по умолчанию = background:var(--surface), .ghost{background:transparent}
	
web/styles.css :root, button


2
	
Матрица возможностей состоит ровно из 8 имён: text, patch, workdir, commands, usage, streaming, stop, output_control. thinking/reasoning в модели нет; preflight отклоняет неизвестное имя как unknown_capability
	
hub/app/capabilities.py


3
	
qwen-server: transport: cdp, model_selection: true, usage: {supported: false}; в qwen.js есть switchModel() через [aria-label="Select Model"], но переключателя режима размышления нет — «Thinking completed» используется только как маркер разметки ответа (extractAnswer)
	
config/agents.yaml, agents/laptop/qwen.js


4
	
9 агентов в реестре; ноутбучные: codex, openclaw-laptop, hermes-laptop, qwen-code; серверные: qwen-server, hermes-server, openclaw-server, llm-server, xiaomi-ocr-server. UI не фильтрует по host
	
config/agents.yaml, web/app.js loadAgents()


5
	
«Расходы и лимиты» — это захардкоженная строка: $('#role-quota').textContent = agent.transport==='http' ? 'Локальный HTTP-маршрут · денежный баланс не сообщается.' : 'Провайдер не сообщает данные баланса или лимитов.'
	
web/app.js wsPopulateRoleProfiles(), блок section.quota-summary в index.html


6
	
Usage хранится: jobs.result_usage jsonb, job_attempts.usage jsonb, пишется из POST /bridge/result (payload.get("usage")), effective_model берётся из usage.reported_model. Таблиц цен/денег/бюджетов-в-деньгах нет; runs.budgets/budget_used — это шаги/вызовы/секунды
	
hub/app/db.py DDL, main.py bridge_result


7
	
usage: supported:true только у codex и llm-server; у всех Qwen/Hermes/OpenClaw — false с причиной «канал не отдаёт usage»
	
config/agents.yaml


8
	
Архивариус — именно роль: ROLE_IDS = ("planner","coder","tester","reviewer","archivist"), ROLE_LABELS["archivist"]="Архивариус", ROLE_META.archivist в UI, шаг архивариуса в FLOW-шаблоне «пять ролей»
	
hub/app/db.py, web/app.js, hub/app/templates.py


9
	
Прото-крон уже есть: _sweeper() + _schedule_index_sweep() c INDEX_SWEEP_S=21600, ключ идемпотентности autoindex:{project}:{stamp}, пропуск проектов с активным прогоном. Пользовательского расписания/таблицы задач нет
	
hub/app/main.py


10
	
Инструменты для «наведения порядка» уже есть как host-операции: write_reports, refresh_index, describe_files, archive_artifacts, create_backup; мутирует дерево только WRITES_ROOT={"apply_patch"}; писательская аренда checkout по host+workdir
	
hub/app/workspace.py OPS, db.py _checkout_key/resources


11
	
Workspace привязан к проекту: projects.host_id (default 'laptop'), workdir, status, repo_root/git_root/branch/head_sha; корень новых проектов projects_root() = HUB_PROJECTS_ROOT → config/hub.yaml: projects.root: 'R:\Projects'; в UI — свободный текст #project-workdir + селект «Ноутбук/Сервер», для нового проекта ставится 'laptop'
	
db.py, workspace.py, main.py _prepare_project, app.js wsProjectDialog/wsSaveProject


12
	
Операции просмотра дисков/каталогов нет в OPS; create_op_job требует, чтобы root входил в allowed_roots(host, project_id) — т.е. в уже зарегистрированные проекты
	
hub/app/workspace.py
Предположения (требуют проверки): есть ли в той же БД таблицы биллинга LiteLLM (db.py: «схема aihub той же базы, что использует LiteLLM»); какие workspace_ops реально декларирует серверный host-worker (agents/server/worker_v2.py + workers.py); есть ли в DOM Qwen Chat доступная кнопка режима размышления; полная карта ролей в templates.py (получено описанием, не дословно).
2. По пунктам: причина и решение
П. 1. «Всё одного цвета» — причина измерима
Токены объявлены, но разница поверхностей ниже порога различимости. Расчёт по фактическим значениям из styles.css:
Пара
	
Контраст
	
Норма WCAG для границ/элементов


кнопка --surface на --panel
	
1.10:1
	
≥ 3:1


поле ввода #111925 на --panel
	
1.04:1
	
≥ 3:1


граница --line на --panel
	
1.36:1
	
≥ 3:1


сайдбар #131a24 на --bg
	
1.05:1
	
—


карточка агента #202c39 на --panel
	
1.19:1
	
—


--faint (используется для текста 9–11px)
	
3.89:1
	
≥ 4.5:1 → не проходит


--text / --muted
	
14.35 / 6.58:1
	
проходит
Вторая причина: помимо токенов поверхности заданы литералами в десятках мест (#131a24, #141b25, #151e2a, #1b2531, #202c39, #21343e, #23313f, #18212d, #1a2431, #111925, …), а единственный акцент --accent расходуется на ссылки, аватары, активные вкладки, статус running и фокус — поэтому он не читается как «главное действие».
Решение (файлы: web/styles.css, точечно web/quality.css):
Развести уровни: --bg (фон) → --panel (панель) → --surface (интерактивный элемент) → --surface-hi (hover) с шагом так, чтобы граница элемента давала ≥ 3:1 к своему фону; поднять --line и --faint.
Ввести семантику: --action-primary, --action-secondary, --action-danger, --state-ok/warn/fail/info, --focus. Акцент оставить только за primary и фокусом; статусы/аватары перевести на нейтральные токены.
Заменить литеральные hex на токены (механическая замена, компоновку не трогать — владелец подтвердил, что UX удобен).
Кнопки: primary (залитая), secondary (контурная с видимой границей), ghost (только в тулбарах), danger. Сейчас .ghost = прозрачный без границы, отсюда «сливается».
П. 2. Нет переключателя размышления у qwen-server
Причина системная, а не «забыли кнопку»: в capabilities.py нет измерения thinking, поэтому UI физически нечего показывать, а preflight отклонит needs:["thinking"] как неизвестное требование. В qwen.js режим не управляется — адаптер лишь распознаёт «Thinking completed» как разделитель ответа.
Решение по слоям:
hub/app/capabilities.py: добавить thinking в CAPABILITIES, LABELS, DEFAULT_REASON (по правилу репозитория «не объявлено = недоступно с причиной»).
config/agents.yaml: объявить thinking у qwen-server только с доказательством (evidence: fixture:...), как это сделано для patch у codex.
agents/laptop/qwen.js (файл используется и серверным воркером): добавить функцию переключения режима по аналогии с switchModel() — клик + перечитывание состояния из DOM, отказ до отправки при несовпадении; фактическое наличие размышления подтверждать тем же маркером «Thinking completed», который уже парсится.
agents/server/worker_v2.py + hub/app/main.py (run_agent, чатовый эндпоинт, ответ /bridge/jobs): передать флаг как часть назначения задания (не как правку конфига клиента — это принцип ADAPTER-01 в коде).
web/index.html + web/app.js: трёхпозиционный контрол (auto/вкл/выкл) в редакторе роли и в форме запуска, показывается только при capabilities.thinking.supported, иначе — причина отказа, как уже сделано для #role-capability.
Альтернатива, требующая решения владельца: режим размышления у Qwen штатно управляется параметром API (enable_thinking/бюджет размышления), а не DOM. Если перевести qwen-server с CDP на API-профиль через LiteLLM, переключатель становится параметром запроса и исчезает хрупкость DOM. Но это уход от подписки Chat к API-ключу — вопрос денег и легальности доступа.
П. 3. Дубли ноутбука
Причина: реестр плоский, host — просто поле; UI показывает всех агентов без фильтра, а роли по умолчанию вообще указывают на ноутбук: db.create_project() и seed_workspace() сидируют все 5 ролей с agent_id='codex', FLOW-шаблон по умолчанию agent="codex".
🔴 Ключевой блокер, найденный в коде: patch:true, workdir:true, commands:true есть только у codex (ноутбук). У всех серверных агентов patch:false, commands:false. То есть «оставить только серверный контур» сегодня означает потерю единственного подтверждённого канала правки файлов — FLOW (кодер → тесты → ревью → слияние) встанет.
Решение:
config/agents.yaml: добавить enabled: false (или hidden: true) для qwen-code, openclaw-laptop, hermes-laptop; записи не удалять — на них ссылаются роли и история.
hub/app/main.py /agents, /agent-profiles: отдавать отключённых отдельным списком/флагом; web/app.js — не показывать их в селектах ролей, запуска и качества.
hub/app/db.py: сменить дефолт новых ролей с codex на серверный канал; миграция существующих roles.agent_id с отчётом «что переведено».
Отдельная задача-предпосылка: подтвердить patch/commands у серверного канала (fixture, как fixture:core02b-apply-patch) или принять, что кодер остаётся на Codex-ноутбуке, а «убрать дубли» = убрать только Qwen/OpenClaw/Hermes-ноутбук.
П. 4. Архивариус → подменю автоматизации проекта
Причина: архивариус — интерактивная роль (ROLE_IDS, ROLE_META, шаг FLOW), а не задача; расписания как сущности нет, но вся инфраструктура для него уже готова: _sweeper(), workspace.OPS (refresh_index, write_reports, describe_files, archive_artifacts, create_backup), идемпотентные ключи, workspace_receipts, аренда checkout.
Решение:
hub/app/db.py: новые таблицы project_jobs (проект, имя, cron-выражение, таймзона, ответственная роль/агент/профиль/модель, промпт, allowlist операций, бюджет, enabled, last_run/next_run) и project_job_runs (slot-ключ, статус, длительность, receipt, стоимость, отчёт).
hub/app/main.py: вынести планировщик из _schedule_index_sweep в общий тик по project_jobs; ключ идемпотентности cron:{project}:{job}:{slot} — повтор тика не даст второго запуска (механизм уже отлажен на autoindex:).
hub/app/workspace.py: архивариус исполняется только через OPS-allowlist, новые операции (move_files, quarantine) добавлять с валидацией resolve()/is_within() и политикой security_policy.py; удаление — только в карантин.
web/index.html + web/app.js: подменю проекта «Автоматизация» (список задач, модель, расписание, последний/следующий запуск, промпт, «Запустить сейчас» / «Пробный прогон», отчёт).
Миграция, а не удаление: update_role() бросает unknown_role для id вне ROLE_IDS, а conversations.role_id='archivist' уже есть в данных. Поэтому: сначала скрыть роль из UI и перевести её в задачу, оставив id в БД помеченным deprecated; удаление из ROLE_IDS — отдельным шагом после миграции чатов и FLOW-шаблона (templates.py).
П. 5. «Расходы и лимиты» не работает
Причина доказана дословно: блок — статическая строка в app.js, выбираемая по transport. Никакого запроса к API, никакого агрегата. При этом:
учёт токенов есть на уровне попытки (job_attempts.usage), но его никто не агрегирует;
usage:false у всех серверных каналов, кроме llm-server → для Qwen/Hermes/OpenClaw токены физически не приходят;
«лимиты» в системе — операционные (MAX_PROMPT_CHARS, MAX_QUEUE, MAX_RESULT_CHARS, таймауты, runs.budgets), денежных нет;
в main.py не найдено эндпоинтов расходов/цен/бюджетов.
Решение, строго в этом порядке:
hub/app/db.py: append-only usage_ledger (проект, роль/задача, агент, профиль, модель, токены in/out/reasoning, источник reported|estimated|absent, idempotency-key = attempt_id) + price_versions + budgets.
hub/app/main.py bridge_result: писать в ledger в той же транзакции, что и finish_attempt (защита от двойного счёта уже есть — duplicate/late/stale).
Новый hub/app/billing.py + эндпоинты /projects/{id}/usage, /budgets: агрегаты и проверка до постановки в очередь рядом с capabilities.preflight в run_agent и чатовом эндпоинте.
web/app.js: заменить строку #role-quota на реальные данные; честно показывать «нет данных канала» там, где usage:false, и «оценка» там, где токены считаются по тексту.
Дешёвый путь, который надо проверить первым: хаб проксирует /v1/chat/completions в LiteLLM, а схема aihub живёт в той же БД, что LiteLLM (db.py, README). Если таблицы биллинга LiteLLM там есть — расходы по API-трафику можно читать готовыми, не изобретая прайс. Для подписочных CDP/CLI-каналов денежного учёта не будет вовсе: там считать запросы/сессии и явно помечать «подписка, не деньги».
П. 6. Привязка workspace: диск + папка
Причина: привязка уже есть (projects.host_id/workdir, init_project, repo_root), но выбор диска зашит в конфиг (projects.root: R:\Projects), UI даёт только текстовое поле, а OPS не содержит операции просмотра каталогов; allowed_roots() строится из уже зарегистрированных проектов, поэтому для нового проекта browse не заработает без отдельного allowlist.
Решение:
config/hub.yaml: projects.allowed_roots (разрешённые диски/корни, напр. R:\Projects, D:\Projects) + projects.default_host: server.
hub/app/workspace.py: новые ops list_roots / list_dir (только чтение, вне WRITES_ROOT), валидация через существующие canon()/is_within() и security_policy.denied_reason; зарегистрировать в host_capabilities (workers.py, agents/server/worker_v2.py).
hub/app/main.py: GET /workspace/roots?host_id=…, GET /workspace/dirs?root=…&cursor=… поверх create_op_job; _prepare_project — выбор корня из allowlist вместо единственного projects_root().
web/index.html + web/app.js: в #project-dialog — селект диска + навигация по папкам + «создать папку», текстовое поле оставить как fallback; дефолт host для нового проекта — server (сейчас в wsProjectDialog жёстко 'laptop', и в DDL projects.host_id default 'laptop').
Проверить, что серверный host-worker реально декларирует init_project/apply_patch/run_checks — иначе «всё на сервере» упрётся в отсутствующие операции.
3. Порядок внедрения и критерии приёмки
Этап
	
Работы
	
Зависимости
	
Критерии приёмки


0. Проверка (0.5–1 д.)
	
Grep-инвентарь: эндпоинты расходов, workspace_ops серверного воркера, таблицы LiteLLM в БД, ссылки ролей на ноутбучных агентов
	
—
	
Отчёт: что подтверждено/опровергнуто; ни одной правки кода


1. Цветовая иерархия (1–3 д.)
	
П.1: токены, контраст, варианты кнопок
	
0
	
Границы интерактивных элементов ≥ 3:1 к фону; --faint-текст ≥ 4.5:1; primary отличим в grayscale; DOM/компоновка не изменились (проверка по browser-acceptance.md)


2. Убрать дубли (2–4 д.)
	
П.3: enabled:false, фильтры в API/UI, миграция дефолтов ролей
	
0; решение по Codex
	
Ни одного ноутбучного дубля в селектах; ни одна роль не ссылается на отключённого агента; FLOW-прогон зелёный; откат конфига проверен


3. Workspace (1–1.5 нед.)
	
П.6: allowlist корней, browse-ops, пикер диска/папки, дефолт server
	
2
	
Новый проект создаётся выбором диска+папки; тесты на .., симлинк, вне allowlist, read-only, нехватка места; init_project идемпотентен; два проекта изолированы (аренда checkout)


4. Деньги (1–2 нед.)
	
П.5: ledger → прайс/чтение LiteLLM → агрегаты → enforcement → UI
	
3
	
Расхождение с источником ≤ 1–2% на контрольной серии; duplicate/late/stale не удваивают запись; каналы без usage помечены «нет данных», оценки — «оценка»; тестовый микро-лимит блокирует вызов до очереди; kill switch работает


5. Thinking (3–5 д.)
	
П.2: capability → адаптер → UI
	
2, 4
	
Состояние сохраняется per project/role; при «вкл» в ответе есть трасса размышления, при «выкл» — нет; несовпадение состояния DOM → отказ до отправки; у неподдерживающих каналов контрол скрыт с причиной


6. Автоматизация + архивариус (1.5–2 нед.)
	
П.4: project_jobs, планировщик, подменю, dry-run/откат, миграция роли
	
3, 4, 5
	
Пробный прогон не меняет ФС; apply строго по плану; откат возвращает состояние; параллельный слот пропускается; видны последний/следующий запуск, стоимость, отчёт; ни одна операция вне workspace; безвозвратных удалений нет; чаты со старой ролью не сломаны


7. Опции
	
Возврат ноутбука как узла, сверка расходов с провайдером, перенос Qwen на API
	
6
	
По отдельному решению владельца
Тесты складывать в существующий контур: tests/run_contract_tests.py, docs/acceptance/, docs/decisions/ — репозиторий уже живёт по правилу «каталог — заявка, не доказательство», новые возможности должны приходить с fixture.
4. Риски
Потеря единственного канала правки файлов при отключении ноутбука (patch:true только у codex) — самый тяжёлый риск; блокирует этап 2.
Удаление archivist из ROLE_IDS ломает update_role() (unknown_role), существующие conversations.role_id и FLOW-шаблон — только мягкая миграция.
Ложные цифры расходов: подписочные CDP/CLI-каналы не отдают usage; любая «стоимость» по ним — оценка. Выдать её за деньги = повторить историю неработающего раздела.
Автономный архивариус с правом перемещать файлы без dry-run/карантина/бюджета — потеря документов и неконтролируемые траты.
Browse-операции расширяют поверхность атаки: обход allowed_roots, симлинки, чтение секретов; обязательна политика security_policy.py и аудит.
Хрупкость DOM Qwen Chat: адаптер уже ломался на смене модели/незавершённой генерации; новый тумблер добавляет ещё одну точку отказа.
Двойное срабатывание планировщика при нескольких инстансах хаба: сейчас sweeper внутри процесса; нужен lease/advisory-lock на слот.
Миграция workspace: смена host_id/корня у существующих проектов при живых run_tasks/checkout-арендах; нужен барьер «нет активных прогонов» (образец уже есть в _schedule_index_sweep).
Косметический риск: правка styles.css без токенизации повторит проблему; в файле уже наслоены блоки UX-01/UX-02/DIAG-01 — легко получить регресс sticky-панелей.
5. Вопросы владельцу
Codex на ноутбуке оставляем? Если нет — каким серверным каналом подтверждать patch/commands и кто оплачивает этот переход?
Что считать «расходом»: деньги по API-ключам, токены, запросы или лимиты подписки? Валюта, период, уровень (проект/роль/задача)?
Поведение при исчерпании лимита: жёсткий блок, предупреждение, очередь или дешёвый fallback?
Qwen оставляем на подписке через CDP или переводим на API-ключ (тогда thinking — штатный параметр, а не DOM-хак)?
Какие диски/корни разрешены для проектов (R:\Projects, D:\…)? Что делать с уже созданными проектами и их workdir?
Полномочия архивариуса: переименование/перемещение/карантин — да; удаление — нет? Кто подтверждает план первые N запусков?
Архивариус как шаг FLOW-процесса сохраняется или полностью переезжает в расписание?
Расписание: часовой пояс, окно (ночь?), частота, лимит стоимости на запуск, срок хранения отчётов и трасс размышления.
Допустимо ли менять компоновку, или этап 1 — строго цвет/состояния? Тёмная тема остаётся единственной?
Один владелец или команда? Сейчас один общий HUB_TOKEN — на бюджеты и аудит решений это влияет напрямую.
AIHUB-END-8b744ce8