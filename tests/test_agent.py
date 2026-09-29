"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid Laya"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


@pytest.mark.parametrize("configured", [False, True])
def test_laya_endpoint_and_authentication(monkeypatch, configured):
    for name in ("LAYA_BASE_URL", "LAYA_MODEL", "LAYA_API_KEY", "LAYA_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://unused.example")
    monkeypatch.setenv("TYPESAFE_API_KEY", "must-not-leak")
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-latest")
    if configured:
        monkeypatch.setenv("LAYA_BASE_URL", "http://localhost:9999/")
        monkeypatch.setenv("LAYA_MODEL", "laya-custom")
        monkeypatch.setenv("LAYA_API_KEY", "local-test")
        monkeypatch.setenv("LAYA_TIMEOUT_SECONDS", "180")
    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "laya-loaded-checkpoint",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "click_target": choice(["1", "2"], "2"),
            },
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(model, "CLIENT", client)
        result = model.choose(page(), "Open Search", [])
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == (
        "http://localhost:9999/v1/systemone" if configured
        else "http://127.0.0.1:8791/v1/systemone"
    )
    assert request.headers.get("Authorization") == ("Bearer local-test" if configured else None)
    assert request.extensions["timeout"]["read"] == (180 if configured else 120)
    assert json.loads(request.content)["model"] == ("laya-custom" if configured else "laya-v10s")
    assert result["choice"] == "e3"
    assert result["model"] == "laya-loaded-checkpoint"


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body, **kwargs):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body, **kwargs):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid Laya"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body, **kwargs):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


def test_quoted_task_text_still_uses_the_llm(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.field_context('Fly from "Zurich" to London', page()["actions"][0], page(), [])
    assert model.field_text(context)[0] == "Zurich"
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": 'Enter "Zurich"'})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "status": "predicted",
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_generated_text_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert len(runner.state["model_calls"]) == 1
    assert runner.state["browser"].act.call_count == 2  # The first call rejects before any browser input.
    assert runner.pending_text is None


def test_changed_field_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=("book", {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_text", helper)
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.state["decision"] = decision()
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_text({"goal": "Find a flight"})


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def test_trace_keeps_separate_complete_model_calls(runner, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "secret-test-key")
    laya = {"model": "laya", "answers": {
        "operation": choice(["TYPE_TEXT", "CLICK", "WAIT", "DONE", "BLOCKED"], "TYPE_TEXT"),
        "type_text_target": choice(["1"], "1"),
    }}
    helper = {"choices": [{"message": {"content": '{"text":"book"}'}}], "usage": {"total_tokens": 5}}
    post = Mock(side_effect=[laya, helper])
    monkeypatch.setattr(model, "post_json", post)
    runner.command("tick")
    calls = runner.snapshot()["model_calls"]
    assert [c["kind"] for c in calls] == ["laya", "text"]
    assert [c["id"] for c in calls] == [1, 2]
    assert all(c["status"] == "success" for c in calls)
    assert calls[0]["request"] == post.call_args_list[0].args[2]
    assert calls[1]["request"] == post.call_args_list[1].args[2]
    assert calls[0]["response"] == laya
    assert calls[1]["response"] == helper
    assert calls[1]["request"]["messages"][0]["content"] == model.TEXT_VALUE
    assert json.loads(calls[1]["request"]["messages"][1]["content"])["goal"] == "Find a book"
    assert "secret-test-key" not in json.dumps(calls)
    laya["answers"].clear()
    runner.state["page"]["text"] = "changed"
    assert calls[0]["response"]["answers"]
    assert calls[0]["request"]["state"]["page"]["text"] == "Search"


@pytest.mark.parametrize("network_error", [False, True])
def test_failed_helper_trace_preserves_request_and_response(runner, monkeypatch, network_error):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    raw = {"choices": [{"message": {"content": '{"text":null}'}}]}
    post = Mock(side_effect=RuntimeError("Connection failed")) if network_error else Mock(return_value=raw)
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises((ValueError, RuntimeError)):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    record = runner.state["model_calls"][0]
    assert record["status"] == "error"
    assert record["request"]["messages"]
    assert record.get("response") == (None if network_error else raw)
    assert record["error"]
    runner.state["browser"].act.assert_not_called()


def test_cancellation_after_text_generation_never_types(runner, monkeypatch):
    stop = False

    def helper(*args, **kwargs):
        nonlocal stop
        stop = True
        return "book", {"model": "test", "latency_ms": 1}

    runner.should_cancel = lambda: "cancelled" if stop else None
    monkeypatch.setattr(loop, "field_text", helper)
    with pytest.raises(loop.RunCancelled):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


@pytest.mark.parametrize("count", [29, 30])
def test_action_budget_allows_thirtieth_but_not_thirty_first(runner, count):
    runner.state["history"] = [{"page_changed": True, "kind": "click"}] * count
    runner.state["decision"] = decision("e3")
    if count == 30:
        with pytest.raises(ValueError, match="30-action"):
            runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        runner.state["browser"].act.assert_not_called()
    else:
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        assert len(runner.state["history"]) == 30
        runner.state["browser"].act.assert_called_once()


def test_done_is_allowed_at_action_budget(runner):
    runner.state["history"] = [{}] * 30
    runner.state["decision"] = decision("DONE")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "done"
    runner.state["browser"].act.assert_not_called()


@pytest.mark.parametrize("count", [59, 60])
def test_decision_budget_allows_sixtieth_but_not_sixty_first(runner, monkeypatch, count):
    choose = Mock(return_value=decision("DONE"))
    monkeypatch.setattr(loop, "choose", choose)
    runner.state["decisions"] = [{}] * count
    if count == 60:
        with pytest.raises(ValueError, match="decision-round budget"):
            runner.command("predict")
        choose.assert_not_called()
    else:
        runner.command("predict")
        assert len(runner.state["decisions"]) == 60
        choose.assert_called_once()


def test_events_log_execution_before_observation(runner):
    runner.state["decision"] = decision("e3")
    events = []
    runner.on_event = lambda event, agent, payload: events.append(event)
    runner.state["browser"].observe.side_effect = RuntimeError("observation failed")
    with pytest.raises(RuntimeError):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert events == ["action_started", "action_executed"]
    assert runner.state["history"][0]["node"] == 20


def test_browser_initialization_failure_releases_created_tab(monkeypatch):
    import jev_ultrafast.browser as browser

    monkeypatch.setattr(browser, "ensure_daemon", Mock())
    cdp = Mock(side_effect=[{"targetId": "owned"}, RuntimeError("attach failed"), {}])
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="attach failed"):
        browser.Browser("https://example.test")
    assert cdp.call_args.args == ("Target.closeTarget",)
    assert cdp.call_args.kwargs == {"targetId": "owned"}
