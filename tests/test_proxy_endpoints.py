"""v1.6 中转站/自定义 endpoint：endpoint 解析、PanelEntry 规范化、LiteLLMBackend per-call 透传。

不调网（monkeypatch litellm.acompletion 捕获参数）。覆盖：
- _resolve_endpoints（api_key_env 解析/明文 fallback/缺 key 报错/bad provider/headers/timeout）
- _normalize_panel（str 官方/dict 中转/label 默认/撞名报错/未知 endpoint/PanelEntry 不含 api_key）
- LiteLLMBackend（endpoint_id→provider 前缀/api_base/api_key/extra_headers/timeout；前缀守卫；官方无 endpoint；多 endpoint 不串）
- compute_hash 输入不含 api_key
"""
from __future__ import annotations

import asyncio
import json

import pytest

from design_review import reviews_db
from design_review.providers.litellm import LiteLLMBackend
from design_review.server import _normalize_one, _normalize_panel, _resolve_endpoints


# ===== _resolve_endpoints =====

def test_resolve_endpoints_api_key_env(monkeypatch):
    monkeypatch.setenv("MY_RELAY_KEY", "sk-relay-123")
    cfg = {"relay": {"provider": "openai", "base_url": "https://x/v1", "api_key_env": "MY_RELAY_KEY"}}
    reg = _resolve_endpoints(cfg)
    assert reg["relay"]["api_key"] == "sk-relay-123"
    assert reg["relay"]["provider"] == "openai"
    assert reg["relay"]["base_url"] == "https://x/v1"
    assert reg["relay"]["headers"] == {}
    assert reg["relay"]["timeout"] is None


def test_resolve_endpoints_plaintext_fallback():
    cfg = {"relay": {"provider": "anthropic", "base_url": "https://x", "api_key": "plain"}}
    reg = _resolve_endpoints(cfg)
    assert reg["relay"]["api_key"] == "plain"


