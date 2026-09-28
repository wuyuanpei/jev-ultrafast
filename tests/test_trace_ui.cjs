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
  window: { location: { origin: 'http://localhost' } }, URL,
  fetch: () => new Promise(() => {}),
});
vm.runInContext(fs.readFileSync('jev_ultrafast/static/app.js', 'utf8'), context);
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
assert.equal(element('model-input-panel').open, true);
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
assert.equal(element('raw-request-panel').open, false);
assert.deepEqual(JSON.parse(element('model-output').textContent), { answers: { operation: 'CLICK' } });
assert.deepEqual(context.fixture.model_calls[0].response.question_contexts[0].token_ids, [1, 2]);
context.fixture.model_calls[0].error = 'Invalid answer';
vm.runInContext('render()', context);
assert.equal(JSON.parse(element('model-output').textContent).error, 'Invalid answer');
assert.equal(JSON.parse(element('model-output').textContent).response.question_contexts, undefined);
delete context.fixture.model_calls[0].error;
context.fixture.model_calls.push({ id: 3, kind: 'laya', status: 'success', request: { text: 'third' } });
vm.runInContext('render()', context);
assert.equal(JSON.parse(element('model-state').textContent).state.text, 'first');
vm.runInContext('state = { model_calls: [], history: [] }; render()', context);
assert.equal(element('download').disabled, true);
assert.equal(element('model-output').textContent, 'No response recorded.');
const html = fs.readFileSync('jev_ultrafast/static/index.html', 'utf8');
assert.match(html, /id="model-input-panel" open/);
assert.match(html, /id="model-output-panel" open/);
console.log('Trace UI tests passed');
