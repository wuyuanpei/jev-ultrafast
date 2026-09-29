"""Local Laya makes choices; an OpenAI-compatible model writes field values."""

import json
import math
import os
import time
from copy import deepcopy
from functools import partial
from urllib.parse import urlsplit

import httpx

from .laya_context import choice_context
from .questions import NEXT_ACTION, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)


def system1_config(provider=None, *, require_key=False):
    provider = os.getenv("SYSTEM1_PROVIDER", "laya") if provider is None else provider
    if provider not in ("laya", "deepseek", "jev"):
        raise ValueError("System1 must be laya, deepseek or jev")
    if provider == "jev":
        if require_key and not os.getenv("TYPESAFE_API_KEY", "").strip():
            raise ValueError("System1 Jev needs TYPESAFE_API_KEY in .env")
        return {"provider": provider, "model": os.getenv("TYPESAFE_MODEL") or "jev-latest",
                "base_url": (os.getenv("TYPESAFE_BASE_URL") or "https://api.typesafe.ai").rstrip("/"),
                "timeout": float(os.getenv("TYPESAFE_TIMEOUT_SECONDS") or "60")}
    if provider == "laya":
        return {"provider": provider, "model": os.getenv("LAYA_MODEL", "laya-v10s"),
                "base_url": os.getenv("LAYA_BASE_URL", "http://127.0.0.1:8791").rstrip("/"),
                "timeout": float(os.getenv("LAYA_TIMEOUT_SECONDS", "120"))}
    if require_key and not deepseek_key():
        raise ValueError("System1 DeepSeek needs SYSTEM1_DEEPSEEK_API_KEY or a DeepSeek TEXT_MODEL_API_KEY")
    return {"provider": provider, "model": os.getenv("SYSTEM1_DEEPSEEK_MODEL") or "deepseek-flash",
            "base_url": (os.getenv("SYSTEM1_DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/"),
            "timeout": float(os.getenv("SYSTEM1_DEEPSEEK_TIMEOUT_SECONDS") or "60")}


def deepseek_key():
    key = os.getenv("SYSTEM1_DEEPSEEK_API_KEY")
    if key:
        return key
    text_host = urlsplit(os.getenv("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1")).hostname
    decision_host = urlsplit(os.getenv("SYSTEM1_DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1").hostname
    return os.getenv("TEXT_MODEL_API_KEY", "") if text_host == decision_host == "api.deepseek.com" else ""


def deepseek_choices(body, trace):
    config = system1_config("deepseek", require_key=True)
    context = choice_context(body)
    question = context["question"]
    if trace is not None:
        trace["decision_input"] = deepcopy({key: context[key] for key in ("state", "questions")})
        trace["indexed_elements"] = deepcopy(body["state"].get("elements", []))
        trace["laya_context"] = {key: value for key, value in context.items() if key not in {"state", "questions"}}
    result = model_request(config["base_url"] + "/chat/completions", deepseek_key(), {
        "model": config["model"], "thinking": {"type": "disabled"}, "temperature": 0,
        "max_tokens": 1024, "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                f'Return only a JSON object with exactly one key "{question}" '
                'and a string value containing the chosen option key. No additional fields or text.'
            )},
            {"role": "user", "content": context["context"]},
        ],
    }, trace=trace, timeout=config["timeout"])
    try:
        completion = result["choices"][0]
        if completion.get("finish_reason") not in (None, "stop"):
            raise ValueError()
        choices = json.loads(completion["message"]["content"])
        if not isinstance(choices, dict) or not set(choices) <= set(body["questions"]):
            raise ValueError()
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError("Invalid DeepSeek choice JSON; no action executed.") from None
    return {"model": result.get("model", config["model"]),
            "answers": {key: {"choice": value} for key, value in choices.items()},
            "usage": result.get("usage", {})}


def validate_selection(answer, ids):
    if not isinstance(answer, dict) or not isinstance(answer.get("choice"), str) or answer["choice"] not in ids:
        raise ValueError("Invalid DeepSeek choice; no action executed.")
    return answer


def post_json(url, key, body, *, timeout=25):
    for attempt in range(3):
        try:
            response = CLIENT.post(
                url, json=body,
                headers={"Authorization": f"Bearer {key}"} if key else {},
                timeout=timeout,
            )
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(
                f"Model provider returned HTTP {response.status_code}; no action executed."
            )
        return response.json()
    raise RuntimeError("Model unavailable")


