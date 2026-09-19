"""Easel harness 抽象层。

用法::

    from easel.harness import get_harness, RunSpec

    harness = get_harness()                 # 读 EASEL_HARNESS，默认 openjiuwen
    result = await harness.run(spec, on_event=sink, on_permission=asker)

约定：
- 默认 harness 是 ``openjiuwen``（进程内 openJiuwen SDK）；``openclaw`` 保留为可显式选用的回退；
- 换 harness 只改 ``EASEL_HARNESS``，不改调用点；
- 授权默认**失败安全**（宁可拒绝，不静默放行）。
"""

from __future__ import annotations

from easel.harness.base import (
    HARNESS_ENV,
    AgentHarness,
    Event,
    EventKind,
    HarnessError,
    HarnessUnavailable,
    PermissionDecision,
    PermissionRequest,
    RunResult,
    RunSpec,
    available_harnesses,
    get_harness,
    register_harness,
    stable_session_id,
)

__all__ = [
    "HARNESS_ENV",
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
    "register_harness",
    "stable_session_id",
]
