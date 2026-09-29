const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, { listeners: {}, addEventListener(event, fn) { this.listeners[event] = fn; } });
  return elements.get(id);
};
const context = vm.createContext({
  document: { getElementById: element, querySelector: () => ({ content: 'test' }) },
  window: { location: { origin: 'http://localhost' } }, URL, URLSearchParams,
  setInterval: () => 0,
  fetch: () => new Promise(() => {}),
});
vm.runInContext(fs.readFileSync('jev_ultrafast/static/app.js', 'utf8'), context);
for (const [id, property] of [['question-contexts', 'innerHTML'], ['model-state', 'textContent'], ['model-output', 'textContent']]) {
  const node = element(id);
  let value = '';
  node.writes = 0;
  Object.defineProperty(node, property, {
    get: () => value,
    set: next => { value = next; node.writes++; node.scrollTop = 0; },
  });
}
function assertPollingPreservesScroll() {
  const panels = ['question-contexts', 'model-state', 'model-output'].map(element);
  const writes = panels.map(node => node.writes);
  panels.forEach(node => { node.scrollTop = 240; });
  for (let i = 0; i < 3; i++) vm.runInContext('render()', context);
  assert.deepEqual(panels.map(node => node.writes), writes, 'unchanged panels must not replace DOM on polling');
  assert.deepEqual(panels.map(node => node.scrollTop), [240, 240, 240]);
}
element('model-input-panel').open = false;
element('raw-request-panel').open = true;
context.fixture = { page: null, history: [], model_calls: [
  { id: 1, kind: 'laya', model: 'local', status: 'success', request: { state: { text: 'first' } }, response: { answers: { operation: 'CLICK' } } },
  { id: 2, kind: 'text', model: '<script>', field: 'Search', status: 'error', request: { messages: [{ role: 'user', content: '{"goal":"second"}' }] }, response: { choices: [] }, error: 'Invalid field' },
] };
vm.runInContext('state = fixture; render()', context);
assert.match(element('history').innerHTML, /Text helper/);
assert.match(element('history').innerHTML, /&lt;script&gt;/);
assert.equal(JSON.parse(element('model-state').textContent).messages[0].content.goal, 'second');
assert.equal(JSON.parse(element('model-output').textContent).error, 'Invalid field');
element('history').listeners.click({ target: { closest: () => ({ dataset: { callId: '1' } }) } });
assert.equal(JSON.parse(element('model-state').textContent).state.text, 'first');
assert.equal(JSON.parse(element('model-output').textContent).answers.operation, 'CLICK');
assert.equal(element('model-input-panel').open, false);
assert.equal(element('raw-request-panel').open, true);
assert.equal(element('model-output-panel').open, true);
context.fixture.model_calls[0].response.question_contexts = [
  { question: 'operation', pass: 1, input_tokens: 800, max_tokens: 1024, token_ids: [1, 2], context: '[CLS] <unsafe> operation' },
  { question: 'click_target', pass: 1, input_tokens: 1024, max_tokens: 1024, context: '[CLS] click only' },
  { question: 'click_target', pass: 2, input_tokens: 200, max_tokens: 1024, context: '[CLS] final' },
];
vm.runInContext('render()', context);
assert.match(element('question-contexts').innerHTML, /2024 input tokens total/);
assert.match(element('question-contexts').innerHTML, /800 \/ 1024 tokens/);
assert.match(element('question-contexts').innerHTML, /Pass 2/);
assert.match(element('question-contexts').innerHTML, /&lt;unsafe&gt;/);
assert.equal(element('raw-request-panel').open, true, 'contexts do not hide HTTP request body');
element('raw-request-panel').open = true;
vm.runInContext('render()', context);
assert.equal(element('raw-request-panel').open, true, 'polling does not collapse an opened HTTP request');
assert.deepEqual(JSON.parse(element('model-output').textContent), { answers: { operation: 'CLICK' } });
assert.deepEqual(context.fixture.model_calls[0].response.question_contexts[0].token_ids, [1, 2]);
assertPollingPreservesScroll();
context.fixture.model_calls[0].response.question_contexts[0].context += ' updated';
vm.runInContext('render()', context);
assert.match(element('question-contexts').innerHTML, /updated/);
context.fixture.model_calls[0].error = 'Invalid answer';
vm.runInContext('render()', context);
assert.equal(JSON.parse(element('model-output').textContent).error, 'Invalid answer');
assert.equal(JSON.parse(element('model-output').textContent).response.question_contexts, undefined);
delete context.fixture.model_calls[0].error;
context.fixture.model_calls.push({ id: 3, kind: 'laya', status: 'success', request: { text: 'third' } });
vm.runInContext('render()', context);
assert.equal(JSON.parse(element('model-state').textContent).state.text, 'first');
context.fixture.model_calls.push({ id: 4, kind: 'deepseek', status: 'success',
  request: { messages: [{ role: 'user', content: '{"goal":"cloud"}' }] },
  response: { choices: [{ message: { content: '{"operation":"DONE"}' } }] },
});
element('history').listeners.click({ target: { closest: () => ({ dataset: { callId: '4' } }) } });
assert.match(element('question-contexts').innerHTML, /cloud/);
assertPollingPreservesScroll();
vm.runInContext('state = { model_calls: [], history: [] }; render()', context);
assert.equal(element('download').disabled, true);
assert.equal(element('model-output').textContent, 'No response recorded.');
context.fixture.model_calls = [{ id: 1, kind: 'deepseek', stage: 'operation', decision_round: 1,
  operation: 'CLICK', status: 'success', request: { messages: [{ content: 'operation context' }] },
  response: { choices: [{ message: { content: '{"operation":"CLICK"}' } }] },
}, { id: 2, kind: 'deepseek', stage: 'click_target', decision_round: 1, status: 'success',
  request: { messages: [{ content: 'target context' }] }, response: { choices: [{ message: { content: '{"click_target":"2"}' } }] },
}];
vm.runInContext('state = fixture; selectedCallId = 1; render()', context);
assert.match(element('question-contexts').innerHTML, /operation context/);
assert.match(element('question-contexts').innerHTML, /target context/);
assert.equal((element('history').innerHTML.match(/data-call-id=/g) || []).length, 1);
assert.match(element('history').innerHTML, /data-call-id="1"/);
assert.doesNotMatch(element('history').innerHTML, /操作选择|目标选择/);
assert.match(element('stage-requests').innerHTML, /HTTP|messages/);
assertPollingPreservesScroll();
const oldHTML = element('question-contexts').innerHTML;
element('history').listeners.click({ target: { closest: () => ({ dataset: { callId: '2' } }) } });
assert.equal(element('question-contexts').innerHTML, oldHTML, 'switching stages keeps the same round panels');
assert.match(element('history').innerHTML, /trace-row active/);
context.fixture.model_calls.push({ id: 3, kind: 'text', status: 'success', decision_round: 1,
  request: {}, response: { text: 'value' } });
vm.runInContext('render()', context);
assert.equal((element('history').innerHTML.match(/data-call-id=/g) || []).length, 2);
assert.match(element('history').innerHTML, /Text helper/);
context.fixture.model_calls[1].status = 'error';
context.fixture.model_calls[0].latency_ms = 10;
context.fixture.model_calls[1].latency_ms = 20;
vm.runInContext('render()', context);
assert.match(element('history').innerHTML, /30 ms/);
assert.match(element('history').innerHTML, /call-status failed/);
const html = fs.readFileSync('jev_ultrafast/static/index.html', 'utf8');
assert.match(html, /id="model-input-panel" open>/);
assert.match(html, /id="model-input-panel" open>[\s\S]*?<\/details>\s*<details id="raw-request-panel" open>/);
assert.match(html, /id="model-output-panel" open/);
console.log('Trace UI tests passed');