def model_request(url, key, body, *, trace=None, **kwargs):
    if trace is not None:
        trace.update(request=deepcopy(body), model=body.get("model"))
    result = post_json(url, key, body, **kwargs)
    if trace is not None:
        trace["response"] = deepcopy(result)
    return result


def validate_choice(answer, ids, *, label="Laya"):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(
                type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1
                for n in numbers
            )
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"Invalid {label} response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {
                k: action[k]
                for k in ("role", "value", "checked", "selected", "expanded")
                if k in action
            }
            element.update(
                index=index, label=action["label"].split(" → ")[0], operations=[]
            )
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append(
                {"index": target, "label": action["label"], "value": action["value"]}
            )
        group[target] = action
    return elements, targets, controls


def choose(state, goal, history, *, trace=None, provider="laya", stage_call=None):
    config = system1_config(provider, require_key=provider == "jev")
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(
        DONE="Every requirement is visibly satisfied.",
        BLOCKED="No supported operation can progress.",
    )
    questions = {
        "operation": {
            "type": "choice",
            "criteria": operations,
            "instructions": {"goal": goal, "rules": NEXT_ACTION},
        }
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{
                        k: a[k]
                        for k in ("role", "checked", "selected", "expanded")
                        if k in a
                    },
                }
                for index, a in candidates.items()
            },
            "instructions": {
                "goal": goal,
                "operation": operation,
                "rules": [NEXT_ACTION, TARGET],
            },
        }
    body = {
        "model": config["model"],
        "include_context": True,
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")}
                for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    base_url = config["base_url"]
    if provider == "jev":
        body.pop("include_context", None)

    if provider == "deepseek":
        def request_stage(question, *, trace=None):
            stage_body = {**body, "questions": {question: questions[question]}}
            result = deepseek_choices(stage_body, trace)
            answer = validate_selection(result["answers"].get(question), questions[question]["criteria"])
            op = answer["choice"] if question == "operation" else operation
            target = None if question == "operation" else answer["choice"]
            choice = (targets[op][target]["id"] if target else
                      controls[op]["id"] if op in controls else op)
            return {**result, "operation": op, "target": target, "choice": choice,
                    "provider": "deepseek", "operation_probabilities": {}, "target_probabilities": {}}

        def invoke(question):
            if stage_call:
                return stage_call(request_stage, question)
            record = {}
            if trace is not None:
                trace.setdefault("stages", []).append(record)
            return request_stage(question, trace=record)

        result = invoke("operation")
        operation = result["operation"]
        if operation in targets:
            target_result = invoke(operation.lower() + "_target")
            result["answers"].update(target_result["answers"])
            result["usage"] = {key: result["usage"].get(key, 0) + target_result["usage"].get(key, 0)
                               for key in result["usage"].keys() | target_result["usage"].keys()
                               if isinstance(result["usage"].get(key, 0), (int, float))
                               and isinstance(target_result["usage"].get(key, 0), (int, float))}
    else:
        result = model_request(
            base_url + "/v1/systemone",
            os.environ.get("TYPESAFE_API_KEY" if provider == "jev" else "LAYA_API_KEY", ""), body,
            trace=trace, timeout=config["timeout"],
        )
    validate = validate_choice if provider == "laya" else validate_selection
    if provider == "jev":
        validate = partial(validate_choice, label="Jev")
    operation_answer = validate(
        result["answers"].get("operation", {}), operations
    )
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate(
            result["answers"].get(operation.lower() + "_target", {}), targets[operation]
        )
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {
            a["id"]: target_answer["probabilities"][index]
            for index, a in targets[operation].items()
        } if "probabilities" in target_answer else {}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        if "probabilities" in operation_answer:
            probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "provider": provider,
        "confidence": operation_answer.get("confidence"),
        "probabilities": probabilities,
        "operation_probabilities": operation_answer.get("probabilities", {}),
        "target_probabilities": target_answer.get("probabilities", {}) if target_answer else {},
        "target_confidence": target_answer.get("confidence") if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [
            {k: h.get(k) for k in ("action", "text")} for h in history[-6:]
        ],
    }


def field_text(context, *, trace=None):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError(
            "TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor."
        )
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip(
        "/"
    )
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = (
        {"thinking": {"type": "disabled"}}
        if "api.deepseek.com/" in base
        else {"reasoning": {"effort": "low"}}
    )
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    result = model_request(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUE},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
        trace=trace,
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        value = output["text"]
        if (
            set(output) != {"text"}
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > 2000
        ):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError(
            "Text helper returned no valid field value; nothing typed."
        ) from None
    return value, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
