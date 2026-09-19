"""Easel harness 抽象层 —— 让后端在 openJiuwen / OpenClaw 等 Agent 运行时之间可切换。

背景
----
Easel 原本把 **OpenClaw** 当作 Agent 运行时（harness）。要把 harness 换成别的实现，
真正的难点不是"改一行命令"，而是三件事散落在调用点里：

1. **怎么起进程 / 怎么建 Agent** —— OpenClaw 走 argv 组装（`openclaw_base_cmd()` 系列
   Windows shim 坑）；openJiuwen 是**进程内 SDK**，用 ``Runner`` + ``ReActAgent`` 建对象；
2. **怎么读事件** —— OpenClaw 靠常驻 gateway 写共享 raw-stream 文件再 tail；
   openJiuwen 在同一个进程里直接返回结果、并用 ``on_event`` 把增量推出来；
3. **怎么处理授权** —— OpenClaw 侧只有 AGENTS.md 里的软约束提示词（无中断点），
   openJiuwen 侧目前同样没有可中断的授权闸门，靠沙箱与策略约束。

本模块把这三件事收敛到一个接口后面，其余代码只认 `Event` / `RunSpec` / `RunResult`。

设计约束
--------
- 开关：环境变量 ``EASEL_HARNESS``，默认 ``openjiuwen``（2026-09-19 起）。
  ``openclaw`` 注册为可显式选用的回退路径，便于迁移期对比验证。
- 事件只描述语义，协议帧/原始载荷留在 :attr:`Event.raw` 里，便于取证与回归对比。
- **授权默认失败安全**：策略 ``ask`` 且没有回调时，结论是"拒绝"而不是"放行"。
  （Easel 现状的病根之一就是"没跟你讨论就把事做了"，抽象层不重复这个错误。）
"""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

__all__ = [
    "AgentHarness",
    "Event",
    "EventKind",
    "HarnessError",
    "HarnessUnavailable",
    "PermissionDecision",
    "PermissionRequest",
    "RunResult",
    "RunSpec",
    "available_harnesses",
    "get_harness",
    "maybe_await",
    "register_harness",
    "stable_session_id",
]


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #
class HarnessError(RuntimeError):
    """harness 运行期错误：进程退出、协议错误、超时。"""


class HarnessUnavailable(HarnessError):
    """该 harness 在当前机器上不可用（可执行文件缺失、未装依赖、未初始化）。"""


# --------------------------------------------------------------------------- #
# 事件模型
# --------------------------------------------------------------------------- #
class EventKind(str, Enum):
    """Easel 侧的事件语义。取值对齐 web/app.py 现有 SSE 事件名，前端无需适配。"""

    TOKEN = "token"          # 正文增量
    THINKING = "thinking"    # 思考增量
    ACTIVITY = "activity"    # 工具调用/进度提示（一行文字）
    PLAN = "plan"            # 多步计划
    TODO = "todo"            # 待办更新
    QUESTION = "question"    # 需要用户决策（授权卡片 / ask_user）
    USAGE = "usage"          # token/费用用量
    STATUS = "status"        # 处理中/空闲
    DONE = "done"            # 本轮收尾
    ERROR = "error"          # 错误


@dataclass(frozen=True)
class Event:
    """一条归一化事件。

    :param kind: 语义类型
    :param text: 可直接展示的文本（正文增量、活动提示等）
    :param data: 结构化载荷（工具调用参数、计划、用量…）
    :param raw: 原始协议帧/日志行，原样保留，供取证与回归对比
    """

    kind: EventKind
    text: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] | None = None


# --------------------------------------------------------------------------- #
# 授权模型
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PermissionRequest:
    """一次授权请求（由各 harness 在自己的协议里触发，例如 ACP 的
    ``session/request_permission`` 或 openJiuwen 侧的工具审批钩子）。"""

    tool_name: str = ""
    title: str = ""
    options: Sequence[Mapping[str, Any]] = ()
    tool_call_id: str = ""
    raw: Mapping[str, Any] | None = None

    def option_ids(self) -> list[str]:
        return [str(o.get("optionId") or "") for o in self.options if isinstance(o, Mapping)]


@dataclass(frozen=True)
class PermissionDecision:
    """授权结论。

    ``approved`` 是给用户看的结论；``option_id`` 是回给 agent 的协议取值
    （沿用 ACP 标准取值：``allow-once`` / ``allow-always`` / ``reject-once``，
    各 harness 按自己的协议映射）。
    """

    approved: bool
    option_id: str | None = None
    always: bool = False
    feedback: str = ""

    @classmethod
    def allow_once(cls) -> "PermissionDecision":
        return cls(approved=True, option_id="allow-once")

    @classmethod
    def allow_always(cls) -> "PermissionDecision":
        return cls(approved=True, option_id="allow-always", always=True)

    @classmethod
    def reject_once(cls, feedback: str = "") -> "PermissionDecision":
        return cls(approved=False, option_id="reject-once", feedback=feedback)

    @classmethod
    def cancelled(cls, feedback: str = "") -> "PermissionDecision":
        return cls(approved=False, option_id=None, feedback=feedback)


# --------------------------------------------------------------------------- #
# 运行规格 / 结果
# --------------------------------------------------------------------------- #
@dataclass
class RunSpec:
    """一轮对话的输入规格。"""

    message: str
    session_key: str                       # Easel 侧会话标识（如 web:<id> / skill-<ms>）
    cwd: Path                              # 本轮的工作目录（也是 ACP fs 沙箱根）
    timeout_s: float = 600.0
    persona: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    session_id: str | None = None          # 不传时由 harness.session_id_for() 推导


