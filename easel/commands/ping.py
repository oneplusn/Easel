"""easel ping — 连通性测试（走当前 harness；默认 openjiuwen，`EASEL_HARNESS=openclaw` 可回退）。

改造点（2026-09-18）：不再硬编码 OpenClaw 的 gateway `127.0.0.1:18789/healthz`，
改为问 harness 自己「可用吗」再跑一轮真实往返。这样默认后端换成 openjiuwen 后，
ping 给出的是**当前后端**的结论，而不是在提示你去装 openclaw。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from easel.harness import HARNESS_ENV, HarnessError, RunSpec, get_harness

PROJECT_ROOT = Path(__file__).resolve().parents[2]

GREEN = "\033[0;32m"
RED = "\033[0;31m"
DIM = "\033[0;90m"
NC = "\033[0m"


def _proxy_env() -> dict[str, str]:
    """返回带外网代理的环境变量（保护内网直连）。"""
    env = os.environ.copy()
    env.setdefault("EASEL_ROOT", str(PROJECT_ROOT))
    env.setdefault("http_proxy", os.environ.get("EASEL_PROXY", ""))
    env.setdefault("https_proxy", os.environ.get("EASEL_PROXY", ""))
    env.setdefault("no_proxy", "localhost,127.0.0.1,*.xiaohongshu.com,*.devops.xiaohongshu.com,10.*")
    return env


def _line(label: str, ok: bool) -> bool:
    status = f"{GREEN}OK{NC}" if ok else f"{RED}FAIL{NC}"
    print(f"  {label:<50s} {status}")
    return ok


def cmd_ping(_args) -> int:
    print("[easel] 连通性测试\n")

    harness = get_harness()
    print(f"  harness: {harness.name}  {DIM}(用 {HARNESS_ENV} 切换；openclaw 为显式回退后端){NC}")

    # Step 1: harness 自检——只查「命令是否可定位」，不启进程、不连网关、不烧配额
    all_ok = _line(f"Step 1: harness '{harness.name}' 可用", harness.health())
    if not all_ok:
        print(f"    └─ {harness.describe()}")

    # Step 2: 一轮真实往返（端到端可达性；这一步才会真正用模型）
    detail = ""
    ok = False
    try:
        result = harness.run_sync(RunSpec(
            message="say PONG",
            session_key=f"ping-{int(time.time() * 1000)}",
            cwd=PROJECT_ROOT,
            timeout_s=120.0,
            env=_proxy_env(),
        ))
        text = (result.text or "").strip()
        ok = bool(text)
        detail = text or "（无输出）"
    except HarnessError as e:
        detail = str(e)
    except Exception as e:  # noqa: BLE001 — 兜底，别把 ping 变成裸崩堆栈
        detail = str(e)

    all_ok &= _line("Step 2: 一轮真实往返（say PONG）", ok)
    for line in detail.splitlines()[:5]:
        print(f"    └─ {line}")

    print()
    if all_ok:
        print(f"{GREEN}✓ 全部通过{NC}")
    else:
        print(f"{RED}✗ 有步骤失败{NC} — 运行 python -m easel doctor 检查环境；"
              f"也可用 {HARNESS_ENV}=openclaw 切到回退后端做对比")

    return 0 if all_ok else 1
