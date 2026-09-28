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
function formatPayload(payload) {
  return JSON.stringify(payload, (key, value) => {
    if (key === "content" && typeof value === "string") {
      try { return JSON.parse(value); } catch { /* Plain-text message. */ }
    }
    return value;
  }, 2);
}
function renderModelCalls() {
  const calls = state.model_calls || [];
  const selected = calls.find(c => c.id === selectedCallId) || calls.at(-1);
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
  $("raw-request-panel").open = !hasContexts;
  $("question-contexts").innerHTML = hasContexts
    ? `<p class="context-total">${contexts.length} sequences / ${contexts.reduce((n, c) => n + c.input_tokens, 0)} input tokens total</p>` + contexts.map(c =>
      `<section class="question-context"><h3>${escape(c.question)}</h3><p class="context-metrics">Pass ${escape(c.pass)} / <strong>${escape(c.input_tokens)} / ${escape(c.max_tokens)} tokens</strong></p><pre>${escape(c.context)}</pre></section>`
    ).join('')
    : selected?.kind === 'laya' ? '<p class="muted">Per-question context unavailable in this response.</p>' : '';
  $("model-output").textContent = selected?.error
    ? formatPayload({ response: selected.response ?? null, error: selected.error })
    : selected?.response != null ? formatPayload(selected.response) : 'No response recorded.';
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
  if (name === "reset") selectedCallId = null;
  render();
  return data;
}
function controls() {
  const live = state?.page && !["done", "blocked"].includes(state.status);
  $("start").disabled = busy;
  $("scenario").disabled = busy;
  $("website-url").disabled = busy;
  $("goal").disabled = busy;
  $("choose").disabled = busy || !live;
  $("execute").disabled = busy || !state?.decision || !live;
  $("auto").disabled = busy || !live;
  $("auto").hidden = automatic;
  $("stop").hidden = !automatic;
  $("download").disabled = !state?.model_calls?.length && !state?.history?.length;
}
async function perform(fn, label) {
  if (busy) return;
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
  $("laya-address").textContent = state.laya_base_url || "Unavailable";
  $("text-model-address").textContent = state.text_model_base_url || "Unavailable";
  $("helper").textContent = `Text helper · ${state.text_model}`;
  $("plan").innerHTML = (state.plan || [])
    .map(
      (goal, i) =>
        `<div class="plan-step ${i === state.plan_index ? "current" : ""}"><span>${i < state.plan_index ? "✓" : i + 1}</span>${escape(goal)}</div>`,
    )
    .join("");
  const page = state.page,
    d =
      state.decision ||
      (state.status === "done" ? state.decisions?.at(-1) : null);
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
    controls();
    return;
  }
  $("empty").hidden = true;
  $("screenshot").hidden = false;
  $("screenshot").src = `data:image/jpeg;base64,${page.screenshot}`;
  $("url").textContent = page.url;
  $("page-title").textContent = page.title;
  $("action-count").textContent = `${state.elements.length} elements`;
  const chosen = page.actions.find((a) => a.id === d?.choice);
  $("choice-title").textContent = d
    ? chosen?.label || d.choice
    : "Choose an action";
  $("latency").textContent = d ? `${d.latency_ms} ms` : "—";
  $("confidence").textContent = d?.target_confidence != null ? percent(d.target_confidence) : "—";
  $("completion").textContent = d ? d.operation : "—";
  $("ranking-note").textContent = d ? "Ranked by Jev" : "Unranked";
  const op = Object.entries(d?.operation_probabilities || {}).sort((a,b)=>b[1]-a[1]);
  $("operation-choices").innerHTML = op.map(([name,p]) =>
    `<span class="operation-choice ${name === d.operation ? 'best' : ''}">${escape(name)} <b>${percent(p)}</b></span>`).join('');
  const probability = e => d?.target_probabilities[e.index] ??
    Math.max(-1, ...(e.options || []).map(o=>d?.target_probabilities[o.index] ?? -1));
  const selectedIndex = d?.target?.split(':')[0];
  const elements = [...state.elements];
  if (d) elements.sort((a,b)=>probability(b)-probability(a));
  $("choices").innerHTML = elements.map(e => {
    const p = probability(e);
    return `<div class="choice ${selectedIndex === e.index ? 'best' : ''}" data-action="${escape(e.index)}"><span class="choice-id">[${escape(e.index)}]</span><div class="choice-label">${escape(e.label)}<small>${escape(e.role)} · ${escape(e.operations.join(' / '))}${e.value ? ' · '+escape(e.value) : ''}${e.checked !== undefined ? ' · checked '+escape(e.checked) : ''}</small>${p >= 0 ? `<div class="bar" style="--probability:${p*100}%"></div>` : ''}</div><span class="probability">${p >= 0 ? percent(p) : '—'}</span></div>`;
  }).join('');
  const targets = new Map();
  for (const a of page.actions) if (a.rect && !targets.has(a.node)) targets.set(a.node, a);
  $("targets").innerHTML = [...targets.values()].map((a,i) => {
    const index=String(i+1);
    return `<div class="target ${index === selectedIndex ? 'selected' : ''}" data-action="${index}" style="left:${100*a.rect.x/page.w}%;top:${100*a.rect.y/page.h}%;width:${100*a.rect.w/page.w}%;height:${100*a.rect.h/page.h}%"><span>${index}</span></div>`;
  }).join('');
  $("targets").hidden = !$("overlays").checked;
  controls();
}
$("history").addEventListener("click", (event) => {
  const row = event.target.closest("[data-call-id]");
  if (!row) return;
  selectedCallId = Number(row.dataset.callId);
  renderModelCalls();
  $("model-input-panel").open = true;
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
$("overlays").addEventListener("change", () => {
  $("targets").hidden = !$("overlays").checked;
});
$("choices").addEventListener("pointerover", (event) => {
  const id = event.target.closest("[data-action]")?.dataset.action;
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === id || t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    );
});
$("choices").addEventListener("pointerleave", () =>
  document
    .querySelectorAll(".target")
    .forEach((t) =>
      t.classList.toggle(
        "selected",
        t.dataset.action === state?.decision?.target?.split(':')[0],
      ),
    ),
);
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
    state = s;
    render();
  })
  .catch(() => {
    $("status").textContent = "Cannot reach local demo server";
    $("laya-address").textContent = "Unavailable";
    $("text-model-address").textContent = "Unavailable";
  });
