"""openJiuwen harness —— Easel 的**默认**后端（2026-09-19 起）。

与 OpenClaw 完全不同的拓扑：openJiuwen 是**进程内 SDK**，没有常驻 gateway，也
没有"共享 jsonl + tail"这一层::

    Easel ──Runner.run_agent(agent, inputs)──▶ ReActAgent ──HTTP──▶ LLM(OpenAI 兼容端点)
      ▲                                             │
      └────────── on_event() 事件回流 ◀─────────────┘

因此本模块只做三件事：

1. **建 Agent** —— ``AgentCard`` + ``ReActAgent`` + ``ReActAgentConfig``；
2. **配模型** —— 解析环境变量并 ``configure_model_client(provider, api_key, api_base,
   model_name, verify_ssl)``；
3. **跑一轮** —— ``await Runner.run_agent(agent=..., inputs={"query": ..., "conversation_id": ...})``，
   结果归一化成 :class:`~easel.harness.base.RunResult`，事件经 ``on_event`` 流出。

模型配置解析顺序（先命中先用）
------------------------------
1. ``spec.env``（调用点传来的进程环境，Easel 侧统一由 ``_proxy_env()`` 构造）
2. ``os.environ``
3. ``~/.jiuwenmemory/.env``（阶段零统一维护的那一份，见下）

键名沿用 openJiuwen 官方示例（``examples/skill_use/main.py``）：
``MODEL_PROVIDER`` / ``MODEL_NAME`` / ``API_KEY`` / ``API_BASE`` / ``LLM_SSL_VERIFY``。
想只改 Easel 侧、不动全局配置时，用 ``EASEL_OJ_*`` 前缀覆盖（优先级最高）。

环境变量
--------
===============================  ======================================================
``EASEL_HARNESS=openjiuwen``     选中本后端（默认值）
``EASEL_OJ_PROVIDER``            覆盖 ``MODEL_PROVIDER``（默认 ``OpenAI``）
``EASEL_OJ_MODEL``               覆盖 ``MODEL_NAME``
``EASEL_OJ_API_KEY``             覆盖 ``API_KEY``
``EASEL_OJ_API_BASE``            覆盖 ``API_BASE``
``EASEL_OJ_MAX_ITERATIONS``      ReAct 最大迭代数（默认 10）
``EASEL_OJ_VERIFY_SSL``          ``true``/``false``（默认 false，覆盖 ``LLM_SSL_VERIFY``）
``EASEL_OJ_STATELESS=1``         不传 ``conversation_id``，每轮完全无状态
``EASEL_OJ_ENV_FILE``            指定额外加载的 .env 路径（默认 ``~/.jiuwenmemory/.env``）
===============================  ======================================================

已知边界（最小可跑版本的明确取舍）
----------------------------------
- **不做 token 级流式**：本轮用 ``Runner.run_agent`` 一次性拿结果，再以单个 ``TOKEN``
  事件推出全文。``Runner.run_agent_streaming`` 存在，但 chunk 结构未在本仓验证，
  留作下一步（见模块末尾 TODO）。
- **暂不注册技能/工具**：Easel 的 ``skills/openclaw/**/SKILL.md`` 仍由 Agent 自己读并
  用 ``sys_operation`` 执行——最小版本尚未接 ``SysOperationCard``；技能注册改造属于
  方案里的「阶段二」，不在本轮范围。
- **无授权闸门**：openJiuwen 侧没有可中断的 ``request_permission``，``on_permission``
  不会被调用（与 OpenClaw 后端同样受限）。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping

from easel.harness.base import (
    AgentHarness,
    Event,
    EventKind,
    HarnessError,
    HarnessUnavailable,
    RunResult,
    RunSpec,
    maybe_await,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 环境变量名
# --------------------------------------------------------------------------- #
PROVIDER_ENVS = ("EASEL_OJ_PROVIDER", "MODEL_PROVIDER")
MODEL_ENVS = ("EASEL_OJ_MODEL", "MODEL_NAME")
API_KEY_ENVS = ("EASEL_OJ_API_KEY", "API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
API_BASE_ENVS = ("EASEL_OJ_API_BASE", "API_BASE", "OPENAI_BASE_URL")
VERIFY_SSL_ENVS = ("EASEL_OJ_VERIFY_SSL", "LLM_SSL_VERIFY", "MODEL_SSL_VERIFY")
MAX_ITER_ENV = "EASEL_OJ_MAX_ITERATIONS"
STATELESS_ENV = "EASEL_OJ_STATELESS"
STREAM_ENV = "EASEL_OJ_STREAMING"
ENV_FILE_ENV = "EASEL_OJ_ENV_FILE"

DEFAULT_PROVIDER = "OpenAI"
DEFAULT_MAX_ITERATIONS = 10
DEFAULT_ENV_FILE = Path.home() / ".jiuwenmemory" / ".env"
DEFAULT_SYSTEM_PROMPT = (
    "你是 Easel（社媒内容工作流整合层）的 Agent 运行时。\n"
    "按项目根目录下的 AGENTS.md / CLAUDE.md 与 skills/ 里的 SKILL.md 规则工作。\n"
    "所有产物写入 outputs/，不要写到项目目录之外。\n"
)

#: 需要存在的子模块（find_spec 探测用，避免 health() 触发一次完整 import）
_REQUIRED_MODULES = ("openjiuwen", "openjiuwen.core.runner", "openjiuwen.core.single_agent")


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _merged_env(spec_env: Mapping[str, str] | None) -> dict[str, str]:
    """``spec.env`` 覆盖 ``os.environ``（调用点只传增量时也不会丢全局配置）。"""
    merged = {k: v for k, v in os.environ.items()}
    if spec_env:
        merged.update({k: str(v) for k, v in spec_env.items()})
    return merged


def _pick(env: Mapping[str, str], names: tuple[str, ...], default: str = "") -> str:
    """按顺序取第一个非空值。"""
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return default


def _as_bool(raw: str, default: bool = False) -> bool:
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: 一眼可辨的占位符（模板/示例里抄来的值，不是真配置）
_PLACEHOLDER_VALUES = {
    "your-model-name", "your-api-key", "your_api_key", "your-model",
    "sk-xxxxxxxxx", "sk-ant-replace_me", "replace_me", "replace-me", "changeme",
    "<your-api-key>", "<api-key>", "none",
}


def _looks_placeholder(value: str) -> bool:
    """判断一个配置值是不是「示例/占位」而非真配置。

    存在的意义：机器上可能残留上一轮实验导出的 ``API_KEY=sk-xxxxxxxxx`` 之类
    占位环境变量——它会**遮住**你刚填好的 ``~/.jiuwenmemory/.env``。
    带占位符的进程环境变量不应压过真实的 .env 配置。
    """
    v = (value or "").strip()
    if not v:
        return True
    low = v.lower()
    if low in _PLACEHOLDER_VALUES:
        return True
    if "example.com" in low or "replace" in low:
        return True
    return low.startswith("sk-") and set(low[3:]) <= {"x", "-", "_"}


def load_env_file(path: Path, *, only_fill: bool = True) -> list[str]:
    """把 ``.env`` 灌进 ``os.environ``，并返回被写入的键名。

    规则（顺序即优先级，避免"填了模板反而不生效"这类坑）：

    - **不覆盖**已有且非占位的进程环境变量（显式配置优先）；
    - 未设置 / 空串 / 占位符（如 ``sk-xxxxxxxxx``）：用 ``.env`` 的值补齐；
    - ``.env`` 里本身为空的键：直接跳过——阶段零的模板把键留空，留空不该覆盖任何东西。

    ``only_fill=False`` 时改为无条件覆盖（仅调试用）。
    """
    if not path or not Path(path).exists():
        return []

    values: dict[str, Any]
    try:
        from dotenv import dotenv_values  # type: ignore
    except Exception:  # noqa: BLE001 - 缺 python-dotenv 时走内置极简解析
        values = {}
        for raw_line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    else:
        values = {k: v for k, v in (dotenv_values(str(path)) or {}).items() if v is not None}

    written: list[str] = []
    for key, value in values.items():
        text = str(value).strip()
        if not key or not text:
            continue  # 模板里留空的键：跳过
        current = os.environ.get(key)
        if only_fill and current and not _looks_placeholder(current):
            continue  # 已有真实配置：.env 不覆盖
        if current == text:
            continue
        os.environ[key] = text
        written.append(key)
    return written


def _env_file_values(path: Path) -> dict[str, str]:
    """读 ``.env``：只收"键名非空且值非空"的项（模板里留空的键不参与覆盖）。"""
    if not path or not Path(path).exists():
        return {}
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and value:
            values[key] = value
    return values


def _apply_env_file(
    env: Mapping[str, str],
    path: Path,
) -> tuple[dict[str, str], list[str]]:
    """把 ``.env`` 的值回填进一份 env 副本，返回 (新 env, 被回填的键)。

    与 :func:`load_env_file` 的分工：那个函数改的是 ``os.environ``（给 SDK 内部
    ``os.getenv`` 用），本函数只处理**本次解析用**的副本——因为调用点传来的
    ``spec.env`` 会把占位符重新盖回 os.environ 之上，只在 os.environ 里替换是不够的。

    回填规则（与 :func:`load_env_file` 一致）：仅当当前值为空或疑似占位符时才用
    ``.env`` 的值；进程里显式给的真实配置仍然优先。
    """
    values = _env_file_values(path)
    merged = dict(env)
    filled: list[str] = []
    for key, value in values.items():
        current = (merged.get(key) or "").strip()
        if current and not _looks_placeholder(current):
            continue
        if current != value:
            merged[key] = value
            filled.append(key)
    return merged, filled


def _resolve_model_env(env: Mapping[str, str]) -> dict[str, Any]:
    """从环境解析出 ``configure_model_client`` 所需的五个参数。"""
    return {
        "provider": _pick(env, PROVIDER_ENVS, DEFAULT_PROVIDER),
        "model_name": _pick(env, MODEL_ENVS),
        "api_key": _pick(env, API_KEY_ENVS),
        "api_base": _pick(env, API_BASE_ENVS),
        "verify_ssl": _as_bool(_pick(env, VERIFY_SSL_ENVS), default=False),
        "max_iterations": int(_pick(env, (MAX_ITER_ENV,), str(DEFAULT_MAX_ITERATIONS)) or DEFAULT_MAX_ITERATIONS),
        "stateless": _as_bool(env.get(STATELESS_ENV, ""), default=False),
        # token 级流式默认开启（EASEL_OJ_STREAMING=0 可退回"一次跑完再整段推出"）
        "streaming": _as_bool(env.get(STREAM_ENV, ""), default=True),
    }


def _extract_text(result: Any) -> str:
    """把 ``Runner.run_agent`` 的返回值抽成纯文本。

    返回值形状随版本/Agent 类型变化（dict / pydantic 对象 / 纯字符串），
    这里逐层兜底，绝不把"取不到正文"变成异常。
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        for key in ("output", "result", "content", "answer", "text", "response"):
            if key in result:
                text = _extract_text(result[key])
                if text:
                    return text
        return json.dumps(dict(result), ensure_ascii=False, default=str)
    for attr in ("output", "content", "result", "text", "answer"):
        if hasattr(result, attr):
            text = _extract_text(getattr(result, attr))
            if text:
                return text
    return str(result)


