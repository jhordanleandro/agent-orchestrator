"""Infraestrutura comum: repositório git temporário, cache isolado e agentes falsos."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import orchestrator as orch  # noqa: E402

FAKE_AGENT = Path(__file__).resolve().parent / "fake_agent.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Logs, worktrees e cota do claude numa pasta do teste, nunca no ~/.cache real."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(orch, "CACHE_DIR", cache)
    monkeypatch.setattr(orch, "RUNS_DIR", cache / "runs")
    monkeypatch.setattr(orch, "WORKTREES_DIR", cache / "worktrees")
    monkeypatch.setattr(orch, "CLAUDE_LIMITS_FILE", cache / "claude-limits.json")
    # Sem consultar o codex de verdade: cada teste diz quais MCP existem.
    monkeypatch.setattr(orch, "codex_mcp_servers", lambda: [])
    # O módulo guardou o stdout da importação; o pytest troca os fluxos por teste.
    monkeypatch.setattr(orch, "_log_stream", sys.stderr)
    return cache


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Teste")
    git(path, "config", "user.email", "teste@localhost")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "inicial")
    return path


def decision(agent: str, prompt: str = "faça", purpose: str | None = None, summary: str = "") -> dict[str, Any]:
    return {"out": json.dumps({
        "next_agent": agent, "reason": f"motivo {agent}", "claude_purpose": purpose,
        "prompt": prompt, "summary": summary,
    })}


class Fakes:
    """Roteiros dos agentes falsos e leitura do que cada um recebeu."""

    def __init__(self, base: Path) -> None:
        self.base = base

    def script(self, name: str, actions: list[dict[str, Any]]) -> None:
        (self.base / f"{name}.json").write_text(json.dumps(actions), encoding="utf-8")

    def calls(self, name: str) -> list[dict[str, Any]]:
        path = self.base / f"{name}.calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    base = tmp_path / "fakes"
    base.mkdir()
    monkeypatch.setenv("FAKE_DIR", str(base))
    return Fakes(base)


def _cmd(name: str, *extra: str) -> list[str]:
    return [sys.executable, str(FAKE_AGENT), name, *extra]


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """O orchestrator.json real, com os comandos trocados pelos agentes falsos."""
    cfg = json.loads((ROOT / "orchestrator.json").read_text(encoding="utf-8"))
    cfg["supervisor"]["direct"] = {"cmd": _cmd("supervisor", "{prompt}"), "prompt_via": "arg"}
    cfg["supervisor_fallback"]["direct"] = {"cmd": _cmd("fallback"), "prompt_via": "stdin"}
    cfg["agents"]["agy"]["direct"] = {"cmd": _cmd("agy", "{prompt}"), "prompt_via": "arg"}
    cfg["agents"]["codex"]["direct"] = {"cmd": _cmd("codex", "{codex_mcp_off}", "exec", "-"), "prompt_via": "stdin"}
    cfg["agents"]["claude"]["direct"] = {"cmd": _cmd("claude"), "prompt_via": "stdin"}
    cfg["timeouts"] = {k: 60 for k in cfg["timeouts"]}
    path = tmp_path / "orchestrator.test.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Liga os eventos JSONL e devolve uma função que lê os emitidos até agora."""
    monkeypatch.setattr(orch, "_events_enabled", True)
    seen: list[dict[str, Any]] = []

    def read() -> list[dict[str, Any]]:
        out = capsys.readouterr().out
        seen.extend(json.loads(line) for line in out.splitlines() if line.startswith("{"))
        return seen

    return read


def last_run_log(cache: Path) -> dict[str, Any]:
    logs = sorted((cache / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime)
    assert logs, "nenhum log de execução gravado"
    return json.loads(logs[-1].read_text(encoding="utf-8"))
