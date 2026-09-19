"""easel doctor — 检查开发环境是否就绪。

默认后端是 **openJiuwen**（进程内 SDK，2026-09-19 起）：检查项围绕
``openjiuwen`` 包、``~/.jiuwenmemory/.env`` 模型配置、JiuwenMemory 记忆服务展开。

只有显式 ``EASEL_HARNESS=openclaw``（迁移期回退路径）时，才追加检查 Node.js /
``openclaw`` 命令 / 18789 gateway —— 这些在新架构下默认不再需要。
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from easel.memory import ensure_memory_server

# 项目根目录（Easel/）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: openJiuwen 侧统一维护的模型/记忆配置（阶段零）
MEMORY_ENV_FILE = Path.home() / ".jiuwenmemory" / ".env"
#: 缺一不可的 6 个键：3 个对话模型 + 3 个向量模型
REQUIRED_ENV_KEYS = (
    "MODEL_NAME", "API_KEY", "API_BASE",
    "EMBED_MODEL_NAME", "EMBED_API_KEY", "EMBED_API_BASE",
)
#: 记忆服务默认端口（.env 的 PORT 优先）
DEFAULT_MEMORY_PORT = 8000

# OpenClaw 回退路径的稳定下限（仅 EASEL_HARNESS=openclaw 时检查）
MIN_OPENCLAW = (2026, 6, 11)

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
NC = "\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = f"{GREEN}OK{NC}" if ok else f"{RED}FAIL{NC}"
    print(f"  {label:<40s} {status}")
    if not ok and detail:
        print(f"    └─ {detail}")
    return ok


def _warn(label: str, detail: str) -> None:
    print(f"  {label:<40s} {YELLOW}WARN{NC}")
    if detail:
        print(f"    └─ {detail}")


# --------------------------------------------------------------------------- #
# 基础探测
# --------------------------------------------------------------------------- #
def _python_version_ok(major: int = 3, minor: int = 11) -> bool:
    return sys.version_info >= (major, minor)


def _module_available(name: str) -> bool:
    """零配额探测：只看模块在不在，不 import（避免拉起重依赖）。"""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _venv_available() -> bool:
    return _module_available("venv")


def _read_env_file() -> dict[str, str]:
    if not MEMORY_ENV_FILE.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in MEMORY_ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _memory_service_healthy(port: int) -> bool:
    try:
        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - 本机探活
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _harness_describe() -> dict:
    """取当前 harness 的自述信息；失败时给出最小结构，doctor 不因此崩掉。"""
    try:
        from easel.harness import get_harness

        return get_harness().describe()
    except Exception as exc:  # noqa: BLE001
        return {"name": os.environ.get("EASEL_HARNESS", "openjiuwen"), "healthy": False, "error": str(exc)}


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    except (ImportError, OSError, RuntimeError):
        return False


# --------------------------------------------------------------------------- #
# OpenClaw 回退路径专用（仅 EASEL_HARNESS=openclaw 时使用）
# --------------------------------------------------------------------------- #
def _node_version_ok(strict: bool) -> bool:
    try:
        result = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return False
        m = re.match(r"v(\d+)\.(\d+)", result.stdout.strip())
        if not m:
            return False
        major, minor = int(m.group(1)), int(m.group(2))
        if strict:
            return (major == 24 and minor >= 16) or (major == 26 and minor >= 1) or major >= 27
        return (major, minor) >= (20, 10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _openclaw_version() -> tuple[int, int, int] | None:
    try:
        from easel.openclaw_cmd import openclaw_base_cmd

        result = subprocess.run(openclaw_base_cmd() + ["--version"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _check_openclaw_fallback(all_ok: bool) -> bool:
    """迁移期回退路径的环境检查（Node.js + openclaw 命令 + gateway）。"""
    oc_ver = _openclaw_version()
    node_strict = oc_ver is None or oc_ver >= (2026, 9, 0)
    node_floor = "24.16" if node_strict else "20.10"
    has_node = shutil.which("node") is not None
    all_ok &= _check(
        f"Node.js >= {node_floor}",
        _node_version_ok(node_strict),
        f"请安装 Node.js >= {node_floor}: https://nodejs.org/" if not has_node
        else f"Node.js 版本不满足 openclaw 要求，请升级到 >= {node_floor}",
    )

    has_openclaw = shutil.which("openclaw") is not None
    all_ok &= _check("openclaw command", has_openclaw, "请安装 openclaw: npm i -g openclaw")
    if has_openclaw:
        min_str = ".".join(map(str, MIN_OPENCLAW))
        ver_str = ".".join(map(str, oc_ver)) if oc_ver else "未知"
        all_ok &= _check(
            f"OpenClaw >= {min_str}",
            oc_ver is not None and oc_ver >= MIN_OPENCLAW,
            f"当前 {ver_str}；请升级：npm i -g openclaw@latest",
        )
        try:
            with urllib.request.urlopen("http://127.0.0.1:18789/healthz", timeout=5) as resp:  # noqa: S310
                gw_ok = resp.status == 200
        except (OSError, urllib.error.URLError):
            gw_ok = False
        all_ok &= _check("OpenClaw gateway (localhost:18789)", gw_ok, "运行 python -m easel gateway start")
    return all_ok


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def cmd_doctor(_args) -> int:
    backend = (os.environ.get("EASEL_HARNESS", "").strip() or "openjiuwen").lower()
    print(f"Easel — 环境检查（harness: {backend}）\n")
    all_ok = True

    # 1. Python 运行时
    all_ok &= _check(
        "Python >= 3.11", _python_version_ok(3, 11),
        f"当前 {sys.version.split()[0]}；openJiuwen 要求 3.11–3.13",
    )
    all_ok &= _check("Python venv module", _venv_available(), "Debian/Ubuntu 请安装 python3-venv")

    # 2. openJiuwen（默认后端，进程内 SDK）
    all_ok &= _check("openjiuwen（agent-core）", _module_available("openjiuwen"),
                     "pip install -U openjiuwen（需 Python 3.11+）")
    all_ok &= _check("openjiuwen.core.runner", _module_available("openjiuwen.core.runner"),
                     "openjiuwen 安装不完整，请重装：pip install -U --force-reinstall openjiuwen")

    info = _harness_describe()
    all_ok &= _check(f"harness '{backend}' 可用", bool(info.get("healthy")),
                     str(info.get("error") or "harness 不可用；检查 EASEL_HARNESS 取值与依赖"))
    model_ok = bool(info.get("model_configured"))
    all_ok &= _check(
        "模型配置（对话模型）", model_ok,
        f"填好 {MEMORY_ENV_FILE} 的 MODEL_NAME / API_KEY / API_BASE，或设 EASEL_OJ_MODEL 等环境变量",
    )
    if info.get("model"):
        print(f"    · provider={info.get('provider') or '-'}  model={info.get('model')}")
    if info.get("placeholder_env"):
        _warn(
            "进程环境里的占位符",
            "检测到疑似占位值：" + ", ".join(info["placeholder_env"])
            + "；它们会被 ~/.jiuwenmemory/.env 的真值纠正，但建议从环境里清掉",
        )

    # 3. ~/.jiuwenmemory/.env（记忆服务的模型配置）
    env_values = _read_env_file()
    if not MEMORY_ENV_FILE.is_file():
        _warn("JiuwenMemory .env", f"{MEMORY_ENV_FILE} 不存在（记忆服务需要它）")
    else:
        missing = [k for k in REQUIRED_ENV_KEYS if not env_values.get(k)]
        all_ok &= _check(
            "JiuwenMemory .env（6 键）", not missing,
            f"缺少：{', '.join(missing)}；编辑 {MEMORY_ENV_FILE} 补齐",
        )

    # 4. JiuwenMemory 记忆服务
    has_memory = _module_available("jiuwen_memory")
    all_ok &= _check("jiuwen_memory（记忆 SDK）", has_memory, "pip install JiuwenMemory")
    if has_memory:
        port = DEFAULT_MEMORY_PORT
        raw_port = (env_values.get("PORT") or "").strip()
        if raw_port.isdigit():
            port = int(raw_port)
        if _memory_service_healthy(port):
            _check(f"记忆服务 /health (127.0.0.1:{port})", True)
        else:
            # 按需拉起：不再要求用户自己另开终端起 memory-server
            # （注意 .env 里的 PORT 会覆盖脚本传参，所以端口一律从 .env 读）
            started, detail = ensure_memory_server(port=port)
            if started:
                _check(f"记忆服务 /health (127.0.0.1:{port})", True)
                print(f"    · {detail}")
            else:
                _warn(
                    f"记忆服务 /health (127.0.0.1:{port})",
                    f"未在监听——{detail}；.env 里 PORT={port}",
                )

    # 5. Python 运行依赖
    for module in ("fastapi", "uvicorn", "sse_starlette", "multipart"):
        all_ok &= _check(f"Python package: {module}", _module_available(module),
                         "运行 pip install -e . 安装 Easel 运行依赖")

    # 6. 前端与媒体
    frontend_ready = (PROJECT_ROOT / "web" / "frontend" / "dist" / "index.html").is_file()
    all_ok &= _check("Web frontend build", frontend_ready,
                     "运行 cd web/frontend && npm ci && npm run build")
    all_ok &= _check("Playwright Chromium", _chromium_available(),
                     "运行 python3 -m playwright install chromium")
    all_ok &= _check("FFmpeg", shutil.which("ffmpeg") is not None, "媒体处理需要 FFmpeg；请安装后重试")

    # 7. 关键项目文件
    for label, path in (
        ("easel/harness/", PROJECT_ROOT / "easel" / "harness"),
        ("skills/", PROJECT_ROOT / "skills"),
        ("web/frontend/", PROJECT_ROOT / "web" / "frontend"),
    ):
        all_ok &= _check(label, path.exists())

    # 8. 仅回退路径才查 OpenClaw
    if backend == "openclaw":
        print("\n  — 回退路径检查（EASEL_HARNESS=openclaw）—")
        all_ok = _check_openclaw_fallback(all_ok)

    print()
    if all_ok:
        print(f"{GREEN}✓ 环境就绪{NC} — 运行 python -m easel ping 验证连通性")
    else:
        print(f"{YELLOW}⚠ 有未满足项{NC} — 请按上述提示修复后重试")
    return 0 if all_ok else 1
