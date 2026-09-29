const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="demo-token"]').content;
let state = null,
  busy = false,
  automatic = false;
const goals = {
  flights: 'Find one-way flights from Zurich to London on September 20, 2026, for one adult in economy. Stop when matching flight options are visible. Do not select or book a flight.',
  travel: 'Find a Design stay in Lisbon with Free cancellation and open Casa Flora.',
  research:
    "Open the article about using finite choices to control browser agents.",
};
const websites = {
  flights: "https://www.google.com/travel/flights?hl=en",
  travel: new URL("/fixture.html?scenario=travel", window.location.origin).href,
  research: new URL("/fixture.html?scenario=research", window.location.origin).href,
};
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const percent = (value) => `${(value * 100).toFixed(value < 0.01 ? 1 : 0)}%`;
let selectedCallId = null;
let displayedTargetIndex = null, hoveredElementIndex = null, overlayContextKey = null;
let view = 'free', freeState = null;
let baseline = { tasks: [], active: false, batch: null, runs: [] };
let selectedRunId = null, selectedAttemptId = null, selectedFrame = null;
let pinnedRun = false, pinnedAttempt = false;
let baselineBusy = false, baselinePolling = false, baselineEpoch = 0;
const baselineLabels = {
  queued: '等待运行', pending: '等待运行', preparing: '准备中', running: '运行中',
  stopping: '正在停止', completed: '已结束', finished: '已结束', done: '已停止',
  passed: '通过', success: '通过', failed: '策略失败', policy_failure: '策略失败',
  environment_blocked: '环境阻塞', blocked: '环境阻塞', timeout: '超时',
  execution_error: '执行错误', error: '执行错误', unknown: '评估未知',
  evaluation_unknown: '评估未知', cancelled: '已取消', interrupted: '已中断',
  met: '目标达成', not_met: '目标未达成',
};
const baselineLabel = value => baselineLabels[value] || value || '未运行';
const duration = ms => ms == null ? '—' : `${(ms / 1000).toFixed(1)} s`;
const resultName = attempt => {
  const result = attempt?.evaluation;
  if (!result) return '';
  if (typeof result === 'string') return baselineLabel(result);
  if (result.final?.outcome) return baselineLabel(result.final.outcome);
  if (result.outcome || result.status || result.result) return baselineLabel(result.outcome || result.status || result.result);
  return result.passed === true ? '通过' : result.passed === false ? '未通过' : '';
};
function formatPayload(payload) {
  return JSON.stringify(payload, (key, value) => {
    if (key === "content" && typeof value === "string") {
      try { return JSON.parse(value); } catch { /* Plain-text message. */ }
    }
    return value;
  }, 2);
}
function observationIndex() {
  return selectedFrame === null ? (state?.observations?.length || 0) - 1 : Number(selectedFrame);
}
function selectedModelCall() {
  const calls = state?.model_calls || [];
  if (view !== 'baseline') return calls.find(c => c.id === selectedCallId) || calls.at(-1);
  if (observationIndex() === -1) return calls.find(c => c.id === selectedCallId);
  const linked = calls.filter(c => Number.isInteger(c.observation_index) && c.observation_index === observationIndex());
  return linked.find(c => c.id === selectedCallId) || linked.find(c => c.kind === 'laya') || linked[0];
}
function historicalDecision(call) {
  if (call?.kind === 'text') call = (state.model_calls || []).find(c => c.id === call.laya_call_id);
  if (!call) return { decision: null, elements: [] };
  const elements = call.request?.state?.elements || [];
  const answers = call.response?.answers;
  const operation = call.operation;
  if (call.status !== 'success' || !operation || !answers?.operation?.probabilities) {
    return { decision: null, elements };
  }
  const target = answers[operation.toLowerCase() + '_target'];
  return { elements, decision: {
    operation, target: call.target, choice: operation, latency_ms: call.latency_ms,
    operation_probabilities: answers.operation.probabilities,
    target_probabilities: target?.probabilities || {}, target_confidence: target?.confidence,
  } };
}
function renderModelCalls() {
  const calls = state.model_calls || [];
  const selected = selectedModelCall();
  $("history").innerHTML = calls.length ? calls.map(c =>
    `<button type="button" class="trace-row ${c.id === selected?.id ? 'active' : ''}" data-call-id="${c.id}" aria-pressed="${c.id === selected?.id}"><span class="number">${String(c.id).padStart(2, '0')}</span><span>${c.kind === 'text' ? 'Text helper' : 'Laya'} <b>${escape(c.operation || c.field || '')}</b><small>${escape(c.model || '')}${c.target ? ' / target '+escape(c.target) : ''}</small></span><span class="time">${c.latency_ms ?? 0} ms</span><span class="call-status ${c.status === 'error' ? 'failed' : ''}">${escape(c.status)}</span></button>`
  ).join('') : '<p class="muted">No model calls yet.</p>';
  $("step-count").textContent = `${calls.length} model calls / ${state.history?.length || 0} actions`;
  $("selected-call").textContent = selected
    ? `Call ${selected.id} / ${selected.kind === 'text' ? 'Text helper' : 'Laya'} / ${selected.model || selected.status}`
    : 'No call selected';
  $("model-state").textContent = selected?.request
    ? formatPayload(selected.request) : 'No request recorded.';
  const contexts = selected?.response?.question_contexts;
  const hasContexts = Array.isArray(contexts) && contexts.length > 0;
  $("question-contexts").innerHTML = hasContexts
    ? `<p class="context-total">${contexts.length} sequences / ${contexts.reduce((n, c) => n + c.input_tokens, 0)} input tokens total</p>` + contexts.map(c =>
      `<section class="question-context"><h3>${escape(c.question)}</h3><p class="context-metrics">Pass ${escape(c.pass)} / <strong>${escape(c.input_tokens)} / ${escape(c.max_tokens)} tokens</strong></p><pre>${escape(c.context)}</pre></section>`
    ).join('')
    : selected?.kind === 'laya' ? '<p class="muted">Per-question context unavailable in this response.</p>' : '';
  const output = selected?.kind === 'laya' && selected.response != null
    ? Object.fromEntries(Object.entries(selected.response).filter(([key]) => key !== 'question_contexts'))
    : selected?.response;
  $("model-output").textContent = selected?.error
    ? formatPayload({ response: output ?? null, error: selected.error })
    : output != null ? formatPayload(output) : 'No response recorded.';
}
async function call(name, body = {}) {
  const response = await fetch(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Demo-Token": token },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "Request failed");
  state = data;
  freeState = data;
  if (name === "reset") selectedCallId = null;
  render();
  return data;
}
function controls() {
  const live = state?.page && !["done", "blocked"].includes(state.status);
  const locked = busy || baseline.active || view !== 'free';
  $('baseline-tab').disabled = busy;
  $("start").disabled = locked;
  $("scenario").disabled = locked;
  $("website-url").disabled = locked;
  $("goal").disabled = locked;
  $("choose").disabled = locked || !live;
  $("execute").disabled = locked || !state?.decision || !live;
  $("auto").disabled = locked || !live;
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("download").disabled = !state?.model_calls?.length && !state?.history?.length;
}
async function perform(fn, label) {
  if (busy || baseline.active || view !== 'free') return;
  busy = true;
  $("error").hidden = true;
  controls();
  $("status").textContent = label;
  try {
    await fn();
  } catch (error) {
    automatic = false;
    try {
      state = await fetch("/api/state").then((r) => r.json());
      freeState = state;
      render();
    } catch {
      /* Preserve the original failure if the server disconnected. */
    }
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("status").textContent = "Paused · needs attention";
  } finally {
    busy = false;
    controls();
  }
}
function render() {
  if (!state) return;
  $("laya-address").textContent = state.laya_base_url || freeState?.laya_base_url || "Unavailable";
  $("text-model-address").textContent = state.text_model_base_url || freeState?.text_model_base_url || "Unavailable";
  $("helper").textContent = `Text helper · ${state.text_model || freeState?.text_model || 'Unavailable'}`;
  $("plan").innerHTML = (state.plan || [])
    .map(
      (goal, i) =>
        `<div class="plan-step ${i === state.plan_index ? "current" : ""}"><span>${i < state.plan_index ? "✓" : i + 1}</span>${escape(goal)}</div>`,
    )
    .join("");
  const inspecting = view === 'baseline';
  const frame = state.observations?.[observationIndex()];
  const historical = inspecting ? historicalDecision(selectedModelCall()) : null;
  const page = inspecting ? frame?.page || (frame ? { url: frame.url, title: frame.title, actions: [] } : null) : state.page;
  $('viewport').style?.setProperty('aspect-ratio', page?.w > 0 && page?.h > 0 ? `${page.w} / ${page.h}` : '1120 / 780');
  const d = inspecting ? historical.decision : state.decision || (state.status === 'done' ? state.decisions?.at(-1) : null);
  const observedElements = inspecting ? (historical.elements.length ? historical.elements : frame?.elements || []) : state.elements || [];
  displayedTargetIndex = d?.target?.split(':')[0] || null;
  const overlayKey = inspecting ? `${selectedRunId}/${selectedAttemptId}/${observationIndex()}/${selectedModelCall()?.id}` : page?.fingerprint;
  if (overlayContextKey !== overlayKey) hoveredElementIndex = null;
  overlayContextKey = overlayKey;
  const labels = {
    idle: "Ready to explore",
    ready: "Page observed · ready for a decision",
    predicted: "Choice ready · inspect or execute",
    done: "Jev reports complete · inspect the page",
    blocked: "Stopped · no supported next action",
  };
  $("status").textContent = labels[state.status] || state.status;
  renderModelCalls();
  if (!page) {
    $("screenshot").hidden = true;
    $("empty").hidden = false;
    $('empty-title').textContent = view === 'baseline' ? '尚无页面观察' : 'Watch the decision happen.';
    $('empty-description').textContent = view === 'baseline' ? '' : 'Start a task to see the browser and its actions.';
    $("targets").innerHTML = '';
    $("choices").innerHTML = '';
    $("operation-choices").innerHTML = '';
    $("url").textContent = 'No page observed';
    $("page-title").textContent = '';
    $("action-count").textContent = '0 elements';
    $("choice-title").textContent = 'Waiting for a page';
    for (const id of ['latency', 'confidence', 'completion']) $(id).textContent = '—';
    renderObservations();
    controls();
    return;
  }
  $("empty").hidden = true;
  const screenshot = page.screenshot_url || (page.screenshot ? `data:image/jpeg;base64,${page.screenshot}` : '');
  $("screenshot").hidden = !screenshot;
  $("screenshot").src = screenshot;
  $("empty").hidden = Boolean(screenshot);
  $('empty-title').textContent = view === 'baseline' ? '截图不可用' : 'Screenshot unavailable';
  $('empty-description').textContent = page.screenshot_error || '';
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${observedElements.length} elements`;
  const chosen = (page.actions || []).find((a) => a.id === d?.choice);
  const chosenElement = observedElements.find(e => e.index === d?.target?.split(':')[0]);
  $("choice-title").textContent = d
    ? chosen?.label || chosenElement?.options?.find(o => o.index === d.target)?.label || chosenElement?.label || d.choice
    : inspecting ? '该次观察没有可用决策' : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? "Ranked by Jev" : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]);
  $("operation-choices").innerHTML = op.map(([name,p]) =>
    `<span class="operation-choice ${name === d.operation ? 'best' : ''}">${escape(name)} <b>${percent(p)}</b></span>`).join('');
  const probability = e => d?.target_probabilities?.[e.index] ??
    Math.max(-1, ...(e.options || []).map(o=>d?.target_probabilities?.[o.index] ?? -1));
  const selectedIndex = d?.target?.split(':')[0];
  const elements = [...observedElements];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $("choices").innerHTML = elements.map(e => {
    const p = probability(e);
    return `<div class="choice ${selectedIndex === e.index ? 'best' : ''}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(' / '))}${e.value ? ' · '+escape(e.value) : ''}${e.checked !== undefined ? ' · checked '+escape(e.checked) : ''}</small>${p >= 0 ? `<div class="bar" style="--probability:${p*100}%"></div>` : ''}</div><span class="probability">${p >= 0 ? percent(p) : '—'}</span></div>`;
  }).join('');
  const targets = new Map();
  for (const a of page.actions || []) {
    if (['click', 'fill', 'select'].includes(a.kind) && a.node != null && !targets.has(a.node)) targets.set(a.node, a);
  }
  const observedIndices = new Set(observedElements.map(e => e.index));
  $("targets").innerHTML = [...targets.values()].map((a,i) => {
    const index=String(i+1);
    const r = a.rect;
    if (!observedIndices.has(index) || !r || ![r.x, r.y, r.w, r.h, page.w, page.h].every(Number.isFinite)
      || r.w <= 0 || r.h <= 0 || page.w <= 0 || page.h <= 0) return '';
    return `<div class="target ${index === selectedIndex || index === hoveredElementIndex ? 'selected' : ''}" data-action="${index}" style="left:${100*r.x/page.w}%;top:${100*r.y/page.h}%;width:${100*r.w/page.w}%;height:${100*r.h/page.h}%"><span>${index}</span></div>`;
  }).join('');
  renderObservations();
  updateOverlayVisibility();
  controls();
}
function renderObservations() {
  const frames = state?.observations || [];
  const inspecting = view === 'baseline';
  $('browser-mode').textContent = inspecting ? 'RECORD' : 'LIVE';
  $('observation-toolbar').hidden = !inspecting || !frames.length;
  if (!inspecting || !frames.length) return;
  const index = selectedFrame === null ? frames.length - 1 : Number(selectedFrame);
  const frame = frames[index];
  const frameOptions = frames.map((f, i) => `<option value="${i}">${i + 1} · Step ${escape(f.step)}${f.phase ? ' / ' + escape(f.phase) : ''} · ${escape(f.title || f.page?.title || '观察')}</option>`).join('');
  if ($('observation-step').innerHTML !== frameOptions) $('observation-step').innerHTML = frameOptions;
  if (!frame) {
    $('observation-step').value = '';
    $('observation-status').textContent = '该调用没有关联的观察记录';
    return;
  }
  $('observation-step').value = String(index);
  const src = frame.screenshot_url || frame.page?.screenshot_url || '';
  $('screenshot').src = src;
  $('screenshot').hidden = !src;
  $('empty').hidden = Boolean(src);
  $('empty-title').textContent = '截图不可用';
  $('empty-description').textContent = frame.screenshot_error || '该次观察未保存截图';
  $('url').textContent = frame.url || frame.page?.url || state.page?.url || '';
  $('page-title').textContent = frame.title || frame.page?.title || state.page?.title || '';
  $('observation-status').textContent = frame.screenshot_error || (!src ? '该次观察未保存截图' : `第 ${frames.indexOf(frame) + 1} / ${frames.length} 次观察`);
}
function renderBaseline() {
  const batch = baseline.batch;
  const attempts = batch?.attempts || [];
  const locked = baselineBusy || baseline.active || busy;
  $('baseline-run-all').disabled = locked || !baseline.tasks.length;
  $('baseline-stop').disabled = baselineBusy || !baseline.active;
  $('baseline-save').disabled = baselineBusy || !batch;
  $('baseline-repeats').disabled = locked;
  const runs = [...baseline.runs];
  if (batch && !runs.some(r => r.id === batch.id)) runs.unshift(batch);
  const runOptions = runs.length
    ? runs.map(r => `<option value="${escape(r.id)}">${escape(r.id)} · ${escape(baselineLabel(r.status))}</option>`).join('')
    : '<option value="">尚无批次</option>';
  if ($('baseline-runs').innerHTML !== runOptions) $('baseline-runs').innerHTML = runOptions;
  $('baseline-runs').value = selectedRunId || batch?.id || '';
  $('baseline-runs').disabled = baselineBusy || !runs.length;
  $('baseline-status').textContent = batch ? `${baselineLabel(batch.status)}${baseline.active && batch.status !== 'running' ? ' · 有批次运行中' : ''}` : '尚未运行';
  $('baseline-path').textContent = batch?.path || '';
  $('baseline-tasks').innerHTML = baseline.tasks.map(task => {
    const matching = attempts.filter(a => a.task_id === task.id);
    const attempt = matching.find(a => ['running', 'preparing'].includes(a.status)) || matching.at(-1);
    return `<tr><th scope="row"><span class="task-number">${escape(task.id.slice(0, 2))}</span><span>${escape(task.name)}</span></th><td>${escape(task.capability)}</td><td>${escape(baselineLabel(attempt?.status))}${resultName(attempt) ? `<small>${escape(resultName(attempt))}</small>` : ''}</td><td>${attempt?.actions ?? '—'}</td><td>${attempt ? `${attempt.laya_calls || 0} / ${attempt.text_calls || 0}` : '—'}</td><td>${duration(attempt?.elapsed_ms)}</td><td><button type="button" class="icon-button" data-run-task="${escape(task.id)}" title="运行${escape(task.name)}" aria-label="运行${escape(task.name)}" ${locked ? 'disabled' : ''}>▶</button></td></tr>`;
  }).join('');
  $('baseline-attempts').innerHTML = attempts.length ? attempts.map(a => {
    const task = baseline.tasks.find(t => t.id === a.task_id);
    const result = resultName(a);
    const reason = a.reason || (typeof a.evaluation === 'object' && a.evaluation?.reason) || '';
    return `<tr class="${a.id === selectedAttemptId ? 'selected-attempt' : ''}"><th scope="row">${escape(task?.name || a.task_id)}<small>第 ${escape(a.repeat)} 次</small></th><td>${escape(baselineLabel(a.status))}${result ? `<small class="${['通过', '目标达成'].includes(result) ? 'result-passed' : 'result-other'}">${escape(result)}</small>` : ''}${reason ? `<small class="attempt-reason">${escape(reason)}</small>` : ''}</td><td>${a.actions || 0}</td><td>${a.laya_calls || 0} / ${a.text_calls || 0}</td><td>${duration(a.elapsed_ms)}</td><td><button type="button" data-attempt-id="${escape(a.id)}" aria-pressed="${a.id === selectedAttemptId}" ${['queued', 'pending'].includes(a.status) ? 'disabled' : ''}>查看记录</button></td></tr>`;
  }).join('') : '<tr><td colspan="6" class="muted">尚无运行记录</td></tr>';
  const passed = attempts.filter(a => a.status === 'passed' || resultName(a) === '通过').length;
  const blocked = attempts.filter(a => resultName(a) === '环境阻塞' || a.status === 'environment_blocked').length;
  $('baseline-summary').textContent = attempts.length ? `${passed} / ${attempts.length} 通过 · ${blocked} 环境阻塞` : '';
  const selected = attempts.find(a => a.id === selectedAttemptId);
  $('baseline-selected').textContent = selected ? `${baseline.tasks.find(t => t.id === selected.task_id)?.name || selected.task_id} · 第 ${selected.repeat} 次 · ${baselineLabel(selected.status)}${resultName(selected) ? ' · ' + resultName(selected) : ''}` : '未选择运行记录';
  $('baseline-evaluation-panel').hidden = !selected?.evaluation;
  $('baseline-evaluation').textContent = selected?.evaluation ? formatPayload(selected.evaluation) : '';
  controls();
}
async function baselineFetch(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Demo-Token': token }, body: JSON.stringify(body),
  };
  const response = await fetch(`/api/baseline${path}`, options);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '基线请求失败');
  return data;
}
function clearFreeTask() {
  freeState = { ...freeState, page: null, status: 'idle', history: [], model_calls: [], decision: null, elements: [], plan: [] };
  if (view === 'free') { state = freeState; render(); }
}
function acceptBaseline(data) {
  const old = baseline.batch;
  if (data.active && !baseline.active) clearFreeTask();
  baseline = { ...baseline, ...data };
  if (pinnedRun && selectedRunId && data.batch?.id !== selectedRunId) baseline.batch = old;
  else {
    const id = data.batch?.id || null;
    if (id !== selectedRunId) {
      selectedAttemptId = null;
      pinnedAttempt = false;
      selectedCallId = null;
      selectedFrame = null;
      if (view === 'baseline') {
        state = { status: 'idle', model_calls: [], history: [], elements: [] };
        render();
      }
    }
    selectedRunId = id;
  }
}
function chooseBaselineAttempt() {
  const attempts = baseline.batch?.attempts || [];
  if (pinnedAttempt && attempts.some(a => a.id === selectedAttemptId)) return selectedAttemptId;
  const recorded = attempts.filter(a => !['queued', 'pending'].includes(a.status)
    && (a.status !== 'cancelled' || a.started_at || a.laya_calls || a.text_calls || a.actions || a.evaluation));
  return (attempts.find(a => ['running', 'preparing'].includes(a.status)) || recorded.at(-1))?.id || null;
}
async function loadBaselineAttempt(id, epoch = baselineEpoch) {
  if (!id || !selectedRunId) return;
  const run = selectedRunId;
  const snapshot = await baselineFetch(`/attempt?${new URLSearchParams({ run_id: run, attempt_id: id })}`);
  if (epoch !== baselineEpoch || view !== 'baseline' || selectedRunId !== run) return;
  if (selectedAttemptId !== id) {
    selectedCallId = null;
    selectedFrame = null;
  }
  selectedAttemptId = id;
  state = snapshot;
  render();
  renderBaseline();
}
async function refreshBaseline() {
  if (baselinePolling || baselineBusy) return;
  baselinePolling = true;
  const epoch = baselineEpoch;
  try {
    const data = await baselineFetch('');
    if (epoch !== baselineEpoch) return;
    acceptBaseline(data);
    if (pinnedRun && selectedRunId && data.batch?.id !== selectedRunId) {
      const batch = await baselineFetch(`/run?${new URLSearchParams({ run_id: selectedRunId })}`);
      if (epoch !== baselineEpoch) return;
      baseline.batch = batch;
    }
    renderBaseline();
    if (view === 'baseline') await loadBaselineAttempt(chooseBaselineAttempt(), epoch);
  } catch (error) {
    if (epoch === baselineEpoch && view === 'baseline') showBaselineError(error);
  } finally {
    baselinePolling = false;
  }
}
function showBaselineError(error) {
  $('baseline-error').textContent = error.message;
  $('baseline-error').hidden = false;
}
async function baselineAction(name, body = {}) {
  if (baselineBusy || busy) return;
  baselineBusy = true;
  const epoch = ++baselineEpoch;
  $('baseline-error').hidden = true;
  renderBaseline();
  try {
    const data = await baselineFetch(`/${name}`, body);
    if (name === 'start') { pinnedRun = false; pinnedAttempt = false; clearFreeTask(); }
    acceptBaseline(data);
    renderBaseline();
    if (data.saved_path) $('baseline-path').textContent = data.saved_path;
    if (view === 'baseline') await loadBaselineAttempt(chooseBaselineAttempt(), epoch);
  } catch (error) {
    showBaselineError(error);
  } finally {
    baselineBusy = false;
    renderBaseline();
  }
}
function startBaseline(taskIds) {
  if (baseline.active || busy || baselineBusy) return;
  const repeats = Number($('baseline-repeats').value);
  if (!Number.isInteger(repeats) || repeats < 1 || repeats > 10) {
    showBaselineError(Error('重复次数必须为 1–10 的整数。'));
    return;
  }
  baselineAction('start', { repeats, ...(taskIds ? { task_ids: taskIds } : {}) });
}
async function selectView(next) {
  if (busy) return;
  view = next;
  try { window.localStorage?.setItem('jev-task-view', next); } catch { /* Storage may be disabled. */ }
  baselineEpoch++;
  for (const name of ['free', 'baseline']) {
    const selected = next === name;
    $(`${name}-tab`).setAttribute('aria-selected', String(selected));
    $(`${name}-tab`).tabIndex = selected ? 0 : -1;
    $(`${name}-panel`).hidden = !selected;
  }
  $('free-controls').hidden = next !== 'free';
  $('error').hidden = true;
  selectedCallId = null;
  if (next === 'free') {
    state = freeState;
    $('observation-toolbar').hidden = true;
    render();
  } else {
    state = { status: 'idle', model_calls: [], history: [], elements: [] };
    render();
    renderBaseline();
    await refreshBaseline();
  }
  controls();
}
$('free-tab').addEventListener('click', () => selectView('free'));
$('baseline-tab').addEventListener('click', () => selectView('baseline'));
for (const name of ['free', 'baseline']) $(`${name}-tab`).addEventListener('keydown', event => {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 'free' : event.key === 'End' ? 'baseline' : name === 'free' ? 'baseline' : 'free';
  $(`${next}-tab`).focus();
  selectView(next);
});
$('baseline-run-all').addEventListener('click', () => startBaseline());
$('baseline-stop').addEventListener('click', () => baselineAction('stop'));
$('baseline-save').addEventListener('click', () => baselineAction('save', { run_id: selectedRunId }));
$('baseline-tasks').addEventListener('click', event => {
  const button = event.target.closest('[data-run-task]');
  if (button && !button.disabled) startBaseline([button.dataset.runTask]);
});
$('baseline-attempts').addEventListener('click', event => {
  const button = event.target.closest('[data-attempt-id]');
  if (!button || button.disabled) return;
  pinnedAttempt = true;
  const epoch = ++baselineEpoch;
  selectedAttemptId = button.dataset.attemptId;
  selectedCallId = null;
  selectedFrame = null;
  renderBaseline();
  loadBaselineAttempt(button.dataset.attemptId, epoch).catch(showBaselineError);
});
$('baseline-runs').addEventListener('change', async () => {
  const id = $('baseline-runs').value;
  if (!id) return;
  const epoch = ++baselineEpoch;
  try {
    const batch = await baselineFetch(`/run?${new URLSearchParams({ run_id: id })}`);
    if (epoch !== baselineEpoch) return;
    pinnedRun = true;
    pinnedAttempt = false;
    selectedRunId = id;
    selectedAttemptId = null;
    selectedCallId = null;
    selectedFrame = null;
    baseline.batch = batch;
    state = { status: 'idle', model_calls: [], history: [], elements: [] };
    render();
    renderBaseline();
    await loadBaselineAttempt(chooseBaselineAttempt(), epoch);
  } catch (error) { showBaselineError(error); }
});
$('observation-step').addEventListener('change', () => {
  selectedFrame = $('observation-step').value;
  selectedCallId = null;
  render();
  $("model-output-panel").open = true;
});
$("history").addEventListener("click", (event) => {
  const row = event.target.closest("[data-call-id]");
  if (!row) return;
  selectedCallId = Number(row.dataset.callId);
  if (view === 'baseline') {
    const call = (state.model_calls || []).find(c => c.id === selectedCallId);
    selectedFrame = Number.isInteger(call?.observation_index) ? String(call.observation_index) : '-1';
    render();
  } else renderModelCalls();
  $("model-output-panel").open = true;
});
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  automatic = false;
  perform(
    () =>
      call("reset", {
        scenario: $("scenario").value,
        url: $("website-url").value.trim(),
        goal: $("goal").value,
      }),
    "Opening a fresh browser…",
  );
});
$("scenario").addEventListener("change", () => {
  const scenario = $("scenario").value;
  if (websites[scenario]) {
    $("website-url").value = websites[scenario];
    $("goal").value = goals[scenario];
  } else {
    $("website-url").focus();
  }
});
$("website-url").addEventListener("input", () => {
  const url = $("website-url").value.trim();
  $("scenario").value = Object.keys(websites).find(key => websites[key] === url) || "custom";
});
$("choose").addEventListener("click", () =>
  perform(() => call("predict"), "Jev is comparing the actions…"),
);
$("execute").addEventListener("click", () =>
  perform(
    () => call("act", { fingerprint: state.page.fingerprint }),
    "Executing the choice…",
  ),
);
$("auto").addEventListener("click", () =>
  perform(async () => {
    automatic = true;
    controls();
    for (let i = 0; i < state.max_steps * 2 && automatic; i++) {
      $("status").textContent = "Running…";
      if ($("pace").checked) {
        await call("predict");
        await new Promise(resolve => setTimeout(resolve, 450));
        if (!automatic) break;
        await call("act", {fingerprint: state.page.fingerprint});
      } else {
        await call("tick");
      }
      if (["done", "blocked"].includes(state.status)) break;
    }
    automatic = false;
  }, "Running the browser…"),
);
$("stop").addEventListener("click", () => {
  automatic = false;
  $("status").textContent = "Pausing after the current request…";
  controls();
});
function updateOverlayVisibility() {
  $('targets').hidden = !$('overlays').checked || $('screenshot').hidden || !$('targets').innerHTML;
}
$("overlays").addEventListener("change", updateOverlayVisibility);
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  hoveredElementIndex = id || null;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === id || t.dataset.action === displayedTargetIndex,
      ),
    );
});
$("choices").addEventListener("pointerleave", () => {
  hoveredElementIndex = null;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === displayedTargetIndex,
      ),
    );
});
$("download").addEventListener("click", () => {
  const { page, ...rest } = state;
  const blob = new Blob(
    [
      JSON.stringify(
        { ...rest, page: { ...page, screenshot: undefined } },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "typesafe-browser-trace.json";
  a.click();
  URL.revokeObjectURL(url);
});
fetch("/api/state")
  .then((r) => r.json())
  .then((s) => {
    freeState = s;
    if (view === 'free') { state = s; render(); }
  })
  .catch(() => {
    $("status").textContent = "Cannot reach local demo server";
    $("laya-address").textContent = "Unavailable";
    $("text-model-address").textContent = "Unavailable";
  });
let storedView;
try { storedView = window.localStorage?.getItem('jev-task-view'); } catch { /* Storage may be disabled. */ }
if (storedView === 'baseline') selectView('baseline');
else refreshBaseline();
setInterval(refreshBaseline, 1000);