@dataclass
class RunResult:
    """一轮对话的结果。"""

    text: str = ""
    stop_reason: str = ""
    returncode: int | None = None
    session_id: str | None = None
    events: list[Event] = field(default_factory=list)


EventSink = Callable[[Event], Any]                      # 可同步可异步
PermissionHandler = Callable[[PermissionRequest], Any]  # 可同步可异步


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
#: 与 web/app.py `_openclaw_session_id()` 完全同源的固定命名空间。
#: 复用同一命名空间可保证"同一个 Easel 会话在换 harness 前后拿到同一个 session id"，
#: 迁移期间新旧路径可以读到同一份历史，不会因为换后端就"失忆"。
_EASEL_SESSION_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def stable_session_id(session_key: str) -> str:
    """Easel 会话 key → 稳定 uuid5（同 key 永远同 id，无需落盘映射）。"""
    return str(uuid.uuid5(_EASEL_SESSION_NS, session_key))


async def maybe_await(value: Any) -> Any:
    """同步/异步回调统一入口（Easel 里 CLI 是同步的，web 是异步的）。"""
    if inspect.isawaitable(value):
        return await value
    return value


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #
class AgentHarness(ABC):
    """Agent 运行时接口。

    最小实现只需 :meth:`health` / :meth:`session_id_for` / :meth:`run` / :meth:`aclose`。
    其余钩子（提问、技能同步、会话自愈…）在不同 harness 上能力不对等，
    因此都有默认实现，避免抽象层被迫"按 OpenClaw 的形状"设计。
    """

    #: 注册名，也是 `EASEL_HARNESS` 的取值
    name: ClassVar[str] = ""

    # ---- 必需 ----
    @abstractmethod
    def health(self) -> bool:
        """当前机器上该 harness 是否可用（不启进程、不烧配额）。"""

    def session_id_for(self, session_key: str) -> str:
        """Easel 会话 key → 稳定的后端会话 id。默认与 OpenClaw 路径同算法。"""
        return stable_session_id(session_key)

    @abstractmethod
    async def run(
        self,
        spec: RunSpec,
        *,
        on_event: EventSink | None = None,
        on_permission: PermissionHandler | None = None,
    ) -> RunResult:
        """跑一轮，事件经 ``on_event`` 流出，结束返回 :class:`RunResult`。"""

    async def aclose(self) -> None:
        """释放长连接/常驻资源。默认无操作（每轮起的进程在 run() 内收尾）。"""

    def run_sync(
        self,
        spec: RunSpec,
        *,
        on_event: EventSink | None = None,
        on_permission: PermissionHandler | None = None,
    ) -> RunResult:
        """同步跑一轮（CLI / skill 等同步调用点用；web 路径直接 await :meth:`run`）。"""
        return asyncio.run(self.run(spec, on_event=on_event, on_permission=on_permission))

    # ---- 可选钩子 ----
    def ask_supported(self) -> bool:
        """是否支持"结构化问答题"（选项卡片）。"""
        return False

    def describe(self) -> dict[str, Any]:
        """给 doctor / 诊断页用的自述信息。"""
        return {"name": self.name, "healthy": self.health(), "ask_supported": self.ask_supported()}


# --------------------------------------------------------------------------- #
# 注册表与工厂
# --------------------------------------------------------------------------- #
HARNESS_ENV = "EASEL_HARNESS"
DEFAULT_HARNESS = "openjiuwen"

_REGISTRY: dict[str, Callable[[], AgentHarness]] = {}


def register_harness(name: str, factory: Callable[[], AgentHarness]) -> None:
    """注册一个 harness 工厂（延迟构造：只有真的被选中才 import 对应模块）。"""
    key = (name or "").strip().lower()
    if not key:
        raise HarnessError("harness 注册名不能为空")
    _REGISTRY[key] = factory


def _builtin_factories() -> dict[str, Callable[[], AgentHarness]]:
    """内置 harness 工厂。延迟 import，避免未选中的后端也要装依赖。"""

    def _openclaw() -> AgentHarness:
        from easel.harness.openclaw import OpenClawHarness

        return OpenClawHarness()

    def _openjiuwen() -> AgentHarness:
        from easel.harness.openjiuwen import OpenJiuwenHarness

        return OpenJiuwenHarness()

    return {"openclaw": _openclaw, "openjiuwen": _openjiuwen}


def available_harnesses() -> list[str]:
    """已注册（含内置）的 harness 名。"""
    names = set(_builtin_factories()) | set(_REGISTRY)
    return sorted(names)


def get_harness(name: str | None = None) -> AgentHarness:
    """取 harness 实例。

    优先级：显式 ``name`` > 环境变量 ``EASEL_HARNESS`` > ``openjiuwen``（默认；2026-09-19 起）。
    取不到时报错并列出可用值——不静默回退，避免"以为换成了 openjiuwen，其实还在 OpenClaw"。
    """
    key = (name or os.environ.get(HARNESS_ENV) or DEFAULT_HARNESS).strip().lower()
    factories = dict(_builtin_factories())
    factories.update(_REGISTRY)
    factory = factories.get(key)
    if factory is None:
        raise HarnessError(
            f"未知 harness {key!r}；可用：{', '.join(sorted(factories))}"
            f"（用 {HARNESS_ENV} 指定）"
        )
    return factory()