def test_resolve_endpoints_missing_key(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    cfg = {"relay": {"provider": "openai", "base_url": "https://x", "api_key_env": "NOPE_KEY"}}
    with pytest.raises(ValueError, match="NOPE_KEY"):
        _resolve_endpoints(cfg)


def test_resolve_endpoints_no_key_at_all():
    cfg = {"relay": {"provider": "openai", "base_url": "https://x"}}
    with pytest.raises(ValueError, match="缺 api_key"):
        _resolve_endpoints(cfg)


def test_resolve_endpoints_bad_provider():
    cfg = {"relay": {"provider": "gemini", "base_url": "https://x", "api_key": "k"}}
    with pytest.raises(ValueError, match="provider"):
        _resolve_endpoints(cfg)


def test_resolve_endpoints_headers_timeout():
    cfg = {"r": {"provider": "openai", "base_url": "https://x", "api_key": "k",
                 "headers": {"Authorization": "Bearer k"}, "timeout": 120}}
    reg = _resolve_endpoints(cfg)
    assert reg["r"]["headers"] == {"Authorization": "Bearer k"}
    assert reg["r"]["timeout"] == 120


# ===== _normalize_panel / _normalize_one =====

def test_normalize_panel_str_official():
    entries = _normalize_panel(["gpt-4o", "zai/glm-5.2"], set())
    assert entries == [
        {"label": "gpt-4o", "model": "gpt-4o", "endpoint_id": None},
        {"label": "zai/glm-5.2", "model": "zai/glm-5.2", "endpoint_id": None},
    ]


def test_normalize_panel_dict_relay_no_credential():
    entries = _normalize_panel(
        [{"endpoint": "zhipu", "model": "glm-5.2", "label": "智谱位"}], {"zhipu"}
    )
    assert entries == [{"label": "智谱位", "model": "glm-5.2", "endpoint_id": "zhipu"}]
    # 关键：PanelEntry 不含 credential（key 只在 registry）
    assert "api_key" not in entries[0]
    assert "api_key_env" not in entries[0]


def test_normalize_panel_label_default():
    entries = _normalize_panel([{"endpoint": "zhipu", "model": "glm-5.2"}], {"zhipu"})
    assert entries[0]["label"] == "zhipu/glm-5.2"


def test_normalize_panel_label_collision():
    with pytest.raises(ValueError, match="撞名"):
        _normalize_panel(
            [{"endpoint": "z", "model": "m", "label": "dup"},
             {"endpoint": "z", "model": "m2", "label": "dup"}],
            {"z"},
        )


def test_normalize_panel_unknown_endpoint():
    with pytest.raises(ValueError, match="未定义"):
        _normalize_panel([{"endpoint": "nope", "model": "m"}], {"zhipu"})


def test_normalize_one_str_and_dict():
    assert _normalize_one("gpt-4o", set()) == {"label": "gpt-4o", "model": "gpt-4o", "endpoint_id": None}
    assert _normalize_one({"endpoint": "z", "model": "m"}, {"z"}) == {
        "label": "z/m", "model": "m", "endpoint_id": "z"
    }


# ===== LiteLLMBackend：endpoint_id → litellm.acompletion 参数 =====

class _Capture:
    """捕获 litellm.acompletion 的 kwargs，返回假响应。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)

        class _Msg:
            content = '{"issues":[]}'

        class _Choice:
            message = _Msg()

        class _Usage:
            def model_dump(self):
                return {"total_tokens": 1}

        class _Resp:
            choices = [_Choice()]
            usage = _Usage()
            _hidden_params = {}

        return _Resp()


def _patch_litellm(monkeypatch):
    cap = _Capture()
    import litellm
    monkeypatch.setattr(litellm, "acompletion", cap)
    return cap


def test_backend_openai_prefix_and_passthrough(monkeypatch):
    cap = _patch_litellm(monkeypatch)
    reg = {"r": {"provider": "openai", "base_url": "https://x/v1", "api_key": "k", "headers": {}, "timeout": None}}
    backend = LiteLLMBackend(endpoint_registry=reg)
    asyncio.run(backend.complete(model="glm-4", system="s", user="u", endpoint_id="r"))
    kw = cap.calls[0]
    assert kw["model"] == "openai/glm-4"  # openai 前缀
    assert kw["api_base"] == "https://x/v1"  # snake_case
    assert kw["api_key"] == "k"


def test_backend_anthropic_prefix(monkeypatch):
    cap = _patch_litellm(monkeypatch)
    reg = {"r": {"provider": "anthropic", "base_url": "https://open.bigmodel.cn/api/anthropic",
                 "api_key": "k", "headers": {}, "timeout": None}}
    backend = LiteLLMBackend(endpoint_registry=reg)
    asyncio.run(backend.complete(model="glm-5.2", system="s", user="u", endpoint_id="r"))
    kw = cap.calls[0]
    assert kw["model"] == "anthropic/glm-5.2"
    assert kw["api_base"] == "https://open.bigmodel.cn/api/anthropic"


def test_backend_prefix_guard(monkeypatch):
    """model 已含 / 则不再拼前缀，防 openai/openai/。"""
    cap = _patch_litellm(monkeypatch)
    reg = {"r": {"provider": "openai", "base_url": "https://x", "api_key": "k", "headers": {}, "timeout": None}}
    backend = LiteLLMBackend(endpoint_registry=reg)
    asyncio.run(backend.complete(model="openai/glm-4", system="s", user="u", endpoint_id="r"))
    assert cap.calls[0]["model"] == "openai/glm-4"


def test_backend_headers_and_timeout(monkeypatch):
    cap = _patch_litellm(monkeypatch)
    reg = {"r": {"provider": "openai", "base_url": "https://x", "api_key": "k",
                 "headers": {"Authorization": "Bearer k"}, "timeout": 120}}
    backend = LiteLLMBackend(endpoint_registry=reg)
    asyncio.run(backend.complete(model="m", system="s", user="u", endpoint_id="r"))
    kw = cap.calls[0]
    assert kw["extra_headers"] == {"Authorization": "Bearer k"}
    assert kw["timeout"] == 120  # endpoint timeout 覆盖全局


def test_backend_no_endpoint_official(monkeypatch):
    """endpoint_id=None 走官方 env，不加前缀、不传 api_base/api_key。"""
    cap = _patch_litellm(monkeypatch)
    backend = LiteLLMBackend(endpoint_registry={})
    asyncio.run(backend.complete(model="gpt-4o", system="s", user="u"))
    kw = cap.calls[0]
    assert kw["model"] == "gpt-4o"
    assert "api_base" not in kw
    assert "api_key" not in kw


def test_backend_multi_endpoint_no_crosstalk(monkeypatch):
    """多 endpoint 并存：各自收到自己的 base/key，不串。"""
    cap = _patch_litellm(monkeypatch)
    reg = {
        "a": {"provider": "openai", "base_url": "https://a/v1", "api_key": "ka", "headers": {}, "timeout": None},
        "b": {"provider": "anthropic", "base_url": "https://b", "api_key": "kb", "headers": {}, "timeout": None},
    }
    backend = LiteLLMBackend(endpoint_registry=reg)
    asyncio.run(backend.complete(model="m1", system="s", user="u", endpoint_id="a"))
    asyncio.run(backend.complete(model="m2", system="s", user="u", endpoint_id="b"))
    assert cap.calls[0]["api_base"] == "https://a/v1" and cap.calls[0]["api_key"] == "ka"
    assert cap.calls[1]["api_base"] == "https://b" and cap.calls[1]["api_key"] == "kb"


# ===== compute_hash 输入不含 api_key =====

def test_hash_panel_has_no_api_key():
    entries = [{"label": "x", "model": "m", "endpoint_id": "r"}]
    serialized = json.dumps(entries, sort_keys=True)
    assert "api_key" not in serialized  # PanelEntry 天然无 key
    h1 = reviews_db.compute_hash(
        document_content="doc", document_files=None, panel=entries,
        dimensions=["d"], adapter="a", project_version={},
        retrieved_cases_ids=[], extra_context="",
    )
    h2 = reviews_db.compute_hash(
        document_content="doc", document_files=None, panel=entries,
        dimensions=["d"], adapter="a", project_version={},
        retrieved_cases_ids=[], extra_context="",
    )
    assert h1 == h2  # 同 entries hash 稳定（dict 可序列化）
