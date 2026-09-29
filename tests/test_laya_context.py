"""Offline serialization and parity against the installed Laya source (when available)."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_ultrafast.laya_context import choice_context, compact, load_profile


def body(count=2):
    return {"state": {"page": {"url": "https://example.test", "title": "中文", "text": "x" * 1400},
                      "elements": [{"label": "MUST_NOT_LEAK"}], "recent_actions": []},
            "questions": {"click_target": {"type": "choice", "instructions": {"goal": "打开文章 <mask>"},
                "criteria": {str(i): {"element": "[1] " + "a" * 90, "role": "button",
                                      "current_value": "中文" * 30, "checked": False} for i in range(count)}}}}


def test_preprocessing_and_format(context_profile):
    result = choice_context(body())
    assert set(result["state"]) == {"page", "recent_actions"}
    assert len(result["state"]["page"]["text"]) == 1200
    assert "MUST_NOT_LEAK" not in result["context"]
    assert result["context"].startswith('<bos>choice question: {"goal":')
    assert result["context"].endswith("<eos>")
    assert result["input_tokens"] <= 1024
    assert result["context"].count("<mask>") == 2
    value = result["questions"]["click_target"]["criteria"]["0"]
    assert value == compact(body()["questions"]["click_target"]["criteria"]["0"])
    assert value.startswith("[1] " + "a" * 46 + " (button)")
    assert value.endswith("checked=False")


def test_fail_closed_for_missing_profile_and_chunking(context_profile, monkeypatch):
    monkeypatch.setenv("LAYA_CONTEXT_MAX_OPTIONS", "1")
    with pytest.raises(ValueError, match="chunks"):
        choice_context(body())
    monkeypatch.setenv("LAYA_CONTEXT_CHECKPOINT", str(context_profile / "missing"))
    with pytest.raises(ValueError, match="LAYA_CONTEXT_CHECKPOINT"):
        choice_context(body())


@pytest.mark.parametrize("count", [2, 20, 100, 200])
def test_exact_reference_sequence(context_profile, monkeypatch, count):
    root = Path(__file__).resolve().parents[2] / "laya-browser-git" / "code"
    fast = root / "apps" / "fast_batch.py"
    common = root / ".venv" / "Lib" / "site-packages" / "laya" / "common.py"
    checkpoint = root.parent / "v10s"
    if not fast.exists() or not common.exists() or not checkpoint.exists():
        pytest.skip("Optional installed Laya reference not available")
    monkeypatch.setenv("LAYA_CONTEXT_CHECKPOINT", str(checkpoint))
    tok, cfg, special, _ = load_profile(str(checkpoint.resolve()))

    # Execute the reference's pure sequence functions, never load torch or weights.
    namespace = {"json": json, "Dict": dict, "List": list, "Union": __import__("typing").Union,
                 "QTYPES": {"choice": 0}}
    for path, names in ((common, {"render_options", "render_criterion", "serialize_state"}),
                        (fast, {"build_items"}),
                        (root / "apps" / "systemone_server.py", {"compact", "predict"})):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        exec(compile(tree, str(path), "exec"), namespace)

    class Adapter:
        mask_token = special["mask_token"]
        mask_token_id = tok.token_to_id(mask_token)
        cls_token_id = tok.token_to_id(special["cls_token"])
        sep_token_id = tok.token_to_id(special["sep_token"])

        def __call__(self, text, **kwargs):
            return {"input_ids": [tok.encode(t, add_special_tokens=False).ids for t in text]
                    if isinstance(text, list) else tok.encode(text, add_special_tokens=False).ids}

    request = body(count)
    result = choice_context(request)
    agent = SimpleNamespace(tok=Adapter(), cfg=cfg, _to_internal=lambda q: {
        "t": q["type"], "ins": json.dumps(q["instructions"]), "crit": q["criteria"]})
    namespace.update(FMT="v3", MAXOPT=999, agent=agent,
                     predict_fast=lambda agent, state, questions, **_: {
                         "items": namespace["build_items"](agent, state, questions)[0]})
    items = namespace["predict"](request["state"], request["questions"])["items"]
    assert result["context"] == tok.decode(items[0]["ids"], skip_special_tokens=False)
    assert result["input_tokens"] == len(items[0]["ids"])
