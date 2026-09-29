"""Reproduce systemone_server v3 + fast_batch's decoded input, without model inference."""

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from tokenizers import Tokenizer


@lru_cache(maxsize=4)
def load_profile(checkpoint):
    path = Path(checkpoint)
    try:
        config_bytes = (path / "rl_agent_config.json").read_bytes()
        config = json.loads(config_bytes)
        token_path = path / "tokenizer"
        tokenizer_bytes = (token_path / "tokenizer.json").read_bytes()
        special_bytes = (token_path / "tokenizer_config.json").read_bytes()
        special = json.loads(special_bytes)
        tok = Tokenizer.from_str(tokenizer_bytes.decode("utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Exact Laya context needs LAYA_CONTEXT_CHECKPOINT with tokenizer and config files") from error
    if config.get("laya_fmt") != "v3":
        raise ValueError("Exact context currently supports Laya v3 only; no approximate fallback")
    tok.no_truncation()
    tok.no_padding()
    tokens = {}
    for key in ("cls_token", "sep_token", "mask_token"):
        value = special[key]
        tokens[key] = value["content"] if isinstance(value, dict) else value
        if tok.token_to_id(tokens[key]) is None:
            raise ValueError(f"Missing Laya special token: {key}")
    return tok, config, tokens, hashlib.sha256(config_bytes + tokenizer_bytes + special_bytes).hexdigest()


def compact(value):
    if not isinstance(value, dict) or "element" not in value:
        return value
    result = str(value["element"])[:50]
    if value.get("role"):
        result += f" ({value['role']})"
    if value.get("current_value"):
        result += f" = {str(value['current_value'])[:30]!r}"
    for key in ("checked", "selected", "expanded"):
        if key in value:
            result += f" {key}={value[key]}"
    return result


def choice_context(body):
    default = Path(__file__).resolve().parents[2] / "laya-browser-git" / "v10s"
    checkpoint = os.getenv("LAYA_CONTEXT_CHECKPOINT") or str(default)
    tok, config, special, digest = load_profile(str(Path(checkpoint).resolve()))
    max_options = int(os.getenv("LAYA_CONTEXT_MAX_OPTIONS", "999"))
    if len(body["questions"]) != 1:
        raise ValueError("Exact DeepSeek context requires one question per stage")
    name, question = next(iter(body["questions"].items()))
    criteria = {key: compact(value) for key, value in question["criteria"].items()}
    if not criteria or len(criteria) > max_options:
        raise ValueError("Laya would split this question into chunks; exact two-stage context is unavailable")
    state = body["state"]
    state = {"page": {**state["page"], "text": state["page"]["text"][:1200]},
             "recent_actions": state.get("recent_actions", [])}
    instructions = question["instructions"]
    instructions = instructions if isinstance(instructions, str) else json.dumps(instructions)
    mask = special["mask_token"]

    def encode(text):
        return tok.encode(text.replace(mask, " "), add_special_tokens=False).ids

    head = encode(f"choice question: {instructions}")
    options = []
    for key, value in criteria.items():
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        label = key if value is None or value == "" else f"{key}: {rendered}"
        options.append([tok.token_to_id(mask)] + encode(" " + label)[:48])
    head_limit = config.get("head_max_len_train") or config.get("head_max_len", 192)
    budget = head_limit - sum(map(len, options))
    if budget < 16:
        per = max(4, (head_limit - 16) // len(options))
        options = [option[:per] for option in options]
        budget = head_limit - sum(map(len, options))
    sep = tok.token_to_id(special["sep_token"])
    ids = [tok.token_to_id(special["cls_token"])] + head[:max(8, budget)] + [sep]
    markers = []
    for option in options:
        markers.append(len(ids))
        ids.extend(option)
    ids.append(sep)
    limit = config.get("max_len", 512)
    room = max(0, limit - len(ids) - 1)
    ids = (ids + encode(json.dumps(state, ensure_ascii=False))[:room] + [sep])[:limit]
    if any(marker >= limit for marker in markers):
        raise ValueError("Laya options exceed the sequence budget; no approximate context sent")
    return {"question": name, "context": tok.decode(ids, skip_special_tokens=False),
            "input_tokens": len(ids), "max_tokens": limit, "profile_sha256": digest,
            "format": "v3", "max_options": max_options,
            "state": state, "questions": {name: {**question, "criteria": criteria}}}
