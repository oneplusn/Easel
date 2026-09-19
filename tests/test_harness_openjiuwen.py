"""openjiuwen harness —— 离线验证（不装 openJiuwen、不联网、不烧配额）。

用假 ``openjiuwen`` 模块注入 ``sys.modules`` 顶替真 SDK，覆盖：
- 注册表与默认后端（jiuwenswarm 已下线）
- 模型配置解析 / 占位符识别 / .env 补齐优先级
- run() 的"未配置模型"报错路径
- run() 正常路径的正文抽取与事件回流
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from easel.harness import HARNESS_ENV, HarnessError, HarnessUnavailable, RunSpec, get_harness  # noqa: E402
from easel.harness import base as base_mod  # noqa: E402
from easel.harness import openjiuwen as oj  # noqa: E402


# --------------------------------------------------------------------------- #
# 注册表 / 默认后端
# --------------------------------------------------------------------------- #
def test_default_harness_is_openjiuwen(monkeypatch):
    monkeypatch.delenv(HARNESS_ENV, raising=False)
    assert base_mod.DEFAULT_HARNESS == "openjiuwen"
    assert get_harness().name == "openjiuwen"


def test_jiuwenswarm_is_gone(monkeypatch):
    """2026-09-19 清理：jiuwenswarm 后端已删除，显式选中应当报错并列出可用值。"""
    monkeypatch.setenv(HARNESS_ENV, "jiuwenswarm")
    with pytest.raises(HarnessError) as ei:
        get_harness()
    assert "jiuwenswarm" in str(ei.value)
    assert "openjiuwen" in str(ei.value)


def test_available_harnesses_has_no_jiuwenswarm():
    assert base_mod.available_harnesses() == ["openclaw", "openjiuwen"]


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        ("", True),
        ("your-model-name", True),
        ("sk-xxxxxxxxx", True),
        ("https://example.com/compatible-mode/v1", True),
        ("sk-ant-REPLACE_ME", True),
        ("sk-real1234567890", False),
        ("gpt-4o", False),
    ],
)
def test_looks_placeholder(value, expected):
    assert oj._looks_placeholder(value) is expected


def test_load_env_file_fills_missing_and_placeholder(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MODEL_NAME=real-model\nAPI_KEY=sk-real-key\nAPI_BASE=https://real.example.cn/v1\nEMPTY_KEY=\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MODEL_NAME", "your-model-name")   # 占位符 → 应被 .env 覆盖
    monkeypatch.setenv("API_KEY", "sk-xxxxxxxxx")         # 占位符 → 应被 .env 覆盖
    monkeypatch.setenv("API_BASE", "https://real.example.cn/v1")  # 真配置 → 保留
    monkeypatch.delenv("EMPTY_KEY", raising=False)

    written = oj.load_env_file(env_file)

    import os

    assert os.environ["MODEL_NAME"] == "real-model"
    assert os.environ["API_KEY"] == "sk-real-key"
    assert "EMPTY_KEY" not in os.environ          # 模板里留空的键不该被写入
    assert set(written) == {"MODEL_NAME", "API_KEY"}


def test_load_env_file_skips_missing_file(tmp_path):
    assert oj.load_env_file(tmp_path / "nope.env") == []


def test_resolve_model_env_defaults():
    model = oj._resolve_model_env({})
    assert model["provider"] == "OpenAI"
    assert model["verify_ssl"] is False
    assert model["max_iterations"] == 10
    assert model["stateless"] is False


def test_resolve_model_env_overrides_win():
    model = oj._resolve_model_env(
        {
            "MODEL_PROVIDER": "SiliconFlow",
            "EASEL_OJ_PROVIDER": "DashScope",
            "MODEL_NAME": "global-model",
            "EASEL_OJ_MODEL": "easel-model",
            "API_KEY": "global-key",
            "EASEL_OJ_API_KEY": "easel-key",
            "EASEL_OJ_MAX_ITERATIONS": "3",
            "LLM_SSL_VERIFY": "true",
        }
    )
    assert model["provider"] == "DashScope"
    assert model["model_name"] == "easel-model"
    assert model["api_key"] == "easel-key"
    assert model["max_iterations"] == 3
    assert model["verify_ssl"] is True


def test_describe_flags_placeholder_env(monkeypatch, tmp_path):
    # 隔离机器上真实的 ~/.jiuwenmemory/.env：本测试只关心"进程环境里的占位符"如何被报告
    monkeypatch.setenv("EASEL_OJ_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("API_KEY", "sk-xxxxxxxxx")
    monkeypatch.setenv("MODEL_NAME", "your-model-name")
    info = oj.OpenJiuwenHarness().describe()
    assert info["name"] == "openjiuwen"
    assert info["model_configured"] is False
    assert "API_KEY" in info["placeholder_env"]
    assert "MODEL_NAME" in info["placeholder_env"]


def test_describe_env_file_fixes_placeholder(monkeypatch, tmp_path):
    """进程环境里的占位符会被 .env 里的真值纠正——describe 报告"实际生效"的配置。"""
    env_file = tmp_path / "mem.env"
    env_file.write_text(
        "API_KEY=sk-real-key\nMODEL_NAME=real-model\n", encoding="utf-8",
    )
    monkeypatch.setenv("EASEL_OJ_ENV_FILE", str(env_file))
    monkeypatch.setenv("API_KEY", "sk-xxxxxxxxx")
    monkeypatch.setenv("MODEL_NAME", "your-model-name")

    info = oj.OpenJiuwenHarness().describe()

    assert info["model_configured"] is True          # .env 的真值生效
    assert "API_KEY" in info["placeholder_env"]      # 但进程里的占位符仍被标出来
    assert "API_KEY" in info["env_file_filled"]      # 并说明是 .env 纠正的


# --------------------------------------------------------------------------- #
# run()
# --------------------------------------------------------------------------- #
def _clean_model_env(monkeypatch, tmp_path):
    """隔离真实机器环境：清掉模型变量，并把 .env 指到一个空文件。"""
    for name in ("EASEL_OJ_PROVIDER", "MODEL_PROVIDER", "EASEL_OJ_MODEL", "MODEL_NAME",
                 "EASEL_OJ_API_KEY", "API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                 "EASEL_OJ_API_BASE", "API_BASE", "OPENAI_BASE_URL",
                 "EASEL_OJ_VERIFY_SSL", "LLM_SSL_VERIFY", "MODEL_SSL_VERIFY",
                 "EASEL_OJ_STATELESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EASEL_OJ_ENV_FILE", str(tmp_path / "absent.env"))


def _fake_openjiuwen(monkeypatch, *, output="PONG", raises=None):
    """把假 openjiuwen 注入 sys.modules，并记录 ReActAgent 收到的配置。"""
    seen: dict = {}

    class FakeConfig:
        def __init__(self):
            seen["config"] = self

        def configure_model_client(self, **kwargs):
            seen["model_client"] = kwargs
            return self

        def configure_prompt_template(self, template):
            seen["prompt"] = template
            return self

        def configure_max_iterations(self, n):
            seen["max_iterations"] = n
            return self

    class FakeAgent:
        def __init__(self, card=None):
            seen["card"] = card

        def configure(self, cfg):
            seen["configured"] = True

    class FakeRunner:
        @classmethod
        async def run_agent(cls, agent, inputs):
            seen["inputs"] = inputs
            if raises is not None:
                raise raises
            return {"output": output}

    mod_root = types.ModuleType("openjiuwen")
    mod_core = types.ModuleType("openjiuwen.core")
    mod_runner = types.ModuleType("openjiuwen.core.runner")
    mod_runner.Runner = FakeRunner
    mod_single = types.ModuleType("openjiuwen.core.single_agent")
    mod_single.AgentCard = lambda **kw: {"card": kw}
    mod_single.ReActAgent = FakeAgent
    mod_single.ReActAgentConfig = FakeConfig
    mod_core.runner = mod_runner
    mod_core.single_agent = mod_single
    for name, mod in (
        ("openjiuwen", mod_root),
        ("openjiuwen.core", mod_core),
        ("openjiuwen.core.runner", mod_runner),
        ("openjiuwen.core.single_agent", mod_single),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    monkeypatch.setattr(oj.OpenJiuwenHarness, "health", lambda self: True)
    return seen


def test_run_requires_model_config(monkeypatch, tmp_path):
    _clean_model_env(monkeypatch, tmp_path)
    monkeypatch.setattr(oj.OpenJiuwenHarness, "health", lambda self: True)
    with pytest.raises(HarnessUnavailable) as ei:
        import asyncio

        asyncio.run(oj.OpenJiuwenHarness().run(RunSpec(message="hi", session_key="t", cwd=tmp_path)))
    assert "模型未配置" in str(ei.value)


def test_run_returns_text_and_events(monkeypatch, tmp_path):
    _clean_model_env(monkeypatch, tmp_path)
    monkeypatch.setenv("API_KEY", "sk-real")
    monkeypatch.setenv("MODEL_NAME", "gpt-4o-mini")
    seen = _fake_openjiuwen(monkeypatch, output="PONG")

    events = []
    spec = RunSpec(message="say PONG", session_key="ping-1", cwd=tmp_path, env={"EASEL_ROOT": str(PROJECT_ROOT)})
    result = oj.OpenJiuwenHarness().run_sync(spec, on_event=events.append)

    assert result.text == "PONG"
    assert result.returncode == 0
    assert result.session_id == base_mod.stable_session_id("ping-1")
    assert [e.kind.value for e in events][0] == "status"
    assert [e.kind.value for e in events][-1] == "done"
    assert any(e.kind.value == "token" and e.text == "PONG" for e in events)

    assert seen["inputs"]["query"] == "say PONG"
    assert seen["inputs"]["conversation_id"] == result.session_id
    assert seen["model_client"]["provider"] == "OpenAI"
    assert seen["model_client"]["model_name"] == "gpt-4o-mini"
    assert seen["max_iterations"] == 10


def test_run_stateless_omits_conversation_id(monkeypatch, tmp_path):
    _clean_model_env(monkeypatch, tmp_path)
    monkeypatch.setenv("API_KEY", "sk-real")
    monkeypatch.setenv("MODEL_NAME", "gpt-4o-mini")
    monkeypatch.setenv("EASEL_OJ_STATELESS", "1")
    seen = _fake_openjiuwen(monkeypatch)

    oj.OpenJiuwenHarness().run_sync(RunSpec(message="hi", session_key="s", cwd=tmp_path))

    assert "conversation_id" not in seen["inputs"]


def test_run_wraps_sdk_failure_into_harness_error(monkeypatch, tmp_path):
    _clean_model_env(monkeypatch, tmp_path)
    monkeypatch.setenv("API_KEY", "sk-real")
    monkeypatch.setenv("MODEL_NAME", "gpt-4o-mini")
    _fake_openjiuwen(monkeypatch, raises=RuntimeError("boom"))

    with pytest.raises(HarnessError) as ei:
        oj.OpenJiuwenHarness().run_sync(RunSpec(message="hi", session_key="s", cwd=tmp_path))
    assert "boom" in str(ei.value)


def test_run_uses_env_file_to_fill_config(monkeypatch, tmp_path):
    _clean_model_env(monkeypatch, tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_NAME=from-file\nAPI_KEY=sk-from-file\n", encoding="utf-8")
    monkeypatch.setenv("EASEL_OJ_ENV_FILE", str(env_file))
    seen = _fake_openjiuwen(monkeypatch)

    oj.OpenJiuwenHarness().run_sync(RunSpec(message="hi", session_key="s", cwd=tmp_path))

    assert seen["model_client"]["model_name"] == "from-file"
    assert seen["model_client"]["api_key"] == "sk-from-file"


def test_extract_text_shape_tolerance():
    assert oj._extract_text(None) == ""
    assert oj._extract_text("plain") == "plain"
    assert oj._extract_text({"output": "a"}) == "a"
    assert oj._extract_text({"output": {"content": "b"}}) == "b"
    assert oj._extract_text(types.SimpleNamespace(content="c")) == "c"
    assert oj._extract_text({"unknown": 1}).startswith("{")