# --------------------------------------------------------------------------- #
# 常驻事件循环 + 会话级 Agent 缓存
# --------------------------------------------------------------------------- #
# 为什么需要这两样（2026-09-19 真机验证实测出来的，不是预防性设计）：
#
# 1) **常驻事件循环**：openJiuwen 的 LLM 客户端与会话状态绑在"创建它的那个事件循环"
#    上。基类 ``run_sync`` 每轮 ``asyncio.run`` 都新建循环，第二轮直接
#    ``RuntimeError: Event loop is closed``，多轮对话根本走不到第二步。
# 2) **按会话复用 Agent**：Runner 的 in-memory checkpointer store 是按 **agent 实例**
#    建的（日志里的 ``agent_id``）。每轮 ``ReActAgent(...)`` 都是新实例 → 历史永远为空，
#    表现为"上一轮刚说过的名字，下一轮就忘了"。
#
# 两者一起才是"多轮对话"。只做其一都不行：
#   复用实例 + 换循环 → Event loop is closed；
#   不换循环 + 不复用 → 失忆。
#
# 因此：**同步路径**（CLI 的 chat/skill、web 非流式）统一提交到下面这个常驻循环，
# 并按 ``session_id`` 复用同一个 Agent。异步路径（web 流式）跑在调用方自己的循环上，
# 不碰这个缓存——否则会拿到别的循环上建出来的 Agent，直接崩。
_LOOP_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None

