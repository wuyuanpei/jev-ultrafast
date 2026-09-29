"""Cloud System1 keeps finite choices and never invokes a real provider in tests."""

import json
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent, demo, model
from jev_ultrafast.questions import NEXT_ACTION, TARGET


@pytest.fixture
def cloud(monkeypatch, context_profile):
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test-key")
    for name in ("SYSTEM1_PROVIDER", "SYSTEM1_DEEPSEEK_API_KEY", "SYSTEM1_DEEPSEEK_MODEL",
                 "SYSTEM1_DEEPSEEK_BASE_URL", "SYSTEM1_DEEPSEEK_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    return {"url": "https://example.test", "title": "Search", "text": "Find", "actions": [
        {"id": "e1", "node": 10, "kind": "fill", "label": "Search", "role": "textbox", "value": ""},
        {"id": "e2", "node": 10, "kind": "click", "label": "Open Search", "role": "textbox", "value": ""},
        {"id": "e3", "node": 20, "kind": "click", "label": "Submit", "role": "button"},
        {"id": "wait", "kind": "wait", "label": "Wait"},
    ]}


def response(choices, finish="stop"):
    return {"model": "deepseek-flash", "choices": [{"finish_reason": finish,
            "message": {"content": json.dumps(choices)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


def test_cloud_same_questions_state_choices_only_and_raw_trace(cloud, monkeypatch):
    trace = {}
    post = Mock(side_effect=[response({"operation": "CLICK"}), response({"click_target": "2"})])
    monkeypatch.setattr(model, "post_json", post)
    decision = model.choose(cloud, "Find a book", [], provider="deepseek", trace=trace)
    url, key, body = post.call_args.args
    assert url == "https://api.deepseek.com/v1/chat/completions"
    assert key == "test-key"
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    content = trace["stages"][1]["decision_input"]
    assert body["messages"][1]["content"] == trace["stages"][1]["laya_context"]["context"]
    assert body["messages"][1]["content"].startswith("<bos>choice question:")
    assert '"elements"' not in body["messages"][1]["content"]
    assert "untrusted" not in body["messages"][0]["content"]
    first = trace["stages"][0]["decision_input"]
    assert first["questions"]["operation"]["instructions"]["rules"] == NEXT_ACTION
    assert list(first["questions"]) == ["operation"]
    assert list(content["questions"]) == ["click_target"]
    assert first["state"] == content["state"]
    assert content["questions"]["click_target"]["instructions"]["rules"] == [NEXT_ACTION, TARGET]
    assert decision["choice"] == "e3"
    assert decision["probabilities"] == decision["operation_probabilities"] == {}
    assert decision["confidence"] is None
    assert trace["stages"][1]["request"] == body and "choices" in trace["stages"][1]["response"]
    assert "test-key" not in json.dumps(trace)
    assert post.call_count == 2
    # Local and cloud receive exactly the same task instructions, state and offered keys.
    monkeypatch.setattr(model, "validate_choice", lambda answer, _: answer)
    post.side_effect = None
    post.return_value = {"model": "local", "answers": {"operation": {"choice": "DONE"}}}
    local = {}
    model.choose(cloud, "Find a book", [], trace=local)
    assert content["state"]["page"] == local["request"]["state"]["page"]
    assert "elements" not in content["state"]
    assert (content["questions"]["click_target"]["instructions"]
            == local["request"]["questions"]["click_target"]["instructions"])


@pytest.mark.parametrize("answer", [
    {"operation": "RUN_CODE"}, {"operation": "CLICK", "click_target": "999"},
    {"operation": "TYPE_TEXT", "type_text_target": "2"}, {"operation": "CLICK"},
    {"operation": "CLICK", "click_target": 2}, {"operation": "DONE", "code": "alert(1)"}, [], None,
])
def test_cloud_rejects_invalid_output(cloud, monkeypatch, answer):
    monkeypatch.setattr(model, "post_json", Mock(return_value=response(answer)))
    with pytest.raises(ValueError, match="Invalid DeepSeek"):
        model.choose(cloud, "Find", [], provider="deepseek")


@pytest.mark.parametrize("operation,target,expected", [
    ("CLICK", "2", "e3"), ("TYPE_TEXT", "1", "e1"), ("WAIT", None, "wait"),
    ("DONE", None, "DONE"), ("BLOCKED", None, "BLOCKED"),
])
def test_cloud_consumes_only_selected_head(cloud, monkeypatch, operation, target, expected):
    answers = [response({"operation": operation})]
    if target:
        answers.append(response({operation.lower() + "_target": target}))
    post = Mock(side_effect=answers)
    monkeypatch.setattr(model, "post_json", post)
    assert model.choose(cloud, "Find", [], provider="deepseek")["choice"] == expected
    assert post.call_count == len(answers)


def test_cloud_select_maps_only_observed_option(cloud, monkeypatch):
    cloud["actions"] = [{"id": "e1", "node": 30, "kind": "select", "label": "Category -> Design",
                         "value": "Design", "current_value": "All"}]
    post = Mock(side_effect=[response({"operation": "SELECT"}), response({"select_target": "1:1"})])
    monkeypatch.setattr(model, "post_json", post)
    assert model.choose(cloud, "Design", [], provider="deepseek")["choice"] == "e1"


def test_cloud_truncation_is_rejected(cloud, monkeypatch):
    monkeypatch.setattr(model, "post_json", Mock(return_value=response({"operation": "DONE"}, finish="length")))
    with pytest.raises(ValueError, match="Invalid DeepSeek"):
        model.choose(cloud, "Find", [], provider="deepseek")


@pytest.mark.parametrize("failure", ["cancelled", "timeout", "stale", "invalid", "network"])
def test_target_boundary_never_executes_on_failure(cloud, monkeypatch, failure):
    cloud["fingerprint"] = "observed"
    browser = Mock(observe=Mock(return_value=cloud), fresh=Mock(return_value=True))
    monkeypatch.setattr(agent, "Browser", Mock(return_value=browser))
    events = []
    def event(name, runner, payload):
        events.append((name, payload))
        if name == "model_end" and payload["call"].get("stage") == "operation":
            if failure in {"cancelled", "timeout"}:
                runner.should_cancel = lambda: failure
            if failure == "stale":
                browser.fresh.return_value = False
    post = Mock(side_effect=[response({"operation": "CLICK"}),
                            RuntimeError("Model unavailable") if failure == "network"
                            else response({"click_target": "999"})])
    monkeypatch.setattr(model, "post_json", post)
    runner = agent.Agent(cloud["url"], "Find", system1_provider="deepseek", on_event=event)
    if failure == "stale":
        runner.command("tick")
    else:
        with pytest.raises((agent.RunCancelled, ValueError, RuntimeError)):
            runner.command("tick")
    browser.act.assert_not_called()
    assert runner.state["decision"] is None
    assert runner.state["decision_rounds"] == 1
    assert post.call_count == (2 if failure in {"invalid", "network"} else 1)
    if post.call_count == 2:
        assert runner.state["model_calls"][1]["status"] == "error"
    assert any(name == "model_end" for name, _ in events)


def test_failed_rounds_consume_budget(cloud, monkeypatch):
    cloud["fingerprint"] = "observed"
    monkeypatch.setattr(agent, "Browser", Mock(return_value=Mock(
        observe=Mock(return_value=cloud), fresh=Mock(return_value=True))))
    post = Mock(return_value=response({"operation": "INVALID"}))
    monkeypatch.setattr(model, "post_json", post)
    runner = agent.Agent(cloud["url"], "Find", system1_provider="deepseek")
    runner.state["decision_rounds"] = 59
    with pytest.raises(ValueError, match="Invalid DeepSeek"):
        runner.command("predict")
    with pytest.raises(ValueError, match="decision-round budget"):
        runner.command("predict")
    assert runner.state["decision_rounds"] == 60
    post.assert_called_once()


def test_no_key_or_unknown_provider_preserves_existing_task(cloud, monkeypatch):
    previous, browser = Mock(), Mock()
    monkeypatch.setattr(demo, "AGENT", previous)
    monkeypatch.setattr(demo, "BASELINE", None)
    monkeypatch.setattr(agent, "Browser", browser)
    monkeypatch.delenv("TEXT_MODEL_API_KEY")
    for provider in ("deepseek", "untrusted"):
        with pytest.raises(ValueError):
            demo.command("reset", {"scenario": "custom", "url": "https://example.test", "goal": "Find",
                                   "system1_provider": provider})
    browser.assert_not_called()
    previous.close.assert_not_called()


def test_do_not_send_text_provider_key_to_other_host(cloud, monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    assert model.deepseek_key() == ""
    monkeypatch.setenv("SYSTEM1_DEEPSEEK_API_KEY", "dedicated")
    assert model.deepseek_key() == "dedicated"


def test_agent_cloud_fill_still_uses_unchanged_helper(cloud, monkeypatch):
    cloud["fingerprint"] = "observed"
    browser = Mock(observe=Mock(return_value=cloud), fresh=Mock(return_value=True))
    monkeypatch.setattr(agent, "Browser", Mock(return_value=browser))
    original_text_model = model.os.getenv("TEXT_MODEL")
    post = Mock(side_effect=[response({"operation": "TYPE_TEXT"}), response({"type_text_target": "1"}),
                            {"model": "helper", "choices": [{"message": {"content": '{"text":"book"}'}}]}])
    monkeypatch.setattr(model, "post_json", post)
    runner = agent.Agent(cloud["url"], "Find a book", system1_provider="deepseek")
    runner.command("tick")
    assert [c["kind"] for c in runner.state["model_calls"]] == ["deepseek", "deepseek", "text"]
    assert [c.get("stage") for c in runner.state["model_calls"]] == ["operation", "type_text_target", None]
    assert runner.state["decision_rounds"] == 1
    assert all(c.args[0].endswith("/chat/completions") for c in post.call_args_list)
    assert post.call_args_list[2].args[2]["messages"][0]["content"] == model.TEXT_VALUE
    assert model.os.getenv("TEXT_MODEL") == original_text_model
    browser.act.assert_called_once_with(cloud["actions"][0], cloud, text="book")
    assert runner.state["history"][0]["probability"] is None
