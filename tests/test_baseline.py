"""Offline worker, persistence and ownership contracts. No model APIs."""

import json
import threading
import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import baseline, demo
from jev_ultrafast.agent import RunCancelled
from jev_ultrafast.baseline import BaselineConflict, BaselineManager


def factory(*, initially_met=False, error=None, hold=None, mutation=True, screenshot_error=False):
    class FakeAgent:
        def __init__(self, url, goal, *, on_event, should_cancel, prepare, **kwargs):
            self.callback, self.cancel = on_event, should_cancel
            self.browser = Mock()
            self.browser.call.return_value = {"data": "eA=="}
            if screenshot_error:
                self.browser.call.side_effect = TimeoutError("screenshot timeout")
            self.state = {"page": {"url": url, "title": "Test", "text": "", "actions": [],
                                   "met": initially_met},
                          "goal": goal, "status": "ready", "history": [], "model_calls": [], "decisions": []}
            prepare(self.browser)
            self.observe("initial")

        def snapshot(self):
            return deepcopy(self.state)

        def observe(self, phase="step"):
            self.callback("observation", self, {"phase": phase})

        def command(self, name):
            call = {"kind": "laya", "id": 1, "status": "pending"}
            self.state["model_calls"].append(call)
            self.callback("model_start", self, {"call": call})
            if hold:
                hold[0].set()
                assert hold[1].wait(5)
            if error:
                call["status"] = "error"
                self.callback("model_end", self, {"call": call})
                raise error
            call["status"] = "success"
            self.callback("model_end", self, {"call": call})
            if reason := self.cancel():
                raise RunCancelled(reason)
            if mutation:
                self.callback("action_started", self, {"action": {"label": "Click", "kind": "click"}})
                action = {"action": "Click", "kind": "click", "node": 1}
                self.state["history"].append(action)
                self.callback("action_executed", self, {"action": action})
                self.state["page"]["met"] = True
                self.observe()
            self.state["status"] = "done"

        def close(self):
            self.browser.close()

    return FakeAgent


def evaluator(task, browser, page, history):
    return {"outcome": "met" if page["met"] else "not_met", "checks": {"met": page["met"]},
            "evidence": {}, "violations": []}


def manager(tmp_path, **kwargs):
    return BaselineManager(tmp_path / "runs", "http://127.0.0.1:9999", inspector=evaluator,
                           preparer=lambda *_: None, **kwargs)


def finish(run):
    run.thread.join(5)
    assert not run.thread.is_alive()
    assert not run.active
    return run.state()["batch"]


def test_sequential_repeated_runs_and_idempotent_save(tmp_path):
    run = manager(tmp_path, agent_factory=factory())
    state = run.start([run.tasks[1]["id"], run.tasks[0]["id"]], 2)
    batch = finish(run)
    assert [r["task_id"] for r in batch["attempts"]] == [run.tasks[0]["id"]] * 2 + [run.tasks[1]["id"]] * 2
    assert all(r["status"] == "passed" for r in batch["attempts"])
    path = run.folder(batch["id"])
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["limits"] == {"actions": 30, "decision_rounds": 60, "decision_calls": 60, "seconds": 300}
    assert {p.name for p in path.iterdir()} >= {"manifest.json", "summary.json", "summary.csv", "report.zh.md"}
    row = batch["attempts"][0]
    trace = run.read_attempt(batch["id"], row["id"])
    assert len(trace["observations"]) == 3  # initial, post-action, final (once)
    assert trace["model_calls"][0]["observation_index"] == 0
    assert trace["observations"][0]["page"]["met"] is False
    assert trace["observations"][1]["page"]["met"] is True
    assert row["evaluation"]["first_met_step"] == 1
    event_lines = (path / row["id"] / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line)["event"] for line in event_lines]
    assert events.index("action_started") < events.index("action_executed")
    assert events.index("action_executed") < events.index("observed", events.index("action_executed"))
    assert run.save()["saved_path"] == run.save()["saved_path"] == state["batch"]["path"]
    assert len(list(run.root.iterdir())) == 1
    assert run.frame(batch["id"], row["id"], "000000.jpg") == b"x"


def test_legacy_trace_links_are_reconstructed_without_rewriting_archive(tmp_path):
    run = manager(tmp_path, agent_factory=factory())
    run.start([run.tasks[0]["id"]])
    batch = finish(run)
    attempt_id = batch["attempts"][0]["id"]
    path = run.attempt_folder(batch["id"], attempt_id) / "trace.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    for frame in old["observations"]:
        frame.pop("page")
    old["model_calls"][0].pop("observation_index")
    baseline.atomic_json(path, old)
    original = path.read_bytes()
    run.snapshots.clear()
    restored = run.read_attempt(batch["id"], attempt_id)
    assert restored["model_calls"][0]["observation_index"] == 0
    assert restored["observations"][0]["page"]["met"] is False
    assert restored["observations"][1]["page"]["met"] is True
    assert path.read_bytes() == original