#: session_id → (配置指纹, ReActAgent)。指纹不同就不复用：
#: 同一个 session_key 换了模型/prompt 时，必须重建 Agent，否则新配置不生效。
#: （这条不是预防性设计：单测里同一个 ``session_key="s"`` 会换不同 MODEL_NAME 跑。）
_AGENTS: "OrderedDict[str, tuple[tuple, Any]]" = OrderedDict()
_MAX_CACHED_AGENTS = 32


def _get_persistent_loop() -> asyncio.AbstractEventLoop:
    """取（必要时创建）常驻事件循环。它跑在一个 daemon 线程里，随进程退出。"""
    global _LOOP, _LOOP_THREAD
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="easel-openjiuwen-loop", daemon=True
            )
            thread.start()
            _LOOP, _LOOP_THREAD = loop, thread
            logger.info("[harness.openjiuwen] 常驻事件循环已启动（多轮会话需要它）")
        return _LOOP


def _on_persistent_loop() -> bool:
    """当前协程是不是跑在常驻循环上（是才允许用 agent 缓存）。"""
    if _LOOP is None:
        return False
    try:
        return asyncio.get_running_loop() is _LOOP
    except RuntimeError:
        return False


def _take_cached_agent(session_id: str, fingerprint: tuple) -> Any | None:
    """取本会话的 Agent；配置指纹对得上才复用，否则视为未命中（等重建）。"""
    entry = _AGENTS.get(session_id)
    if entry is None:
        return None
    cached_fp, agent = entry
    if cached_fp != fingerprint:
        _AGENTS.pop(session_id, None)
        return None
    _AGENTS.move_to_end(session_id)
    return agent


