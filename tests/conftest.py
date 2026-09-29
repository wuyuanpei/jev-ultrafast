import json

import pytest
from tokenizers import Tokenizer, decoders, models, pre_tokenizers


@pytest.fixture
def context_profile(tmp_path, monkeypatch):
    path = tmp_path / "checkpoint"
    (path / "tokenizer").mkdir(parents=True)
    tokens = ["<unk>", "<bos>", "<eos>", "<mask>"] + sorted(pre_tokenizers.ByteLevel.alphabet())
    tok = Tokenizer(models.BPE(vocab={t: i for i, t in enumerate(tokens)}, merges=[], unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    tok.add_special_tokens(tokens[:4])
    tok.save(str(path / "tokenizer" / "tokenizer.json"))
    (path / "tokenizer" / "tokenizer_config.json").write_text(json.dumps({
        "cls_token": "<bos>", "sep_token": "<eos>", "mask_token": "<mask>"}), encoding="utf-8")
    (path / "rl_agent_config.json").write_text(json.dumps({
        "laya_fmt": "v3", "max_len": 1024, "head_max_len_train": 768}), encoding="utf-8")
    monkeypatch.setenv("LAYA_CONTEXT_CHECKPOINT", str(path))
    monkeypatch.setenv("LAYA_CONTEXT_MAX_OPTIONS", "999")
    return path
