"""OpenClaw harness —— Easel 的**回退**后端（默认已改为 openjiuwen）。

与 :mod:`easel.harness.openjiuwen` 的关键区别在"事件从哪来"：

- OpenClaw：``openclaw agent`` 只是瘦客户端，真正跑模型的是常驻 **gateway**，
  它把原始事件（token/thinking/收尾）逐条写进**一个共享 jsonl 文件**，Easel 侧 tail 它；
- openJiuwen：进程内 SDK，``Runner.run_agent`` 直接返回结果，事件在同一个进程里回流。

本模块把 OpenClaw 这套逻辑从 ``web/app.py`` 里抽出来，行为**逐条对齐**现网实现：

=  ==============================================================  ==========================
#  现网位置                                                        说明
=  ==============================================================  ==========================
1  ``web/app.py:1269-1275``                                        ``_build_agent_argv``
2  ``easel/cli.py:51-74``                                          ``build_tui_argv``
3  ``web/app.py:1008``                                             ``session_id_for``（uuid5）
4  ``web/app.py:1104-1124`` + ``1419-1436``                        ``map_raw_line``（事件归一化）
5  ``web/app.py:1455-1485``                                        ``tail_raw_stream``（共享文件 tail）
=  ==============================================================  ==========================

注意：本模块是**新增的并行路径**，没有改动 ``web/app.py`` / ``easel/cli.py``。
默认 harness 已于 2026-09-19 改为 ``openjiuwen``；本模块保留为可显式选用的回退
（``EASEL_HARNESS=openclaw``），但不再随本方案安装 OpenClaw。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

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

#: 与 ``web/app.py`` / ``easel/cli.py`` 同值
OPENCLAW_PROFILE = "easel"
DEFAULT_THINKING_LEVEL = "medium"
#: 与 ``web/app.py`` ``SHARED_RAW_STREAM`` 同价（默认值必须与 scripts/gateway.sh 一致）
RAW_STREAM_ENV = "EASEL_RAW_STREAM_PATH"
DEFAULT_RAW_STREAM = "/tmp/easel-raw-stream.jsonl"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STOP_REASON_RE = re.compile(r"ended with stopReason=(\S+)")

#: agent stdout 里这些前缀是框架日志，不是回答内容（与 easel/commands/skill.py 同源）
_LOG_TAG_PREFIXES = (
    "[provider-", "[agents/", "[agent/", "[plugins]", "[tools]", "[diagnostic]",
    "[fetch-", "[heartbeat]", "[health-", "[gateway]",
)


class RawStreamLatch:
    """共享 raw-stream 的**本轮隔离闩锁**。

    gateway 把每个并发 run 的事件都写进同一个文件，事件带 ``runId`` 而不带 sessionId。
    所以本轮用"第一个新事件的 runId"闩锁自己，之后只放行同 runId 的事件
    （对应 ``web/app.py:1104`` 的 ``_raw_event_for_run``）。
    """

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.ignored_foreign = 0

    def accept(self, event: Mapping[str, Any]) -> bool:
        rid = event.get("runId")
        if self.run_id is None:
            if rid is None:
                return False  # 还没拿到 runId，等带 runId 的事件再闩锁
            self.run_id = rid
            return True
        if rid is not None and rid != self.run_id:
            self.ignored_foreign += 1
            return False
        return True


def map_raw_line(line: str, latch: RawStreamLatch | None = None) -> Event | None:
    """一行 OpenClaw raw-stream JSON → Easel 事件（映射口径见方案文档 §5.3）。

    现网实际形态（``web/app.py:1419-1436``）::

        {"id": 12, "runId": "...", "event": "assistant_text_stream",   "evtType": "text_delta", "delta": "你"}
        {"id": 13, "runId": "...", "event": "assistant_thinking_stream","evtType": "thinking_delta", "delta": "嗯"}
        {"id": 14, "runId": "...", "event": "assistant_message_end"}

    未识别/无 delta 的行返回 ``None``（不把框架噪声混进可见流）。
    """
    line = (line or "").strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(event, Mapping):
        return None
    if latch is not None and not latch.accept(event):
        return None

    kind = str(event.get("event") or "")
    evt_type = str(event.get("evtType") or "")
    delta = str(event.get("delta") or "")

    if kind == "assistant_message_end":
        return Event(EventKind.DONE, data={"message_end": True}, raw=dict(event))
    if not delta:
        return None
    if kind == "assistant_text_stream" and evt_type == "text_delta":
        return Event(EventKind.TOKEN, text=delta, raw=dict(event))
    if kind == "assistant_thinking_stream" and evt_type == "thinking_delta":
        return Event(EventKind.THINKING, text=delta, raw=dict(event))
    return None


def tail_raw_stream(
    path: Path,
    *,
    start_offset: int,
    is_done: Any,
    poll_s: float = 0.04,
    latch: RawStreamLatch | None = None,
) -> Iterator[Event]:
    """阻塞式 tail 共享 raw-stream（放线程里跑），产出 :class:`Event`。

    :param start_offset: 本轮开始时的文件尾偏移（只读之后追加的行）
    :param is_done: 无参回调，返回 True 表示"进程已退出，可以读干收工"
    """
    fh = None
    buf = ""
    try:
        # gateway 刚起/本轮还没有事件时文件可能不存在：轮询等它出现（进程先退出就收工）
        while fh is None:
            try:
                fh = open(path, "r", encoding="utf-8")
            except OSError:
                if is_done():
                    return
                time.sleep(poll_s)

        fh.seek(start_offset)
        while True:
            chunk = fh.readline()
            if chunk == "":
                if is_done():
                    buf += fh.read()
                    for line in buf.split("\n"):
                        event = map_raw_line(line, latch)
                        if event is not None:
                            yield event
                    return
                time.sleep(poll_s)
                continue
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                event = map_raw_line(line, latch)
                if event is not None:
                    yield event
    except Exception as exc:  # noqa: BLE001 — tail 失败不能冒充"模型正常收尾"
        logger.warning("[harness.openclaw] raw-stream tail 异常: %s", exc)
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass


def clean_agent_stdout(raw: str) -> str:
    """清掉 ANSI 与框架日志行，得到可展示的回答文本（口径同 ``easel/commands/skill.py``）。"""
    lines = []
    for line in raw.splitlines():
        clean = _ANSI_RE.sub("", line)
        stripped = clean.lstrip()
        if stripped.startswith("[") and any(tag in stripped[:40] for tag in _LOG_TAG_PREFIXES):
            continue
        if clean.strip():
            lines.append(clean.rstrip())
    return "\n".join(lines).strip()


class OpenClawHarness(AgentHarness):
    """回退 harness：沿用 OpenClaw（profile ``easel``）作为 Agent 运行时。

    默认后端已改为 ``openjiuwen``（2026-09-19）；本后端只在显式
    ``EASEL_HARNESS=openclaw`` 时启用，且不随本方案安装 OpenClaw。
    """

    name = "openclaw"

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        profile: str = OPENCLAW_PROFILE,
        thinking_level: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        self._command = list(command) if command else None
        self._profile = profile
        self._thinking_level = thinking_level or (os.environ.get("EASEL_THINKING_LEVEL", "").strip() or DEFAULT_THINKING_LEVEL)
        self._cwd = Path(cwd) if cwd else Path(__file__).resolve().parents[2]

    # ---- 配置自述 ----
    def base_argv(self) -> list[str]:
        """openclaw 启动前缀。默认走 ``easel.openclaw_cmd.openclaw_base_cmd()``（Windows .cmd shim 坑）。"""
        if self._command:
            return list(self._command)
        from easel.openclaw_cmd import openclaw_base_cmd

        return openclaw_base_cmd()

    def build_agent_argv(
        self,
        message: str,
        session_key: str,
        *,
        session_id: str,
        timeout_s: float,
        thinking_level: str | None = None,
    ) -> list[str]:
        """web/skill 入口的 argv（对齐 ``web/app.py:1269-1275``）。"""
        return self.base_argv() + [
            "--profile", self._profile,
            "agent", "--agent", "main",
            "--session-key", f"agent:main:{session_key}",
            "--session-id", session_id,
            "--thinking", thinking_level or self._thinking_level,
            "--timeout", str(int(timeout_s)),
            "--message", message,
        ]

    def build_tui_argv(self, session_key: str, *, message: str = "", timeout_ms: int | None = None) -> list[str]:
        """``easel chat`` 的 argv（对齐 ``easel/cli.py:61-73``；tui 子命令是唯一能共存的本地模式）。"""
        argv = self.base_argv() + ["--profile", self._profile, "tui", "--session", session_key]
        if timeout_ms is not None:
            argv += ["--timeout-ms", str(int(timeout_ms))]
        if message:
            argv += ["--message", message]
        return argv

    @property
    def raw_stream_path(self) -> Path:
        return Path(os.environ.get(RAW_STREAM_ENV, DEFAULT_RAW_STREAM))

    def describe(self) -> dict[str, Any]:
        info = super().describe()
        info.update({
            "profile": self._profile,
            "thinking_level": self._thinking_level,
            "raw_stream": str(self.raw_stream_path),
        })
        return info

    # ---- 健康检查 ----
    def health(self) -> bool:
        try:
            argv = self.base_argv()
        except FileNotFoundError:
            return False
        except Exception as exc:  # noqa: BLE001
            logger.debug("[harness.openclaw] base_argv 失败: %s", exc)
            return False
        return bool(argv)

    async def run(
        self,
        spec: RunSpec,
        *,
        on_event: Any = None,
        on_permission: Any = None,
    ) -> RunResult:
        """跑一轮：起 ``openclaw agent`` + 并行 tail 共享 raw-stream。

        ``on_permission`` 在本后端**无对应物**：OpenClaw 的"先讨论"只是 AGENTS.md 里的提示词，
        没有可应答的中断点（这正是要换后端的原因之一）。若调用方要求授权闸门，
        会在日志里显式说明，而不是假装支持。
        """
        if on_permission is not None:
            logger.info(
                "[harness.openclaw] 本后端不支持授权闸门（无 request_permission 等价物），"
                "on_permission 不会被调用；要硬闸门请改用带审批钩子的后端"
            )
        if not self.health():
            raise HarnessUnavailable("找不到 openclaw（npm i -g openclaw，或 easel doctor）")

        timeout_s = float(spec.timeout_s)
        session_id = spec.session_id or self.session_id_for(spec.session_key)
        argv = self.build_agent_argv(
            spec.message, spec.session_key, session_id=session_id, timeout_s=timeout_s
        )

        stream = self.raw_stream_path
        try:
            start_offset = stream.stat().st_size
        except OSError:
            start_offset = 0

        env = os.environ.copy()
        env.update({k: str(v) for k, v in (spec.env or {}).items()})

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(spec.cwd or self._cwd),
            env=env,
        )

        latch = RawStreamLatch()
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        result = RunResult(session_id=session_id)
        text_parts: list[str] = []
        stop_reason = ""
        tail_done = False

        def _emit_from_thread(event: Event) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        def _is_done() -> bool:
            return proc.returncode is not None

        def _tail() -> None:
            try:
                for event in tail_raw_stream(stream, start_offset=start_offset, is_done=_is_done, latch=latch):
                    _emit_from_thread(event)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        stdout_lines: list[str] = []

        async def _drain_stdout() -> None:
            nonlocal stop_reason
            if proc.stdout is None:
                return
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                text = _ANSI_RE.sub("", line.decode("utf-8", errors="replace"))
                stdout_lines.append(text)
                match = _STOP_REASON_RE.search(text)
                if match:
                    stop_reason = match.group(1)

        tail_task = loop.run_in_executor(None, _tail)
        stdout_task = asyncio.create_task(_drain_stdout())

        deadline = loop.time() + timeout_s + 30
        try:
            while True:
                if tail_done and proc.returncode is not None:
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    result.stop_reason = "timeout"
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=min(1.0, max(0.05, remaining)))
                except asyncio.TimeoutError:
                    continue
                if event is None:
                    tail_done = True
                    continue
                if event.kind == EventKind.TOKEN:
                    text_parts.append(event.text)
                elif event.kind == EventKind.DONE:
                    continue
                if on_event is not None:
                    await maybe_await(on_event(event))
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            stdout_task.cancel()
            try:
                await stdout_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            await tail_task

        result.text = "".join(text_parts).strip() or clean_agent_stdout("".join(stdout_lines))
        result.stop_reason = stop_reason or result.stop_reason or "end_turn"
        result.returncode = proc.returncode
        done_event = Event(EventKind.DONE, text=result.text, data={
            "stop_reason": result.stop_reason,
            "session_id": session_id,
            "ignored_foreign_events": latch.ignored_foreign,
        })
        result.events.append(done_event)
        if on_event is not None:
            await maybe_await(on_event(done_event))
        return result