def _remember_agent(session_id: str, fingerprint: tuple, agent: Any) -> None:
    _AGENTS[session_id] = (fingerprint, agent)
    _AGENTS.move_to_end(session_id)
    while len(_AGENTS) > _MAX_CACHED_AGENTS:
        _AGENTS.popitem(last=False)


def release_session(session_id: str) -> None:
    """丢掉某个会话的 Agent（会话结束/换后端时用；不丢也不影响正确性，只是占内存）。"""
    _AGENTS.pop(session_id, None)


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
class OpenJiuwenHarness(AgentHarness):
    """openJiuwen（agent-core）进程内 harness。

    ``health()`` 只做**零配额**探测（模块在不在、模型配没配），不建 Agent、不打网络。
    """

    name = "openjiuwen"

    # ---- 必需 ----
    def health(self) -> bool:
        """``openjiuwen`` 及其最小子模块是否可导入（不 import、不联网、不烧配额）。"""
        return all(importlib.util.find_spec(mod) is not None for mod in _REQUIRED_MODULES)

    def describe(self) -> dict[str, Any]:
        raw_env = _merged_env(None)
        env_file = Path(raw_env.get(ENV_FILE_ENV) or DEFAULT_ENV_FILE)
        env, filled_env = _apply_env_file(raw_env, env_file)
        model = _resolve_model_env(env)
        placeholders = sorted(
            name
            for name in (*API_KEY_ENVS, *MODEL_ENVS, *API_BASE_ENVS)
            if (raw_env.get(name) or "").strip() and _looks_placeholder(raw_env[name])
        )
        return {
            "name": self.name,
            "healthy": self.health(),
            "ask_supported": self.ask_supported(),
            "streaming": bool(model["streaming"]),
            "model_configured": bool(
                model["api_key"] and model["model_name"]
                and not _looks_placeholder(model["api_key"])
                and not _looks_placeholder(model["model_name"])
            ),
            "provider": model["provider"],
            "model": model["model_name"] or "(未配置)",
            "api_base": model["api_base"] or "(provider 默认)",
            "max_iterations": model["max_iterations"],
            "env_file": str(env_file),
            "env_file_exists": env_file.exists(),
            "env_file_filled": filled_env,
            # 非空但明显是占位符的进程环境变量：会遮住 .env，值得在 doctor/ping 里露出来
            "placeholder_env": placeholders,
        }

    async def run(
        self,
        spec: RunSpec,
        *,
        on_event=None,
        on_permission=None,
    ) -> RunResult:
        """跑一轮：建 Agent → 跑 → 归一化结果与事件。"""
        if on_permission is not None:
            logger.info(
                "[harness.openjiuwen] 本后端没有可中断的授权闸门，on_permission 不会被调用"
            )
        if not self.health():
            raise HarnessUnavailable(
                "找不到 openjiuwen。请先在当前环境安装：pip install -U openjiuwen"
            )

        env = _merged_env(spec.env)
        # 阶段零统一维护的 ~/.jiuwenmemory/.env：
        #   load_env_file  → 写 os.environ（SDK 内部的 os.getenv 也能读到）
        #   _apply_env_file → 把 .env 的真值回填进本次解析副本（spec.env 里的占位符盖不掉）
        env_file = Path(env.get(ENV_FILE_ENV) or DEFAULT_ENV_FILE)
        loaded = load_env_file(env_file)
        env, filled = _apply_env_file(env, env_file)
        if loaded or filled:
            logger.info(
                "[harness.openjiuwen] 用 %s 补齐/纠正配置：%s",
                env_file,
                ",".join(sorted(set(loaded) | set(filled))),
            )

        model = _resolve_model_env(env)
        if not model["api_key"] or not model["model_name"]:
            raise HarnessUnavailable(
                "openJiuwen 模型未配置：请设置 API_KEY / MODEL_NAME（以及 API_BASE），"
                f"或填好 {DEFAULT_ENV_FILE}"
            )

        session_id = spec.session_id or self.session_id_for(spec.session_key)

        async def emit(kind: EventKind, text: str = "", data: Mapping[str, Any] | None = None,
                       raw: Mapping[str, Any] | None = None) -> None:
            if on_event is None:
                return
            await maybe_await(on_event(Event(kind=kind, text=text, data=data or {}, raw=raw)))

        await emit(
            EventKind.STATUS,
            "running",
            {"harness": self.name, "provider": model["provider"], "model": model["model_name"]},
        )

        # 延迟 import：只有真被选中跑一轮时才付这个 import 代价
        from openjiuwen.core.runner import Runner
        from openjiuwen.core.single_agent import AgentCard, ReActAgent, ReActAgentConfig

        system_prompt = spec.persona or DEFAULT_SYSTEM_PROMPT
        if spec.cwd:
            system_prompt = f"{system_prompt}\n当前工作目录：{spec.cwd}\n"

        # 会话复用（多轮对话的关键，见文件上方说明）：仅常驻循环上复用，
        # 异步路径（web 流式）跑在调用方自己的循环上，不复用、也不入缓存。
        fingerprint = (
            model["provider"], model["model_name"], model["api_key"], model["api_base"],
            model["verify_ssl"], model["max_iterations"], model["stateless"], system_prompt,
            model["streaming"],
        )
        reusable = _on_persistent_loop()
        agent = _take_cached_agent(session_id, fingerprint) if reusable else None
        if agent is not None:
            logger.info("[harness.openjiuwen] 复用会话 %s 的既有 Agent", session_id)
        else:
            agent = ReActAgent(card=AgentCard(name="easel", description="Easel agent"))
            config = (
                ReActAgentConfig()
                .configure_model_client(
                    provider=model["provider"],
                    api_key=model["api_key"],
                    api_base=model["api_base"],
                    model_name=model["model_name"],
                    verify_ssl=model["verify_ssl"],
                )
                .configure_prompt_template([{"role": "system", "content": system_prompt}])
                .configure_max_iterations(model["max_iterations"])
            )
            agent.configure(config)
            if reusable:
                _remember_agent(session_id, fingerprint, agent)

        inputs: dict[str, Any] = {"query": spec.message}
        if not model["stateless"]:
            # Runner._prepare_agent 优先读 inputs 里的 conversation_id（见 runner.py:522）
            inputs["conversation_id"] = session_id

        await emit(EventKind.ACTIVITY, f"openJiuwen ReActAgent 开始执行（{model['model_name']}）")

        text = ""
        usage: dict[str, Any] = {}
        streamed_any = False

        if model["streaming"]:
            text, usage, streamed_any = await self._run_streaming(
                agent=agent, inputs=inputs, spec=spec, emit=emit,
            )

        if not streamed_any:
            # 流式关闭、或流式失败且没拿到任何正文 → 退回一次性调用
            try:
                result = await asyncio.wait_for(
                    Runner.run_agent(agent=agent, inputs=inputs),
                    timeout=float(spec.timeout_s),
                )
            except asyncio.TimeoutError as exc:
                raise HarnessError(f"本轮超时（{spec.timeout_s}s）") from exc
            except HarnessError:
                raise
            except Exception as exc:  # noqa: BLE001 - SDK 异常统一转成 harness 层错误
                await emit(EventKind.ERROR, str(exc))
                raise HarnessError(f"openJiuwen 执行失败：{type(exc).__name__}: {exc}") from exc
            text = _extract_text(result).strip()
            if text:
                await emit(EventKind.TOKEN, text)
            usage = {**usage, "raw_result_type": type(result).__name__}

        await emit(EventKind.USAGE, "", usage)
        await emit(EventKind.DONE, "", {"session_id": session_id})

        return RunResult(
            text=text,
            stop_reason="end_turn",
            returncode=0,
            session_id=session_id,
            events=[],
        )

    async def _run_streaming(self, *, agent, inputs, spec, emit) -> tuple[str, dict[str, Any], bool]:
        """用 ``Runner.run_agent_streaming`` 逐 chunk 推事件。

        返回 ``(全文, usage, 是否产出过正文)``。chunk 形状（0.1.18 实测）::

            OutputSchema(type='llm_output',    payload={'content': '西湖', 'result_type': 'answer'})
            OutputSchema(type='llm_usage',     payload={'usage_metadata': {...}})
            OutputSchema(type='context.usage', payload={...})

        流式中途失败但**已拿到部分正文**时保留半截内容并标记为"已产出"，
        不重跑（重跑会让用户看到重复输出、也重复计费）。
        """
        from openjiuwen.core.runner import Runner

        parts: list[str] = []
        usage: dict[str, Any] = {"streaming": True}
        try:
            # 老版本/精简安装可能没有 base stream 枚举：拿不到就按后端默认模式跑
            try:
                from openjiuwen.core.session.stream import BaseStreamMode

                stream_modes: Any = [BaseStreamMode.OUTPUT]
            except Exception:  # noqa: BLE001
                stream_modes = None
            async with asyncio.timeout(float(spec.timeout_s)):
                async for chunk in Runner.run_agent_streaming(
                    agent=agent,
                    inputs=inputs,
                    stream_modes=stream_modes,
                ):
                    ctype = str(getattr(chunk, "type", "") or "")
                    payload = getattr(chunk, "payload", None)
                    payload = payload if isinstance(payload, Mapping) else {}
                    if ctype == "llm_output":
                        piece = str(payload.get("content") or "")
                        if piece:
                            parts.append(piece)
                            await emit(EventKind.TOKEN, piece)
                    elif ctype == "llm_usage":
                        meta = payload.get("usage_metadata")
                        if isinstance(meta, Mapping):
                            usage.update({
                                key: meta.get(key)
                                for key in ("model_name", "input_tokens", "output_tokens", "total_tokens")
                                if key in meta
                            })
                    elif "thinking" in ctype.lower():
                        piece = str(payload.get("content") or "")
                        if piece:
                            await emit(EventKind.THINKING, piece)
        except asyncio.TimeoutError:
            if parts:
                logger.warning("[harness.openjiuwen] 流式超时，保留已收到的 %d 段增量", len(parts))
                return "".join(parts), usage, True
            raise HarnessError(f"本轮超时（{spec.timeout_s}s）") from None
        except Exception as exc:  # noqa: BLE001 - 流式失败不致命，交给调用方决定是否回退
            if parts:
                logger.warning(
                    "[harness.openjiuwen] 流式中断（%s: %s），保留已收到的 %d 段增量",
                    type(exc).__name__, exc, len(parts),
                )
                return "".join(parts), usage, True
            logger.warning(
                "[harness.openjiuwen] 流式调用失败，回退一次性调用：%s: %s",
                type(exc).__name__, exc,
            )
            return "", usage, False
        return "".join(parts), usage, bool(parts)

    def run_sync(
        self,
        spec: RunSpec,
        *,
        on_event=None,
        on_permission=None,
    ) -> RunResult:
        """同步跑一轮——**提交到常驻事件循环**，而不是 ``asyncio.run``。

        基类默认实现每轮新建事件循环，会让同一会话的第二轮报
        ``RuntimeError: Event loop is closed``（多轮对话直接断）。这里统一
        复用常驻循环，配合会话级 Agent 缓存，多轮上下文才能续上。
        """
        loop = _get_persistent_loop()
        future = asyncio.run_coroutine_threadsafe(
            self.run(spec, on_event=on_event, on_permission=on_permission),
            loop,
        )
        try:
            return future.result(timeout=float(spec.timeout_s) + 60.0)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise HarnessError(f"本轮超时（{spec.timeout_s}s）") from exc

    def ask_supported(self) -> bool:
        """openJiuwen 最小版本还没接结构化问答卡片。"""
        return False


# --------------------------------------------------------------------------- #
# TODO（下一步，不在最小可跑范围内）
# --------------------------------------------------------------------------- #
# 1) token 级流式：改用 ``Runner.run_agent_streaming(agent=..., inputs=..., stream_modes=[...])``，
#    把 chunk 映射成 EventKind.TOKEN / THINKING / ACTIVITY；需先在 0.1.x 上确认 chunk 结构。
# 2) 技能与工具：给 Agent 配 ``SysOperationCard``（OperationMode.LOCAL + LocalWorkConfig），
#    并把 ``skills/openclaw/**`` 注册成 openJiuwen skill/tool，替代"Agent 自己读 SKILL.md"。
# 3) 会话持久化：确认 AgentSession 的 checkpoint 存储位置，让 ``session_id_for`` 推导出的
#    uuid5 能跨进程续上历史（对齐 OpenClaw 侧的 transcript 行为）。
