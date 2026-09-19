"""``easel.memory``：记忆服务按需拉起的单元测试。

不碰真端口、不起真服务：探活用 monkeypatch，拉起路径用「立刻退出的假 exe」验证
「起了但健康检查没起来」会如实返回 False 并给出日志路径。
"""

from __future__ import annotations

import sys
import time

import pytest

from easel import memory


def test_read_memory_env_parses_and_strips(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nPORT=8317\nAPI_KEY="sk-x"\n\nEMBED_MODEL=\'m3\'\nBROKEN\n',
        encoding="utf-8",
    )
    values = memory.read_memory_env(env_file)
    assert values == {"PORT": "8317", "API_KEY": "sk-x", "EMBED_MODEL": "m3"}


def test_memory_port_prefers_env_and_falls_back():
    assert memory.memory_port({"PORT": "8317"}) == 8317
    assert memory.memory_port({}) == memory.DEFAULT_MEMORY_PORT
    assert memory.memory_port({"PORT": "not-a-port"}) == memory.DEFAULT_MEMORY_PORT


def test_memory_service_healthy_false_when_nothing_listens():
    # 高位端口，且超时很短，避免测试拖慢
    assert memory.memory_service_healthy(59987, timeout=0.3) is False


def test_placeholder_env_flags_known_placeholders(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "your-model-name")
    monkeypatch.setenv("API_KEY", "sk-real-looking-but-short")
    monkeypatch.setenv("API_BASE", "https://example.com/compatible-mode/v1")
    hits = memory.placeholder_env()
    assert hits == ["MODEL_NAME", "API_BASE"]


def test_ensure_memory_server_short_circuits_when_healthy(monkeypatch):
    monkeypatch.setattr(memory, "memory_service_healthy", lambda *a, **k: True)
    ok, detail = memory.ensure_memory_server(port=8317)
    assert ok is True
    assert "8317" in detail


def test_ensure_memory_server_reports_missing_executable(monkeypatch):
    monkeypatch.setattr(memory, "memory_service_healthy", lambda *a, **k: False)
    monkeypatch.setattr(memory, "find_memory_server", lambda: None)
    monkeypatch.delenv("EASEL_MEMORY_SERVER", raising=False)
    ok, detail = memory.ensure_memory_server(port=8317, wait_s=0.1)
    assert ok is False
    assert "memory-server" in detail


def test_ensure_memory_server_spawns_then_times_out(tmp_path, monkeypatch):
    """假 exe 立刻退出 → 不该谎报成功，要返回 False 并指向日志文件。"""
    monkeypatch.setattr(memory, "memory_service_healthy", lambda *a, **k: False)
    monkeypatch.setattr(memory, "find_memory_server", lambda: sys.executable)
    log_path = tmp_path / "logs" / "memory-server.log"
    started = time.monotonic()
    ok, detail = memory.ensure_memory_server(port=8317, wait_s=0.6, log_path=log_path)
    assert ok is False
    assert "memory-server.log" in detail
    assert log_path.parent.is_dir()
    assert time.monotonic() - started < 10
