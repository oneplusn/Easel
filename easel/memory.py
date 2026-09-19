"""JiuwenMemory 记忆服务的按需拉起。

背景（2026-09-19 真机验证暴露）：记忆服务 ``memory-server`` 一直靠人工在后台起，
端口还会被 ``~/.jiuwenmemory/.env`` 里的 ``PORT`` 覆盖（脚本传参不生效），换机器或
重启后很容易忘。这里把「读端口 → 探活 → 没起就拉起 → 等健康」收敛成一个函数，
供 ``easel doctor`` 等调用点复用。

约定：
* 端口优先取 ``~/.jiuwenmemory/.env`` 的 ``PORT``，缺省 8000；
* 可执行文件优先取 PATH 上的 ``memory-server``，其次取当前解释器同目录
  （venv ``Scripts`` / conda ``Scripts``）下的同名文件；
* 拉起时把子进程 stdout/stderr 追加写进 ``<repo>/logs/memory-server.log``
  （``logs/`` 已在 .gitignore 内），detached，不阻塞调用方退出。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MEMORY_ENV_FILE = Path.home() / ".jiuwenmemory" / ".env"
DEFAULT_MEMORY_PORT = 8000
DEFAULT_LOG_PATH = Path(__file__).resolve().parents[1] / "logs" / "memory-server.log"

#: 昨天接 JiuwenSwarm 时留在进程环境里的占位值；它们会遮住 .env 里的真值。
PLACEHOLDER_MARKERS = ("your-model-name", "your-audio-model-name", "sk-xxxxxxxxx", "example.com")


def read_memory_env(path: Path | str | None = None) -> dict[str, str]:
    """读 ``~/.jiuwenmemory/.env``（只做 KEY=VALUE 的朴素解析，够用且无依赖）。"""
    env_file = Path(path) if path is not None else MEMORY_ENV_FILE
    values: dict[str, str] = {}
    if not env_file.is_file():
        return values
    for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def memory_port(env: dict[str, str] | None = None) -> int:
    """端口以 .env 的 ``PORT`` 为准（它确实会覆盖脚本参数），非法或缺省用 8000。"""
    values = read_memory_env() if env is None else env
    raw = (values.get("PORT") or "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_MEMORY_PORT


def memory_service_healthy(port: int, timeout: float = 1.5) -> bool:
    """``GET /health`` 返回 2xx 即视为在监听。"""
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - 固定本机地址
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def find_memory_server() -> str | None:
    """定位 ``memory-server``：先 PATH，再当前解释器同目录（venv/conda Scripts）。"""
    exe = shutil.which("memory-server")
    if exe:
        return exe
    for name in ("memory-server.exe", "memory-server"):
        cand = Path(sys.executable).with_name(name)
        if cand.is_file():
            return str(cand)
    return None


def placeholder_env(names: tuple[str, ...] = ("MODEL_NAME", "API_KEY", "API_BASE")) -> list[str]:
    """返回进程环境里仍是占位值的变量名（doctor 用来提醒清掉）。"""
    hits: list[str] = []
    for name in names:
        raw = os.environ.get(name, "")
        if raw and any(marker in raw for marker in PLACEHOLDER_MARKERS):
            hits.append(name)
    return hits


def ensure_memory_server(
    *,
    port: int | None = None,
    wait_s: float = 20.0,
    log_path: Path | str | None = None,
) -> tuple[bool, str]:
    """确保记忆服务在监听；返回 ``(是否可用, 说明)``，不抛异常。"""
    resolved_port = memory_port() if port is None else int(port)
    if memory_service_healthy(resolved_port):
        return True, f"记忆服务已在 127.0.0.1:{resolved_port} 运行"

    exe = find_memory_server()
    if exe is None:
        return False, (
            "未找到 memory-server 可执行文件；请先 pip install 'JiuwenMemory[server]'，"
            "或用 EASEL_MEMORY_SERVER 指向它的绝对路径"
        )
    exe_env = os.environ.get("EASEL_MEMORY_SERVER")
    if exe_env and Path(exe_env).is_file():
        exe = exe_env

    target_log = Path(log_path) if log_path is not None else DEFAULT_LOG_PATH
    try:
        target_log.parent.mkdir(parents=True, exist_ok=True)
        creationflags = 0
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：与 easel 进程解耦，Ctrl+C 不连带杀死它
            creationflags = 0x00000008 | 0x00000200
        with target_log.open("ab") as log:
            subprocess.Popen(  # noqa: S603 - 可执行文件来自本地 venv，参数固定
                [exe],
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(Path(exe).parent),
                creationflags=creationflags,
            )
    except OSError as exc:
        return False, f"拉起 memory-server 失败：{exc}"

    deadline = time.monotonic() + max(wait_s, 0.0)
    while time.monotonic() < deadline:
        if memory_service_healthy(resolved_port, timeout=1.0):
            return True, f"已按需拉起记忆服务 127.0.0.1:{resolved_port}（日志 {target_log}）"
        time.sleep(0.5)
    return False, f"拉起后 {wait_s:.0f}s 内 /health 仍不可用，见日志 {target_log}"
