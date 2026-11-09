/* Quality view for the isolated UI23 quality slice. */
(() => {
  'use strict';
  const q = id => document.getElementById(id);
  const state = { suite: null, channels: [], runs: [], selectedRunId: null, loading: false, polling: false };
  const token = () => { try { return localStorage.getItem('aihub_token') || ''; } catch { return ''; } };
  const node = (tag, cls = '', text = undefined) => {
    const item = document.createElement(tag);
    if (cls) item.className = cls;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  };
  const errText = e => String(e && e.message || e || 'Неизвестная ошибка');
  const sec = value => value === null || value === undefined ? 'недоступно' : `${Number(value).toFixed(2)} с`;
  const number = value => value === null || value === undefined ? 'недоступно' : String(value);
  const tokens = (usage, key) => usage && usage[key] !== null && usage[key] !== undefined ? String(usage[key]) : 'недоступно';
  const when = value => value ? new Date(value).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
  const statusLabel = status => ({ queued: 'в очереди', running: 'в работе', succeeded: 'успешно', completed: 'завершён', failed: 'ошибка', timeout: 'таймаут', cancelled: 'отменён', unknown: 'неизвестно' }[status] || status || '—');

  async function api(path, options = {}) {
    const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) };
    const access = token();
    if (access) headers.Authorization = 'Bearer ' + access;
    const response = await fetch(path, { ...options, headers, cache: 'no-store' });
    const text = await response.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
    if (response.status === 401) {
      if (typeof window.openGate === 'function') window.openGate('Токен не подошёл — введите его снова.');
      throw new Error('Требуется токен доступа.');
    }
    if (!response.ok) throw new Error(data && (data.detail || data.error) || `${response.status}`);
    return data;
  }

  function showError(message) {
    q('q-error').textContent = message || '';
    q('q-error').hidden = !message;
  }

  function chosenChannel() {
    return state.channels.find(channel => channel.channel_id === q('q-channel').value) || null;
  }
  function requiresImage(channel) {
    return channel.requires_image === true || (channel.required_modalities || []).includes('image');
  }
  function textChannels() { return state.channels.filter(channel => !requiresImage(channel)); }
  function routeFor(run) { return state.channels.find(channel => channel.channel_id === run.channel_id) || null; }
  function routeName(route, run = {}) {
    return route ? `${route.agent_name} · ${route.profile_name || 'по настройкам агента'} · ${route.model_name || 'по настройкам агента'}`
      : 'Маршрут из сохранённого снимка запуска';
  }

  function updateStartEnabled() {
    const selected = chosenChannel();
    q('q-start').disabled = state.loading || !selected || !selected.enabled || requiresImage(selected);
  }

  function categoryLabel(value) {
    return ({ instruction: 'Инструкции', format: 'Формат', reasoning: 'Расчёты', retrieval: 'Работа с контекстом' })[value] || 'Проверка';
  }

  function setSelectOptions(select, placeholder, rows, value) {
    select.replaceChildren(node('option', '', placeholder));
    select.options[0].value = '';
    for (const row of rows) {
      const option = node('option', '', row.label);
      option.value = row.value;
      select.append(option);
    }
    select.value = rows.some(row => row.value === value) ? value : '';
  }

  function updateRouteSelection() {
    const agentId = q('q-agent').value;
    const profile = q('q-provider').value === '__default__' ? null : q('q-provider').value || null;
    const rawModel = q('q-model').value;
    const model = rawModel === '__default__' ? null : rawModel || null;
    const selected = textChannels().find(channel => channel.agent_id === agentId
      && (channel.profile || null) === profile && (channel.model || null) === model) || null;
    q('q-channel').value = selected ? selected.channel_id : '';
    const note = q('q-selection-status');
    if (!agentId) note.textContent = 'Выберите агента, провайдера и модель.';
    else if (!q('q-provider').value) note.textContent = 'Выберите провайдера для этого агента.';
    else if (!rawModel && textChannels().some(channel => channel.agent_id === agentId
      && (channel.profile || null) === profile && channel.model !== null && channel.model !== undefined)) {
      note.textContent = 'Выберите модель для этого провайдера.';
    } else if (!selected) note.textContent = 'Для этой комбинации нет доступного маршрута.';
    else if (!selected.enabled) note.textContent = `Этот маршрут отключён для новых задач. Причина: ${selected.reason || 'не указана оператором'}`;
    else note.textContent = `Выбран маршрут: ${selected.agent_name} · ${selected.profile_name} · ${selected.model_name || selected.model || 'по настройкам агента'}.`;
    updateStartEnabled();
  }

  function renderRouteSelects() {
    const channels = textChannels();
    const oldAgent = q('q-agent').value;
    const oldProvider = q('q-provider').value;
    const oldModel = q('q-model').value;
    const agents = [...new Map(channels.map(channel => [channel.agent_id, channel])).values()];
    setSelectOptions(q('q-agent'), 'Выберите агента', agents.map(channel => ({
      value: channel.agent_id, label: channel.agent_name,
    })), oldAgent);
    const agentId = q('q-agent').value;
    const providers = [...new Map(channels.filter(channel => channel.agent_id === agentId)
      .map(channel => [channel.profile || '__default__', channel])).values()];
    q('q-provider').disabled = !agentId || !providers.length;
    setSelectOptions(q('q-provider'), 'Выберите провайдера', providers.map(channel => ({
      value: channel.profile || '__default__', label: channel.profile_name || 'По настройкам агента',
    })), oldProvider);
    const profile = q('q-provider').value === '__default__' ? null : q('q-provider').value || null;
    const models = [...new Map(channels.filter(channel => channel.agent_id === agentId
      && (channel.profile || null) === profile).map(channel => [channel.model || '', channel])).values()];
    q('q-model').disabled = !agentId || !q('q-provider').value || !models.length;
    setSelectOptions(q('q-model'), 'Выберите модель', models.map(channel => ({
      value: channel.model === null || channel.model === undefined ? '__default__' : channel.model,
      label: `${channel.model_name || channel.model || 'По настройкам агента'}${channel.enabled ? '' : ` · отключена: ${channel.reason || 'причина не указана'}`}`,
    })), oldModel);
    updateRouteSelection();
  }

  function renderSuite() {
    if (!state.suite) return;
    q('q-suite-description').textContent = `${state.suite.description} · версия проверки ${state.suite.version}`;
    const box = q('q-suite-cases');
    box.replaceChildren();
    const cases = state.suite.cases || [];
    const selectedCount = Number(q('q-case-count').value) || cases.length;
    q('q-case-count').max = String(cases.length);
    const chosen = Math.min(selectedCount, cases.length);
    q('q-scope-note').textContent = chosen === cases.length
      ? `В запуске: ${chosen} из ${cases.length} базовых проверок. Результат не является общей оценкой модели.`
      : `В запуске: ${chosen} из ${cases.length} выбранных базовых проверок. Результат не является общей оценкой модели.`;
    cases.forEach((item, index) => {
      const selected = index < selectedCount;
      const card = node('div', `quality-case${selected ? ' selected' : ' unselected'}`);
      card.append(node('strong', '', item.title), node('small', '', `${categoryLabel(item.category)} · ${selected ? 'в этом запуске' : 'не включена'}`));
      box.append(card);
    });
  }

  function renderChannels() {
    const list = q('q-channel-list');
    list.replaceChildren();
    const filter = q('q-channel-filter').value.trim().toLocaleLowerCase('ru-RU');
    const visible = state.channels.filter(channel => !filter || [channel.agent_name, channel.profile_name, channel.model_name, channel.model].join(' ').toLocaleLowerCase('ru-RU').includes(filter));
    q('q-channel-count').textContent = `${visible.length} / ${state.channels.length}`;
    if (!visible.length) list.append(node('p', 'quality-muted', 'Каналы не найдены.'));
    for (const channel of visible) {
      const card = node('article', 'quality-channel' + (channel.enabled ? '' : ' disabled'));
      const head = node('div', 'quality-channel-head');
      const title = node('div', 'quality-channel-title', `${channel.agent_name} · ${channel.profile_name} · ${channel.model_name || channel.model || 'default'}`);
      const badge = node('span', 'badge ' + (channel.enabled ? 'succeeded' : 'failed'), channel.enabled ? 'включён' : 'отключён');
      head.append(title, badge);
      card.append(head);
      card.append(node('div', 'quality-channel-meta', `Провайдер: ${channel.profile_name || 'по настройкам агента'} · Модель: ${channel.model_name || 'по настройкам агента'}`));
      if (channel.reason) card.append(node('div', 'quality-channel-reason', `Причина: ${channel.reason}`));
      if (channel.audit) card.append(node('div', 'quality-channel-audit', `Аудит ${when(channel.audit.changed_at)} · ${channel.audit.enabled_before ? 'включён' : 'отключён'} → ${channel.audit.enabled_after ? 'включён' : 'отключён'} · ${channel.audit.reason}`));
      const actions = node('div', 'quality-channel-actions');
      const reason = document.createElement('input');
      reason.type = 'text'; reason.maxLength = 500; reason.placeholder = 'Причина изменения обязательна';
      reason.setAttribute('aria-label', `Причина ${channel.enabled ? 'отключения' : 'включения'} ${channel.agent_name} ${channel.model_name || channel.model || ''}`);
      const button = node('button', channel.enabled ? 'danger' : 'ghost', channel.enabled ? 'Отключить для новых задач' : 'Включить канал');
      button.type = 'button';
      button.onclick = async () => {
        const note = reason.value.trim();
        if (!note) { reason.focus(); showError('Укажите причину изменения канала: она будет записана в аудит.'); return; }
        button.disabled = true;
        showError('');
        try {
          await api('/quality/channels', { method: 'PATCH', body: JSON.stringify({ channel_id: channel.channel_id, enabled: !channel.enabled, reason: note }) });
          await loadChannels();
          showError('');
        } catch (e) { showError(errText(e)); }
        finally { button.disabled = false; }
      };
      actions.append(reason, button);
      card.append(actions);
      list.append(card);
    }
  }

  function metric(label, value) {
    const box = node('div', 'quality-metric');
    box.append(node('small', '', label), node('strong', '', value));
    return box;
  }

  function renderRunList() {
    const list = q('q-run-list');
    list.replaceChildren();
    q('q-run-count').textContent = `${state.runs.length}`;
    if (!state.runs.length) list.append(node('p', 'quality-muted', 'Запусков пока нет. Выберите маршрут и запустите базовые проверки вручную.'));
    for (const run of state.runs) {
      const card = node('article', 'quality-run' + (run.id === state.selectedRunId ? ' active' : ''));
      const button = node('button', 'quality-run-button');
      button.type = 'button';
      const title = node('div', 'quality-run-title');
      title.append(node('strong', '', routeName(routeFor(run), run)), node('span', 'badge ' + run.status, statusLabel(run.status)));
      const count = Number(run.case_count) || 0;
      const fraction = `${number(run.passed_count)} из ${count} ${count === 4 ? 'базовых' : 'выбранных'} проверок`;
      const scoreLabel = run.score === null || run.score === undefined ? 'доля проверок: недоступна' : `доля проверок: ${run.score}%`;
      const meta = node('div', 'quality-run-meta', `${fraction} · ${scoreLabel} · ${when(run.created_at)}`);
      const timing = node('div', 'quality-run-meta', `Среднее ожидание: ${sec(run.queue_wait_seconds)} · Суммарное выполнение: ${sec(run.execution_seconds)} · Время прогона: ${sec(run.wall_seconds)}`);
      button.append(title, meta, timing);
      button.onclick = () => loadRunDetail(run.id).catch(e => showError(errText(e)));
      card.append(button);
      list.append(card);
    }
  }

  function renderRunDetail(run) {
    const box = q('q-run-detail');
    box.replaceChildren();
    box.hidden = !run;
    if (!run) return;
    box.append(node('h3', '', `${routeName(routeFor(run), run)} · ${statusLabel(run.status)}`));
    const count = Number(run.case_count) || (run.items || []).length;
    const passed = Number(run.passed_count) || 0;
    box.append(node('p', 'quality-muted', `${passed} из ${count} ${count === 4 ? 'базовых' : 'выбранных'} проверок пройдены. Процент относится только к этому запуску и не является общей оценкой модели.`));
    const metrics = node('div', 'quality-metrics');
    metrics.append(metric('Пройдено', number(run.passed_count)), metric('Не пройдено', number(run.failed_count)), metric('Среднее ожидание', sec(run.queue_wait_seconds)), metric('Суммарное выполнение', sec(run.execution_seconds)), metric('Время прогона', sec(run.wall_seconds)), metric('Входные токены', tokens(run.total_usage, 'input_tokens')), metric('Из них кэшировано', tokens(run.total_usage, 'cached_input_tokens')), metric('Выходные токены', tokens(run.total_usage, 'output_tokens')), metric('Токены рассуждений', tokens(run.total_usage, 'reasoning_output_tokens')), metric('Стоимость', 'недоступна'), metric('Время до первого токена', 'не измеряется'));
    box.append(metrics, node('p', 'quality-muted', 'Указанная модель — сообщение адаптера или провайдера; это не независимая проверка её личности.'));
    const technical = node('details', 'quality-technical');
    technical.append(node('summary', '', 'Технические сведения'));
    technical.append(node('p', 'quality-muted', `ID запуска: ${run.id} · канал: ${run.channel_id} · набор: ${run.suite_id} · версия набора: ${run.suite_version} · версия проверяющего модуля: ${run.grader_version}`));
    box.append(technical);
    for (const item of run.items || []) {
      const card = node('article', 'quality-item');
      const head = node('div', 'quality-item-head');
      head.append(node('strong', '', `${item.ordinal + 1}. ${item.case && item.case.title || 'Базовая проверка'}`), node('span', 'badge ' + (item.passed ? 'succeeded' : item.passed === false ? 'failed' : item.job_status || 'queued'), item.passed === true ? 'проверка пройдена' : item.passed === false ? 'проверка не пройдена' : statusLabel(item.job_status || item.status)));
      card.append(head);
      card.append(node('p', 'quality-item-reason', item.grader_reason || item.error || 'Ожидает завершения задания.'));
      const caseMetrics = node('div', 'quality-metrics');
      caseMetrics.append(metric('Модель из ответа адаптера', item.reported_model || 'не раскрыта'), metric('Среднее ожидание', sec(item.queue_wait_seconds)), metric('Суммарное выполнение', sec(item.execution_seconds)), metric('Время задачи', sec(item.wall_seconds)), metric('Входные токены', tokens(item.usage, 'input_tokens')), metric('Из них кэшировано', tokens(item.usage, 'cached_input_tokens')), metric('Выходные токены', tokens(item.usage, 'output_tokens')), metric('Токены рассуждений', tokens(item.usage, 'reasoning_output_tokens')), metric('Стоимость', item.cost === null || item.cost === undefined ? 'недоступна' : item.cost), metric('Время до первого токена', sec(item.first_token_latency_seconds)));
      card.append(caseMetrics);
      const caseTech = node('details', 'quality-technical');
      caseTech.append(node('summary', '', 'Сведения о задании'));
      caseTech.append(node('p', 'quality-muted', `Задание: ${item.job_id || 'не создано'} · агент: ${item.job_agent_id || run.agent_id || '—'} · роль: ${item.job_role_id || '—'} · запрошенный провайдер: ${item.requested_profile || 'не указан'} · запрошенная модель: ${item.requested_model || 'не указана'} · источник сообщения о модели: ${item.identity_evidence_source === 'not_reported' ? 'не раскрыта' : item.identity_evidence_source || 'не указан'}`));
      caseTech.append(node('p', 'quality-muted', `ID проверки: ${item.case_id || '—'} · ID результата: ${item.id || '—'}`));
      card.append(caseTech);
      if (item.raw_response !== null && item.raw_response !== undefined) {
        card.append(node('div', 'quality-muted', 'Сырой ответ'));
        card.append(node('pre', 'quality-raw', item.raw_response));
      }
      for (const attempt of item.attempts || []) {
        const row = node('div', 'quality-attempts row');
        row.append(node('span', 'badge ' + attempt.status, `Попытка ${attempt.attempt_no}: ${statusLabel(attempt.status)}`), node('span', '', `Длительность: ${sec(attempt.elapsed_seconds)}`), node('span', '', `Токены на входе / выходе: ${tokens(attempt.usage, 'input_tokens')} / ${tokens(attempt.usage, 'output_tokens')}`), node('span', '', `Кэшировано: ${tokens(attempt.usage, 'cached_input_tokens')} · рассуждения: ${tokens(attempt.usage, 'reasoning_output_tokens')}`));
        card.append(row);
      }
      if (item.job_id) {
        const openJob = node('button', 'ghost quality-job-link', 'Детали задания');
        openJob.type = 'button';
        openJob.onclick = () => {
          q('view-quality').hidden = true;
          if (typeof window.switchView === 'function') window.switchView('ops');
          if (typeof window.openJob === 'function') window.openJob(item.job_id);
          else showError('Детали задания доступны в разделе Ops после обновления страницы.');
        };
        card.append(openJob);
      }
      box.append(card);
    }
  }

  async function loadSuite() {
    const data = await api('/quality/suites');
    state.suite = (data.suites || [])[0] || null;
    if (state.suite) renderSuite();
  }
  async function loadChannels() {
    const data = await api('/quality/channels');
    state.channels = [...new Map((data.channels || []).map(channel => [channel.channel_id, channel])).values()];
    renderRouteSelects();
    renderChannels();
  }
  async function loadRuns() {
    const data = await api('/quality/runs?limit=50');
    state.runs = data.runs || [];
    renderRunList();
  }
  async function loadRunDetail(id) {
    state.selectedRunId = id;
    const run = await api('/quality/runs/' + encodeURIComponent(id));
    renderRunList();
    renderRunDetail(run);
  }
  async function refreshActiveRuns() {
    if (state.polling || !token() || q('view-quality').hidden ||
        !state.runs.some(run => ['starting', 'queued', 'running'].includes(run.status))) return;
    state.polling = true;
    try {
      await loadRuns();
      const selected = state.runs.find(run => run.id === state.selectedRunId);
      if (selected) await loadRunDetail(selected.id);
    } catch (e) { showError(errText(e)); }
    finally { state.polling = false; }
  }
  async function loadAll() {
    if (!token() || state.loading) return;
    state.loading = true;
    q('q-refresh').disabled = true;
    showError('');
    try {
      await Promise.all([loadSuite(), loadChannels(), loadRuns()]);
      if (state.selectedRunId) await loadRunDetail(state.selectedRunId);
    } catch (e) { showError(errText(e)); }
    finally { state.loading = false; q('q-refresh').disabled = false; updateStartEnabled(); }
  }

  q('q-refresh').onclick = loadAll;
  // Read-only refresh while accepted work is active; this never starts or retries jobs.
  setInterval(refreshActiveRuns, 5000);
  q('q-agent').onchange = () => { q('q-provider').value = ''; q('q-model').value = ''; renderRouteSelects(); };
  q('q-provider').onchange = () => { q('q-model').value = ''; renderRouteSelects(); };
  q('q-model').onchange = updateRouteSelection;
  q('q-case-count').onchange = renderSuite;
  q('q-channel-filter').oninput = renderChannels;
  q('q-start-form').onsubmit = async event => {
    event.preventDefault();
    const channel = chosenChannel();
    const caseCount = Number(q('q-case-count').value);
    const timeout = Number(q('q-timeout').value);
    if (!channel || !channel.enabled) { showError('Выберите включённый канал.'); return; }
    if (requiresImage(channel)) { showError('Маршруты с обязательным изображением не поддерживаются текущим текстовым набором проверок.'); return; }
    if (!Number.isInteger(caseCount) || caseCount < 1 || caseCount > 4) { showError('Количество задач должно быть от 1 до 4.'); return; }
    if (!Number.isInteger(timeout) || timeout < 10 || timeout > 180) { showError('Лимит времени на одну задачу — от 10 до 180 секунд.'); return; }
    const button = q('q-start');
    button.disabled = true;
    q('q-start-status').className = 'msg';
    q('q-start-status').textContent = 'Добавляю отдельные jobs в общую очередь…';
    showError('');
    const idempotencyKey = window.crypto && crypto.randomUUID ? crypto.randomUUID() : `quality-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    try {
      const run = await api('/quality/runs', { method: 'POST', body: JSON.stringify({
        suite_id: state.suite && state.suite.id,
        suite_version: state.suite && state.suite.version,
        agent_id: channel.agent_id,
        profile: channel.profile,
        model: channel.model,
        case_count: caseCount,
        timeout_s: timeout,
        idempotency_key: idempotencyKey,
      }) });
      state.selectedRunId = run.id;
      q('q-start-status').className = 'msg ok';
      q('q-start-status').textContent = 'Проверка сохранена. Исполнение идёт через обычную очередь.';
      await loadRuns();
      await loadRunDetail(run.id);
    } catch (e) {
      q('q-start-status').className = 'msg error';
      q('q-start-status').textContent = errText(e);
      showError(errText(e));
    } finally { updateStartEnabled(); }
  };

  document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => {
    const selected = tab.dataset.view === 'quality';
    q('view-quality').hidden = !selected;
    if (selected) loadAll();
  }));
  const gate = q('gate');
  if (gate && typeof MutationObserver !== 'undefined') {
    new MutationObserver(() => { if (gate.hidden && token()) loadAll(); }).observe(gate, { attributes: true, attributeFilter: ['hidden'] });
  }
  if (token()) loadAll();
})();