def test_helper_links_to_laya_on_same_observation(tmp_path):
    class HelperAgent(factory(initially_met=True, mutation=False)):
        def command(self, name):
            for ident, kind in ((1, "laya"), (2, "text")):
                call = {"id": ident, "kind": kind, "status": "pending"}
                self.state["model_calls"].append(call)
                self.callback("model_start", self, {"call": call})
            self.state["status"] = "done"

    run = manager(tmp_path, agent_factory=HelperAgent)
    run.start([run.tasks[0]["id"]])
    batch = finish(run)
    attempt_id = batch["attempts"][0]["id"]
    trace = run.read_attempt(batch["id"], attempt_id)
    assert [c["observation_index"] for c in trace["model_calls"]] == [0, 0]
    assert trace["model_calls"][1]["laya_call_id"] == 1
    for call in trace["model_calls"]:
        call.pop("observation_index")
        call.pop("laya_call_id", None)
    baseline.atomic_json(run.attempt_folder(batch["id"], attempt_id) / "trace.json", trace)
    run.snapshots.clear()
    restored = run.read_attempt(batch["id"], attempt_id)
    assert restored["model_calls"][1]["laya_call_id"] == 1
    assert restored["model_calls"][1]["observation_index"] == 0


def test_cloud_batch_pins_provider_and_keeps_separate_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEM1_DEEPSEEK_API_KEY", "cloud-key")
    providers = []

    class CloudAgent(factory(initially_met=True, mutation=False)):
        def __init__(self, *args, system1_provider, **kwargs):
            providers.append(system1_provider)
            super().__init__(*args, **kwargs)

        def command(self, name):
            self.state["decision_rounds"] = 1
            for ident, kind, stage in ((1, "deepseek", "operation"), (2, "deepseek", "type_text_target"),
                                       (3, "text", None)):
                call = {"id": ident, "kind": kind, "status": "success", "decision_round": 1, "stage": stage}
                self.state["model_calls"].append(call)
                self.callback("model_start", self, {"call": call})
            self.state["status"] = "done"

    run = manager(tmp_path, agent_factory=CloudAgent)
    run.start([run.tasks[0]["id"]], repeats=2, system1_provider="deepseek")
    batch = finish(run)
    assert providers == ["deepseek", "deepseek"]
    assert batch["system1"]["provider"] == "deepseek"
    for row in batch["attempts"]:
        assert row["decision_calls"] == row["deepseek_calls"] == 2
        assert row["decision_rounds"] == row["text_calls"] == 1
        assert row["laya_calls"] == 0
        assert row["system1_provider"] == "deepseek"
        trace = run.read_attempt(batch["id"], row["id"])
        assert trace["model_calls"][2]["decision_call_id"] == 2
        assert "laya_call_id" not in trace["model_calls"][2]
    manifest = json.loads((run.folder(batch["id"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["system1"]["provider"] == "deepseek"
    assert manifest["limits"]["decision_rounds"] == 60
    assert manifest["limits"]["decision_calls"] == 120
    assert "cloud-key" not in json.dumps(manifest)


def test_stop_and_poll_do_not_wait_for_model_or_repeat_input(tmp_path):
    started, release = threading.Event(), threading.Event()
    run = manager(tmp_path, agent_factory=factory(hold=(started, release)))
    run.start([t["id"] for t in run.tasks[:2]])
    try:
        assert started.wait(3)
        before = time.monotonic()
        assert run.state()["active"]
        with pytest.raises(BaselineConflict):
            run.start()
        run.stop()
        assert time.monotonic() - before < 1
    finally:
        release.set()
    batch = finish(run)
    assert all(r["status"] == "cancelled" for r in batch["attempts"])
    assert batch["attempts"][0]["actions"] == 0


@pytest.mark.parametrize("repeats", [0, 11, True, "3"])
def test_invalid_repeats_do_not_start(tmp_path, repeats):
    run = manager(tmp_path)
    with pytest.raises(ValueError):
        run.start(repeats=repeats)
    assert not run.active


def test_paths_are_whitelisted_and_no_new_run_on_read(tmp_path):
    run = manager(tmp_path, agent_factory=factory())
    run.start([run.tasks[0]["id"]])
    batch = finish(run)
    for value in ("../secret", "C:\\Windows", None):
        with pytest.raises(ValueError):
            run.read_run(value)
    with pytest.raises(ValueError):
        run.read_attempt(batch["id"], "../../secret")
    with pytest.raises(ValueError):
        run.frame(batch["id"], batch["attempts"][0]["id"], "../manifest.json")
    with pytest.raises(ValueError):
        run.start(["../../secret"])
    assert len(run.runs()) == 1


def test_normal_failure_continues_but_service_failure_cancels_queue(tmp_path):
    for index, error in enumerate((ValueError("bad model choice"), RuntimeError("Model connection failed"))):
        run = manager(tmp_path / str(index), agent_factory=factory(error=error))
        run.start([t["id"] for t in run.tasks[:2]])
        batch = finish(run)
        assert batch["attempts"][0]["status"] == "error"
        assert batch["attempts"][1]["status"] == ("cancelled" if index else "error")
        assert run.read_attempt(batch["id"], batch["attempts"][0]["id"])["model_calls"]


def test_initial_completion_and_missing_screenshot(tmp_path):
    run = manager(tmp_path, agent_factory=factory(initially_met=True, mutation=False, screenshot_error=True))
    run.start([run.tasks[1]["id"]])
    batch = finish(run)
    row = batch["attempts"][0]
    assert row["status"] == "passed"
    assert row["evaluation"]["first_met_step"] == row["evaluation"]["extra_actions"] == 0
    trace = run.read_attempt(batch["id"], row["id"])
    assert all(o["screenshot_error"] for o in trace["observations"])


def test_timeout_and_early_done(tmp_path):
    run = manager(tmp_path, agent_factory=factory(), timeout=0)
    run.start([run.tasks[0]["id"]])
    assert finish(run)["attempts"][0]["status"] == "timeout"
    other = manager(tmp_path / "other", agent_factory=factory(mutation=False))
    other.start([other.tasks[0]["id"]])
    row = finish(other)["attempts"][0]
    assert row["status"] == "failed"
    assert row["evaluation"]["early_stop"]


def test_restart_marks_incomplete_without_resuming(tmp_path):
    run = manager(tmp_path, agent_factory=factory())
    run.start([run.tasks[0]["id"]])
    batch = finish(run)
    batch["status"] = "running"
    batch["attempts"][0]["status"] = "running"
    run.write_summary(batch)
    fresh = manager(tmp_path, agent_factory=Mock(side_effect=AssertionError("must not run")))
    assert fresh.state()["batch"]["status"] == "interrupted"
    assert fresh.state()["batch"]["attempts"][0]["status"] == "interrupted"
    assert not fresh.active


def test_baseline_ownership_rejects_manual_commands(monkeypatch):
    monkeypatch.setattr(demo, "BASELINE", Mock(active=True))
    for command in ("reset", "predict", "act", "tick"):
        with pytest.raises(BaselineConflict):
            demo.command(command, {})


def test_credential_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("LAYA_API_KEY", "sensitive-key")
    run = manager(tmp_path, agent_factory=factory(error=ValueError("oops sensitive-key")))
    run.start([run.tasks[0]["id"]])
    finish(run)
    for path in run.root.rglob("*"):
        if path.suffix in {".json", ".jsonl", ".csv", ".md"}:
            assert "sensitive-key" not in path.read_text(encoding="utf-8-sig")
    assert "sensitive-key" not in json.dumps(run.state())


def test_first_completion_then_leaving_is_not_success(tmp_path):
    class LeavesGoal(factory(initially_met=True, mutation=False)):
        def command(self, name):
            self.state["page"]["met"] = False
            self.state["history"].append({"kind": "click", "action": "Back"})
            self.observe()
            self.state["status"] = "done"

    run = manager(tmp_path, agent_factory=LeavesGoal)
    run.start([run.tasks[0]["id"]])
    row = finish(run)["attempts"][0]
    assert row["status"] == "failed"
    assert row["evaluation"]["first_met_step"] == 0
    assert row["evaluation"]["extra_actions"] == 1
    assert row["evaluation"]["final"]["outcome"] == "not_met"


def test_unknown_evaluation_is_never_passed(tmp_path):
    run = manager(tmp_path, agent_factory=factory(initially_met=True, mutation=False))
    run.inspector = Mock(side_effect=ValueError("Unrecognized DOM"))
    run.start([run.tasks[0]["id"]])
    assert finish(run)["attempts"][0]["status"] == "evaluation_unknown"


def test_archive_failure_still_closes_owned_tab(tmp_path, monkeypatch):
    instances = []
    write = baseline.atomic_json

    class FailDuringModel(factory()):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

        def command(self, name):
            def fail_trace(path, data):
                if path.name == "trace.json":
                    raise OSError("Disk full")
                return write(path, data)

            monkeypatch.setattr(baseline, "atomic_json", fail_trace)
            super().command(name)

    run = manager(tmp_path, agent_factory=FailDuringModel)
    run.start([run.tasks[0]["id"]])
    batch = finish(run)
    assert batch["status"] == "error"
    instances[0].browser.close.assert_called_once()
    events = (run.folder(batch["id"]) / batch["attempts"][0]["id"] / "events.jsonl").read_text(encoding="utf-8")
    assert '"event": "model_start"' in events
