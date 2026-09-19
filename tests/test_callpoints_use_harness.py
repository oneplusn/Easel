"""调用点是否真的走 harness —— 离线验证（默认后端 2026-09-19 起 = openjiuwen）。

这三条调用点（``easel ping`` / ``easel skill`` / ``web.app.run_agent_sync``）过去直接
拼 ``openclaw`` 命令；现在只问 ``get_harness()``。测试用一个假 harness 顶替，
不依赖 openclaw、openJiuwen 或任何模型配额。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from easel.harness import AgentHarness, HarnessError, HarnessUnavailable, RunResult  # noqa: E402
from easel.commands import ping as ping_mod  # noqa: E402
from easel.commands import skill as skill_mod  # noqa: E402


class FakeHarness(AgentHarness):
    """只记录「被调用时的 spec」，不做任何进程/网络操作。"""

    name = "fake"

    def __init__(self, *, healthy: bool = True, text: str = "PONG", fail: Exception | None = None):
        self._healthy = healthy
        self._text = text
        self._fail = fail
        self.specs = []

    def health(self) -> bool:
        return self._healthy

    async def run(self, spec, *, on_event=None, on_permission=None):  # noqa: ANN001
        self.specs.append(spec)
        if self._fail is not None:
            raise self._fail
        return RunResult(text=self._text, stop_reason="end_turn", returncode=0)


# --------------------------------------------------------------------------- #
# easel ping
# --------------------------------------------------------------------------- #
def test_ping_goes_through_harness(monkeypatch):
    fake = FakeHarness(text="PONG")
    monkeypatch.setattr(ping_mod, "get_harness", lambda: fake)

    assert ping_mod.cmd_ping(None) == 0
    assert len(fake.specs) == 1
    spec = fake.specs[0]
    assert spec.cwd == PROJECT_ROOT
    assert "PONG" in spec.message
    assert spec.session_key.startswith("ping-")


def test_ping_fails_when_backend_unavailable(monkeypatch):
    fake = FakeHarness(healthy=False, fail=HarnessUnavailable("找不到 openjiuwen"))
    monkeypatch.setattr(ping_mod, "get_harness", lambda: fake)

    assert ping_mod.cmd_ping(None) == 1


# --------------------------------------------------------------------------- #
# easel skill
# --------------------------------------------------------------------------- #
def _fake_skill_dir(tmp_path: Path) -> Path:
    skills = tmp_path / "skills" / "openclaw"
    (skills / "skill-demo").mkdir(parents=True)
    (skills / "skill-demo" / "SKILL.md").write_text("---\nname: demo\n---\n", encoding="utf-8")
    return skills


def test_skill_goes_through_harness(monkeypatch, tmp_path, capsys):
    skills_dir = _fake_skill_dir(tmp_path)
    monkeypatch.setattr(skill_mod, "SKILLS_DIR", skills_dir)
    fake = FakeHarness(text="回答正文")
    monkeypatch.setattr(skill_mod, "get_harness", lambda: fake)

    rc = skill_mod.cmd_skill(SimpleNamespace(name="demo", input="一段文案", profile=None))

    assert rc == 0
    assert len(fake.specs) == 1
    assert "/skill-demo" in fake.specs[0].message
    assert "一段文案" in fake.specs[0].message
    assert "回答正文" in capsys.readouterr().out


def test_skill_timeout_maps_to_124(monkeypatch, tmp_path):
    monkeypatch.setattr(skill_mod, "SKILLS_DIR", _fake_skill_dir(tmp_path))
    fake = FakeHarness(fail=HarnessError("本轮超时（300s）"))
    monkeypatch.setattr(skill_mod, "get_harness", lambda: fake)

    assert skill_mod.cmd_skill(SimpleNamespace(name="demo", input="x", profile=None)) == 124


def test_skill_keeps_legacy_entry_name():
    """v0.2.x 的旧入口名仍可导入（调用点改名的兼容面）。"""
    assert skill_mod._run_via_openclaw is skill_mod._run_via_harness


# --------------------------------------------------------------------------- #
# web.app.run_agent_sync
# --------------------------------------------------------------------------- #
class _FakeLock:
    def acquire(self, timeout: float = 300.0) -> bool:  # noqa: ARG002
        return True

    def release(self) -> None:
        return None


def test_run_agent_sync_goes_through_harness(monkeypatch):
    web_app = __import__("web.app", fromlist=["run_agent_sync"])
    fake = FakeHarness(text="hello from harness")
    monkeypatch.setattr(web_app, "get_harness", lambda: fake)
    monkeypatch.setattr(web_app, "_heal_openclaw_session", lambda sk: None)  # noqa: ARG005
    monkeypatch.setattr(web_app, "_CrossProcLock", lambda sk: _FakeLock())  # noqa: ARG005

    assert web_app.run_agent_sync("hi", timeout=30) == "hello from harness"
    spec = fake.specs[0]
    assert spec.message == "hi"
    assert spec.session_key.startswith("web-")      # 未显式给 session_id 时按 web-<ms> 生成
    assert spec.cwd == PROJECT_ROOT


def test_run_agent_sync_maps_timeout(monkeypatch):
    web_app = __import__("web.app", fromlist=["run_agent_sync"])
    fake = FakeHarness(fail=HarnessError("本轮超时（300s）"))
    monkeypatch.setattr(web_app, "get_harness", lambda: fake)
    monkeypatch.setattr(web_app, "_heal_openclaw_session", lambda sk: None)  # noqa: ARG005
    monkeypatch.setattr(web_app, "_CrossProcLock", lambda sk: _FakeLock())  # noqa: ARG005

    assert web_app.run_agent_sync("hi", timeout=30) == "⏱️ 请求超时"
