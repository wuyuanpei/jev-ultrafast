const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    listeners: {}, value: id === 'baseline-repeats' ? '1' : '', checked: true,
    addEventListener(name, fn) { this.listeners[name] = fn; },
    setAttribute(name, value) { this[name] = value; }, focus() {},
  });
  return elements.get(id);
}
const tasks = Array.from({ length: 10 }, (_, index) => ({
  id: `${String(index + 1).padStart(2, '0')}_task`,
  name: index ? `Task ${index + 1}` : '<unsafe>', capability: 'Search & stop',
}));
const attempts = tasks.slice(0, 2).map((task, index) => ({
  id: `${task.id}/attempt-01`, task_id: task.id, repeat: 1,
  status: index ? 'queued' : 'running', actions: 0, laya_calls: 1, text_calls: 0, elapsed_ms: 200,
}));
const batch = { id: '20260928_123456_001', status: 'running', path: 'C:\\artifacts\\baselines\\run', attempts };
const historical = { ...batch, id: '20260927_123456_001', status: 'interrupted', path: 'C:\\artifacts\\baselines\\old' };
let data = { tasks, active: true, batch, runs: [{ id: batch.id, status: batch.status }] };
const snapshot = {
  page: { url: 'https://example.com/final', title: 'Final page', actions: [], w: 1120, h: 780 },
  elements: [], history: [], status: 'ready',
  model_calls: [{ id: 1, kind: 'laya', observation_index: 0, status: 'success', request: { first: true }, response: { answers: {}, question_contexts: [{ question: 'operation', pass: 1, input_tokens: 3, max_tokens: 1024, context: '<bos>', token_ids: [1] }] } }],
  observations: [
    { step: 0, screenshot_url: '/api/baseline/file?first', url: 'https://example.com/initial', title: 'Initial' },
    { step: 1, screenshot_url: '/api/baseline/file?second', url: 'https://example.com/final', title: 'Final' },
  ],
};
const requests = [];
let failure = null, deferred = null;
const context = vm.createContext({
  document: { getElementById: element, querySelector: () => ({ content: 'secret' }), querySelectorAll: () => [] },
  window: { location: { origin: 'http://localhost' }, confirm: () => false, prompt: () => null }, URL, URLSearchParams,
  setInterval: () => 0,
  fetch: async (path, options = {}) => {
    requests.push({ path, options });
    if (deferred && path === '/api/baseline') return deferred;
    if (failure && path.endsWith('/start')) return { ok: false, json: async () => ({ error: failure }) };
    let value = path === '/api/state' ? { status: 'idle', page: null, elements: [], model_calls: [], history: [], laya_base_url: 'local' } : data;
    if (path.includes('/attempt?')) value = snapshot;
    if (path.includes('/run?')) value = path.includes(historical.id) ? historical : batch;
    if (path.endsWith('/save')) value = { ...data, saved_path: JSON.parse(options.body).run_id === historical.id ? historical.path : batch.path };
    if (path.endsWith('/rename')) {
      historical.name = JSON.parse(options.body).name;
      value = { ...data, runs: [...data.runs, { id: historical.id, status: historical.status, name: historical.name }],
        renamed_batch: historical };
    }
    return { ok: true, json: async () => JSON.parse(JSON.stringify(value)) };
  },
});
const run = code => vm.runInContext(code, context);
const flush = () => new Promise(resolve => setImmediate(resolve));

