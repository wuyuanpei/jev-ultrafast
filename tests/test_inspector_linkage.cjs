const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
let overlayNodes = [];
const element = id => {
  if (!nodes.has(id)) nodes.set(id, { listeners: {}, checked: true, style: { setProperty(key, value) { this[key] = value; } },
    addEventListener(name, callback) { this.listeners[name] = callback; } });
  return nodes.get(id);
};
const context = vm.createContext({
  document: { getElementById: element, querySelector: () => ({ content: 'test' }), querySelectorAll: () => overlayNodes },
  window: { location: { origin: 'http://localhost' } }, URL, URLSearchParams,
  setInterval: () => 0, fetch: () => new Promise(() => {}),
});
const run = code => vm.runInContext(code, context);
run(fs.readFileSync('jev_ultrafast/static/app.js', 'utf8'));
const laya = (id, observation, operation, label, probability) => ({
  id, observation_index: observation, kind: 'laya', status: 'success', operation,
  target: operation === 'DONE' ? null : '1', latency_ms: 12,
  request: { state: { page: { text: label }, elements: [{ index: '1', label, role: 'button', operations: [operation] }] } },
  response: { answers: { operation: { probabilities: { [operation]: probability, WAIT: 1 - probability } },
    [operation.toLowerCase() + '_target']: { probabilities: { 1: probability }, confidence: probability } } },
});
context.fixture = {
  status: 'done', page: { title: 'Latest', actions: [] }, elements: [{ label: 'LATEST WRONG' }], history: [],
  decision: { operation: 'WRONG_LATEST' },
  observations: ['First', 'Retry', 'Done observation', 'Final'].map((title, index) => ({
    step: index < 2 ? 0 : 1, phase: index === 3 ? 'final' : 'step',
    title, screenshot_url: `/frame-${index}.jpg`, page: { title, url: `https://example.test/${index}`, actions: [] },
  })),
  model_calls: [laya(1, 0, 'CLICK', 'Old link', 0.8), laya(2, 1, 'TYPE_TEXT', 'Search field', 0.9),
    { id: 3, observation_index: 1, laya_call_id: 2, kind: 'text', status: 'success',
      request: { messages: [{ content: '{"goal":"Search"}' }] }, response: { text: 'Alan Turing' } },
    laya(4, 2, 'DONE', 'Article', 0.95)],
};
run("view = 'baseline'; state = fixture; render()");
assert.equal(element('screenshot').src, '/frame-3.jpg');
assert.equal(element('operation-choices').innerHTML, '');
assert.equal(element('model-output').textContent, 'No response recorded.');
const observe = index => {
  element('observation-step').value = String(index);
  element('observation-step').listeners.change();
};
const selectCall = id => element('history').listeners.click({ target: { closest: () => ({ dataset: { callId: String(id) } }) } });
observe(0);
assert.equal(element('screenshot').src, '/frame-0.jpg');
assert.equal(element('choice-title').textContent, 'Old link');
assert.match(element('operation-choices').innerHTML, /CLICK <b>80%/);
assert.match(element('choices').innerHTML, /Old link/);
assert.doesNotMatch(element('choices').innerHTML, /LATEST WRONG|Search field/);
assert.equal(JSON.parse(element('model-state').textContent).state.page.text, 'Old link');
assert.match(element('history').innerHTML, /trace-row active" data-call-id="1"/);
selectCall(3);
assert.equal(element('observation-step').value, '1', 'same step number must not alias distinct observations');
assert.equal(element('screenshot').src, '/frame-1.jpg');
assert.equal(element('choice-title').textContent, 'Search field');
assert.match(element('operation-choices').innerHTML, /TYPE_TEXT <b>90%/);
assert.equal(JSON.parse(element('model-output').textContent).text, 'Alan Turing');
run('render()');
assert.equal(element('observation-step').value, '1');
assert.equal(JSON.parse(element('model-output').textContent).text, 'Alan Turing', 'poll preserves helper selection');
selectCall(4);
assert.equal(element('observation-step').value, '2');
assert.equal(element('choice-title').textContent, 'DONE');
observe(1);
assert.equal(JSON.parse(element('model-state').textContent).state.page.text, 'Search field');
observe(3);
assert.equal(element('operation-choices').innerHTML, '');
assert.equal(element('choices').innerHTML, '');
assert.equal(element('model-state').textContent, 'No request recorded.');
context.fixture.model_calls.push({ id: 5, observation_index: 1, kind: 'laya', status: 'error', error: 'HTTP 502',
  request: { state: { elements: [] } } });
selectCall(5);
assert.equal(element('observation-step').value, '1');
assert.equal(element('operation-choices').innerHTML, '', 'failed call does not borrow another decision');
assert.equal(JSON.parse(element('model-output').textContent).error, 'HTTP 502');

const historicalPage = context.fixture.observations[0].page;
Object.assign(historicalPage, { w: 1000, h: 500, actions: [
  { kind: 'click', node: 10, label: 'Missing rectangle' },
  { kind: 'fill', node: 20, rect: { x: 100, y: 50, w: 200, h: 30 } },
  { kind: 'click', node: 20, rect: { x: 100, y: 50, w: 200, h: 30 } },
  { kind: 'select', node: 30, rect: { x: 500, y: 250, w: 100, h: 40 } },
  { kind: 'wait', node: 40, rect: { x: 1, y: 1, w: 1, h: 1 } },
] });
context.fixture.model_calls[0].request.state.elements = ['Missing', 'Input', 'Select'].map((label, i) => ({
  index: String(i + 1), label, operations: ['CLICK'], role: 'button',
}));
context.fixture.model_calls[0].target = '2';
observe(0);
assert.equal(element('targets').hidden, false);
assert.equal(element('viewport').style['aspect-ratio'], '1000 / 500');
assert.equal((element('targets').innerHTML.match(/class="target /g) || []).length, 2, 'editable element gets one box');
assert.doesNotMatch(element('targets').innerHTML, /data-action="1"|data-action="4"|NaN/);
assert.match(element('targets').innerHTML, /data-action="2" style="left:10%;top:10%;width:20%;height:6%/);
assert.match(element('targets').innerHTML, /data-action="3" style="left:50%;top:50%/);
overlayNodes = ['2', '3'].map(index => ({ dataset: { action: index }, selected: false,
  classList: { toggle(name, value) { overlayNodes.find(n => n.dataset.action === index).selected = value; } },
}));
element('choices').listeners.pointerover({ target: { closest: () => ({ dataset: { action: '3' } }) } });
assert.deepEqual(overlayNodes.map(n => n.selected), [true, true], 'hover and historical decision are highlighted');
run('render()');
assert.match(element('targets').innerHTML, /target selected" data-action="3"/, 'polling preserves hover');
element('choices').listeners.pointerleave();
assert.deepEqual(overlayNodes.map(n => n.selected), [true, false], 'leave restores historical selection, not latest decision');
element('overlays').checked = false;
element('overlays').listeners.change();
assert.equal(element('targets').hidden, true);
element('overlays').checked = true;
element('overlays').listeners.change();
assert.equal(element('targets').hidden, false);
context.fixture.observations[3].elements = context.fixture.model_calls[0].request.state.elements;
context.fixture.observations[3].page = JSON.parse(JSON.stringify(historicalPage));
context.fixture.observations[3].page.actions[1].rect.x = 300;
observe(3);
assert.match(element('choices').innerHTML, /Input/);
assert.match(element('targets').innerHTML, /data-action="2" style="left:30%/);
assert.doesNotMatch(element('targets').innerHTML, /target selected/);
assert.equal(element('operation-choices').innerHTML, '', 'final observation is unranked');
delete context.fixture.observations[3].screenshot_url;
run('render()');
element('overlays').listeners.change();
assert.equal(element('targets').hidden, true, 'no boxes floating over missing screenshot');
context.fixture.model_calls.push({ id: 6, observation_index: 0, kind: 'deepseek', status: 'success', model: 'deepseek-flash',
  operation: 'CLICK', target: '2', decision_input: { state: { elements: context.fixture.model_calls[0].request.state.elements } },
  request: { messages: [{ role: 'user', content: '{"state":{},"questions":{}}' }] },
  response: { choices: [{ message: { content: '{"operation":"CLICK","click_target":"2"}' } }] },
  decision: { provider: 'deepseek', operation: 'CLICK', target: '2', choice: 'e2',
    operation_probabilities: {}, target_probabilities: {}, target_confidence: null, latency_ms: 20 },
});
selectCall(6);
assert.equal(element('observation-step').value, '0');
assert.match(element('selected-call').textContent, /DeepSeek \(System1\)/);
assert.match(element('ranking-note').textContent, /概率未提供/);
assert.equal(element('operation-choices').innerHTML, '');
assert.equal(element('confidence').textContent, '—');
assert.equal(element('choice-title').textContent, 'Input');
assert.match(element('choices').innerHTML, /choice best" data-action="2"/);
assert.doesNotMatch(element('choices').innerHTML, /100%/);
assert.match(element('question-contexts').innerHTML, /questions/);
context.fixture.model_calls.push({ id: 7, observation_index: 0, decision_call_id: 6,
  kind: 'text', status: 'success', request: {}, response: { text: 'helper' } });
selectCall(7);
assert.equal(element('choice-title').textContent, 'Input');
assert.match(element('ranking-note').textContent, /DeepSeek/);
assert.equal(JSON.parse(element('model-output').textContent).text, 'helper');
const cloudCall = context.fixture.model_calls.find(c => c.id === 6);
context.fixture.model_calls.push({ ...cloudCall, id: 8, decision_round: 2, stage: 'operation', target: null,
  decision: { ...cloudCall.decision, target: null, choice: 'CLICK' },
  response: { choices: [{ message: { content: '{"operation":"CLICK"}' } }] },
});
selectCall(8);
assert.match(element('question-contexts').innerHTML, /操作选择/);
assert.match(element('question-contexts').innerHTML, /目标调用尚未发出/);
assert.doesNotMatch(element('choices').innerHTML, /choice best/);
context.fixture.model_calls.push({ ...cloudCall, id: 9, decision_round: 2, stage: 'click_target' });
run('render()');
assert.equal(element('choice-title').textContent, 'Input');
assert.match(element('stage-requests').innerHTML, /messages/);
assert.match(element('stage-outputs').innerHTML, /click_target/);
assert.equal(element('model-state').hidden, true);
selectCall(9);
assert.equal(element('observation-step').value, '0');
assert.equal(element('choice-title').textContent, 'Input');
context.fixture.model_calls.push({ ...cloudCall, id: 10, decision_round: 3, stage: 'operation', operation: 'DONE',
  decision: { ...cloudCall.decision, operation: 'DONE', target: null, choice: 'DONE' },
});
selectCall(10);
assert.match(element('question-contexts').innerHTML, /该操作无需目标调用/);
selectCall(7);
assert.equal(element('stage-outputs').hidden, true);
assert.equal(element('model-output').hidden, false);
console.log('Inspector linkage tests passed');
