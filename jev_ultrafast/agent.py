"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import time
from functools import partial
from pathlib import Path

from .browser import Browser, StalePage
from .model import action_space, choose, field_context, field_text, system1_config
from .questions import MAX_STEPS


class RunCancelled(RuntimeError):
    """Cooperative cancellation at a boundary before browser input."""


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False,
                 on_event=None, should_cancel=None, prepare=None, system1_provider=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        config = system1_config(system1_provider, require_key=True)
        self.pending_text = None
        self.on_event = on_event
        self.should_cancel = should_cancel
        self.browser = Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            if prepare:
                prepare(self.browser)
            self.check_cancelled()
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            system1_provider=config["provider"],
            system1_model=config["model"],
            system1_base_url=config["base_url"],
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            status="ready",
            plan=plan,
            plan_index=0,
            decisions=[],
            decision_rounds=0,
            text_calls=[],
            model_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        try:
            self.emit("observation", phase="initial")
        except Exception:
            self.browser.close()
            raise
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def check_cancelled(self):
        callback = getattr(self, "should_cancel", None)
        reason = callback() if callback else None
        if reason:
            raise RunCancelled(str(reason))

    def emit(self, event, **payload):
        callback = getattr(self, "on_event", None)
        if callback:
            callback(event, self, payload)
        elif event == "observation":
            page = self.state["page"]
            self.state.setdefault("observations", []).append({
                "page": page, "phase": payload.get("phase"), "step": len(self.state["history"]),
                "elements": action_space(page["actions"])[0],
            })

    def observe(self, phase="step"):
        self.state["page"] = self.state["browser"].observe(screenshot=self.screenshots)
        self.emit("observation", phase=phase)

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
        }

    def call_model(self, kind, function, *args, **metadata):
        calls = self.state.setdefault("model_calls", [])
        record = dict(id=len(calls) + 1, kind=kind, status="pending", **metadata)
        if self.state.get("observations"):
            record["observation_index"] = len(self.state["observations"]) - 1
        calls.append(record)
        started = time.perf_counter()
        self.emit("model_start", call=record)
        try:
            result = function(*args, trace=record)
            record["status"] = "success"
            if kind in {"laya", "deepseek", "jev"}:
                record.update(operation=result["operation"], target=result["target"])
                record["decision"] = {k: result.get(k) for k in (
                    "choice", "operation", "target", "confidence", "target_confidence", "latency_ms",
                    "operation_probabilities", "target_probabilities", "provider",
                )}
            return result
        except Exception as error:
            record.update(status="error", error=str(error))
            raise
        finally:
            record["latency_ms"] = round((time.perf_counter() - started) * 1000)
            if "decision" in record:
                record["decision"]["latency_ms"] = record["latency_ms"]
            self.emit("model_end", call=record)

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        self.check_cancelled()
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["decision"] = None
                state["status"] = "ready"
                self.observe("stale_retry")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                self.observe("refresh")
            self.check_cancelled()
            state["decision"] = None
            if state["status"] in {"done", "blocked"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            rounds = state.get("decision_rounds", len(state["decisions"]))
            if rounds >= MAX_STEPS * 2:
                raise ValueError("Reached the demo's decision-round budget")
            state["decision_rounds"] = rounds + 1
            provider = state.get("system1_provider", "laya")
            def stage_call(function, question):
                self.check_cancelled()
                if not state["browser"].fresh(state["page"]):
                    raise StalePage("Page changed between decision stages. Choose again.")
                return self.call_model("deepseek", function, question,
                                       decision_round=rounds + 1, stage=question)

            if provider == "deepseek":
                decision = choose(state["page"], state["goal"], state["history"],
                                  provider=provider, stage_call=stage_call)
            else:
                chooser = partial(choose, provider="jev") if provider == "jev" else choose
                decision = self.call_model(provider, chooser, state["page"], state["goal"], state["history"],
                                           decision_round=rounds + 1)
            self.check_cancelled()
            state["decision"] = decision
            state["decisions"].append(
                {
                    **state["decision"],
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if len(state["history"]) >= MAX_STEPS:
                state["status"] = "blocked"
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] == "fill":
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before text generation. Choose again.")
                context = field_context(state["goal"], action, page, state["history"])
                if self.pending_text and self.pending_text[0] == context:
                    _, text, helper = self.pending_text
                else:
                    text, helper = self.call_model(
                        "text", field_text, context, field=action["label"],
                        decision_round=state.get("decision_rounds"),
                        decision_call_id=next((c["id"] for c in reversed(state.get("model_calls", []))
                                               if c["kind"] in {"laya", "deepseek", "jev"}), None),
                    )
                    self.pending_text = (context, text, helper)
                    state["text_calls"].append({**helper, "field": action["label"], "value": text})
            # Browser.act checks freshness immediately before input, including after text generation.
            self.check_cancelled()
            self.emit("action_started", action=action, text=text)
            state["browser"].act(action, page, text=text)
            self.pending_text = None
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            # Record execution before observing. A stale post-action observation must not erase the action.
            state["history"].append(
                {
                    "step": len(state["history"]) + 1,
                    "action": action["label"],
                    "kind": action["kind"],
                    "choice": selected,
                    "node": action.get("node"),
                    "role": action.get("role"),
                    "previous_value": action.get("value"),
                    "previous_checked": action.get("checked"),
                    "probability": decision["probabilities"].get(selected),
                    "confidence": decision["confidence"],
                    "latency_ms": decision["latency_ms"],
                    "text": text,
                    "text_helper": helper["model"] if helper else None,
                    "text_latency_ms": helper["latency_ms"] if helper else 0,
                    "operation": decision["operation"],
                    "target": decision["target"],
                    "page_changed": None,
                    "url": page["url"],
                    "usage": decision["usage"],
                    "executed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                    "elapsed_ms": state["elapsed_ms"],
                }
            )
            self.emit("action_executed", action=state["history"][-1])
            self.observe()
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=state["page"]["fingerprint"] != page["fingerprint"],
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            state["status"] = (
                "blocked"
                if len(repeated) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                else "ready"
            )
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked"}:
            yield self.command("tick")

    def close(self):
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