async function main() {
  run(fs.readFileSync('jev_ultrafast/static/app.js', 'utf8'));
  await flush();
  assert.equal(element('start').disabled, true, 'active baseline locks free commands on refresh');
  await run("selectView('baseline')");
  assert.equal(element('free-panel').hidden, true);
  assert.equal(element('baseline-panel').hidden, false);
  assert.equal(element('baseline-tab')['aria-selected'], 'true');
  assert.equal((element('baseline-tasks').innerHTML.match(/data-run-task=/g) || []).length, 10);
  assert.match(element('baseline-tasks').innerHTML, /&lt;unsafe&gt;/);
  assert.equal(element('baseline-run-all').disabled, true);
  assert.equal(element('baseline-stop').disabled, false);
  assert.equal(element('baseline-delete').disabled, true);
  assert.equal(element('baseline-rename').disabled, true);
  assert.equal(element('system1-provider').disabled, true);
  assert.equal(element('baseline-path').textContent, batch.path);
  assert.match(element('baseline-attempts').innerHTML, /selected-attempt/);
  assert.equal(element('screenshot').src, '/api/baseline/file?second');
  assert.equal(element('targets').hidden, true);
  assert.equal(element('model-output').textContent.includes('token_ids'), false);

  element('observation-step').value = '0';
  element('observation-step').listeners.change();
  assert.equal(element('screenshot').src, '/api/baseline/file?first');
  await run('refreshBaseline()');
  assert.equal(element('screenshot').src, '/api/baseline/file?first', 'polling preserves chosen observation');

  await run("baselineAction('save', { run_id: selectedRunId })");
  let request = requests.find(r => r.path.endsWith('/save'));
  assert.equal(request.options.headers['X-Demo-Token'], 'secret');
  assert.equal(JSON.parse(request.options.body).run_id, batch.id);
  await run("baselineAction('stop')");
  assert.ok(requests.some(r => r.path.endsWith('/stop')));

  data = { ...data, active: false, batch: { ...batch, status: 'completed', attempts: attempts.map(a => ({ ...a, status: 'passed', evaluation: { ever_met: true, first_met_step: 0, extra_actions: 0, final: { outcome: 'met', checks: { title: true } } } })) } };
  await run('refreshBaseline()');
  assert.equal(element('baseline-run-all').disabled, false);
  assert.match(element('baseline-summary').textContent, /2 \/ 2 通过/);
  assert.match(element('baseline-attempts').innerHTML, /目标达成/);
  assert.equal(element('baseline-evaluation-panel').hidden, false);
  assert.equal(JSON.parse(element('baseline-evaluation').textContent).final.checks.title, true);
  element('baseline-repeats').value = '11';
  const beforeInvalid = requests.length;
  run('startBaseline()');
  assert.equal(requests.length, beforeInvalid);
  assert.match(element('baseline-error').textContent, /1–10/);
  element('baseline-repeats').value = '3';
  run("freeState = { page: { url: 'closed-free-task' }, status: 'ready', history: [], model_calls: [] }");
  run('startBaseline()');
  await flush();
  request = requests.filter(r => r.path.endsWith('/start')).at(-1);
  assert.deepEqual(JSON.parse(request.options.body), { repeats: 3, system1_provider: 'laya' });
  assert.equal(run('freeState.page'), null, 'baseline start invalidates the closed free-task snapshot');

  element('baseline-tasks').listeners.click({ target: { closest: () => ({ dataset: { runTask: '04_task' }, disabled: false }) } });
  await flush();
  request = requests.filter(r => r.path.endsWith('/start')).at(-1);
  assert.deepEqual(JSON.parse(request.options.body), { repeats: 3, system1_provider: 'laya', task_ids: ['04_task'] });
  element('system1-provider').value = 'deepseek';
  element('system1-provider').listeners.change();
  run("startBaseline(['04_task'])");
  await flush();
  request = requests.filter(r => r.path.endsWith('/start')).at(-1);
  assert.equal(JSON.parse(request.options.body).system1_provider, 'deepseek');

  element('baseline-attempts').listeners.click({ target: { closest: () => ({ dataset: { attemptId: attempts[1].id }, disabled: false }) } });
  assert.equal(run('selectedAttemptId'), attempts[1].id, 'selection is pinned before the snapshot request returns');
  await flush();
  assert.ok(requests.at(-1).path.includes('attempt_id=02_task%2Fattempt-01'));
  await run('refreshBaseline()');
  assert.equal(run('selectedAttemptId'), attempts[1].id, 'polling preserves manually selected attempt');

  snapshot.observations.push({ step: 1, phase: 'final', screenshot_error: 'Screenshot timeout' });
  run('selectedFrame = null');
  await run('refreshBaseline()');
  assert.equal(element('screenshot').hidden, true);
  assert.equal(element('empty-description').textContent, 'Screenshot timeout');
  assert.equal(element('observation-step').value, '2', 'same-step final observation is separately selectable');
  element('observation-step').value = '1';
  element('observation-step').listeners.change();
  assert.equal(element('screenshot').src, '/api/baseline/file?second');

  failure = 'Browser is already owned';
  await run("baselineAction('start', { repeats: 1 })");
  assert.equal(element('baseline-error').hidden, false);
  assert.equal(element('baseline-error').textContent, failure);
  failure = null;

  let resolveDeferred;
  deferred = new Promise(resolve => { resolveDeferred = resolve; });
  const countBefore = requests.filter(r => r.path === '/api/baseline').length;
  const pending = run('refreshBaseline()');
  await run('refreshBaseline()');
  assert.equal(requests.filter(r => r.path === '/api/baseline').length, countBefore + 1, 'polls never overlap');
  resolveDeferred({ ok: true, json: async () => data });
  await pending;
  deferred = null;

  data.batch.attempts[1].status = 'cancelled';
  run('pinnedAttempt = false; selectedAttemptId = null');
  await run('refreshBaseline()');
  assert.equal(run('selectedAttemptId'), attempts[1].id, 'zero-action cancellation with model calls remains inspectable');
  const cancelledButton = element('baseline-attempts').innerHTML.match(/<button[^>]*data-attempt-id="02_task\/attempt-01"[^>]*>/)[0];
  assert.equal(cancelledButton.includes('disabled'), false);

  element('baseline-runs').value = historical.id;
  await element('baseline-runs').listeners.change();
  assert.equal(run('selectedRunId'), historical.id);
  await run('refreshBaseline()');
  assert.equal(run('selectedRunId'), historical.id, 'polling keeps a selected historical run');
  assert.equal(element('baseline-path').textContent, historical.path);

  assert.equal(element('baseline-rename').disabled, false);
  const beforeRename = requests.length;
  element('baseline-rename').listeners.click();
  assert.equal(requests.length, beforeRename, 'cancelled rename sends no request');
  context.window.prompt = (message, initial) => {
    assert.match(message, new RegExp(historical.id));
    assert.equal(initial, historical.id);
    return '<研究批次>';
  };
  element('baseline-rename').listeners.click();
  await flush();
  const rename = requests.find(r => r.path.endsWith('/rename'));
  assert.deepEqual(JSON.parse(rename.options.body), { run_id: historical.id, name: '<研究批次>' });
  assert.match(element('baseline-runs').innerHTML, /&lt;研究批次&gt; · 20260927_123456_001/);
  assert.equal(element('baseline-runs').value, historical.id);
  assert.equal(element('baseline-path').textContent, historical.path);
  await run('refreshBaseline()');
  assert.match(element('baseline-runs').innerHTML, /&lt;研究批次&gt;/);
  assert.equal(element('baseline-runs').value, historical.id);
  await run("baselineAction('save', { run_id: selectedRunId })");
  request = requests.filter(r => r.path.endsWith('/save')).at(-1);
  assert.equal(JSON.parse(request.options.body).run_id, historical.id);
  assert.equal(element('baseline-path').textContent, historical.path);

  assert.equal(element('baseline-delete').disabled, false);
  const beforeDelete = requests.length;
  element('baseline-delete').listeners.click();
  assert.equal(requests.length, beforeDelete, 'cancel confirmation sends no deletion');
  context.window.confirm = message => { assert.match(message, /无法撤销/); return true; };
  data = { tasks, active: false, batch: null, runs: [] };
  element('baseline-delete').listeners.click();
  await flush();
  const deletion = requests.find(r => r.path.endsWith('/delete'));
  assert.equal(JSON.parse(deletion.options.body).run_id, historical.id);
  assert.equal(deletion.options.headers['X-Demo-Token'], 'secret');
  assert.equal(element('baseline-delete').disabled, true);
  assert.equal(element('screenshot').hidden, true);
  await run("selectView('free')");
  assert.equal(element('free-controls').hidden, false);
  assert.equal(element('baseline-panel').hidden, true);
  assert.equal(element('observation-toolbar').hidden, true);
  assert.equal(element('start').disabled, false);
  assert.equal(element('screenshot').hidden, true, 'stale baseline screenshot cleared');

  const html = fs.readFileSync('jev_ultrafast/static/index.html', 'utf8');
  assert.match(html, /class="baseline-run-picker">[\s\S]*?id="baseline-runs"[\s\S]*?id="baseline-rename"[^>]*>重命名<\/button>[\s\S]*?id="baseline-delete"[^>]*>删除<\/button>\s*<\/div>/);
  assert.match(html, /id="baseline-repeats"[^>]*min="1" max="10" step="1" value="1"/);
  assert.match(html, /role="tabpanel" aria-labelledby="baseline-tab"/);
  console.log('Baseline UI tests passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
