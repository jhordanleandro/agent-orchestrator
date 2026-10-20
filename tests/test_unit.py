"""Peças isoladas: parser de decisão, veredito, streams, configuração e git."""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from conftest import git, orch

# ─── Decisão do supervisor ───────────────────────────────────────────────────


def test_decisao_em_bloco_json_no_meio_de_texto() -> None:
    text = "Analisei o repo.\n```json\n{\"next_agent\": \"codex\", \"prompt\": \"x\"}\n```\nFim."
    assert orch.extract_decision(text) == {"next_agent": "codex", "prompt": "x"}


def test_decisao_vale_a_ultima_quando_o_exemplo_do_prompt_e_ecoado() -> None:
    text = (
        'Formato: {"next_agent": "agy|codex|claude|DONE", "prompt": "..."}\n'
        'Minha resposta: {"next_agent": "claude", "prompt": "revise"}'
    )
    assert orch.extract_decision(text)["next_agent"] == "claude"


def test_decisao_com_ansi_e_chaves_dentro_do_prompt() -> None:
    raw = {"next_agent": "codex", "prompt": "use {\"a\": 1} e function f() { return 1 }"}
    text = f"\x1b[32mok\x1b[0m {json.dumps(raw)} depois"
    assert orch.extract_decision(text) == raw


@pytest.mark.parametrize("text", ["", "sem json aqui", '{"outra": "coisa"}', "{quebrado"])
def test_sem_decisao_valida(text: str) -> None:
    assert orch.extract_decision(text) is None


@pytest.mark.parametrize("agent", ["done", "FIM", "concluído", "none", ""])
def test_normaliza_variantes_de_done(agent: str) -> None:
    assert orch.normalize_decision({"next_agent": agent})["next_agent"] == "DONE"


def test_normaliza_proposito_e_textos_vazios() -> None:
    d = orch.normalize_decision({"next_agent": " Codex ", "claude_purpose": "inventado", "prompt": None})
    assert d == {"next_agent": "codex", "reason": "(sem motivo informado)", "claude_purpose": "review",
                 "prompt": "", "summary": ""}
    assert orch.normalize_decision({"next_agent": "claude", "claude_purpose": "Diagnosis"})["claude_purpose"] == "diagnosis"


# ─── Veredito da revisão ─────────────────────────────────────────────────────


@pytest.mark.parametrize("text, expected", [
    ("Tudo certo.\nVEREDITO: APROVADO", "APROVADO"),
    ("**VEREDITO:** aprovado", "APROVADO"),
    ("VEREDITO: **AJUSTES**", "AJUSTES"),
    ("Antes: VEREDITO: AJUSTES\n...\nVEREDITO: APROVADO", "APROVADO"),
    ("Aprovado, sem a linha final", None),
    ("", None),
])
def test_veredito(text: str, expected: str | None) -> None:
    assert orch.review_verdict(text) == expected


# ─── Utilitários ─────────────────────────────────────────────────────────────


def test_clip_preserva_comeco_e_fim() -> None:
    text = "INICIO" + "x" * 1000 + "FIM_DO_ERRO"
    out = orch.clip(text, 90)
    assert out.startswith("INICIO") and out.endswith("FIM_DO_ERRO") and "omitidos" in out
    assert orch.clip("  curto  ", 90) == "curto"


def test_carimbo_unico_desvia_dos_ja_usados(monkeypatch: pytest.MonkeyPatch) -> None:
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return datetime(2026, 10, 20, 7, 29, 13, 123456)

    monkeypatch.setattr(orch, "datetime", Fixed)
    assert orch.unique_stamp(lambda s: False) == "20261020-072913-123"
    taken = {"20261020-072913-123", "20261020-072913-123-2"}
    assert orch.unique_stamp(taken.__contains__) == "20261020-072913-123-3"


def test_fill_nao_interpreta_chaves_do_prompt() -> None:
    values = {"{prompt}": 'x = {"a": 1}', "{repo}": "/r"}
    assert orch.Orchestrator._fill("{repo}:{prompt}:{outro}", values) == '/r:x = {"a": 1}:{outro}'


def test_janelas_do_claude_ignoram_as_vencidas() -> None:
    now = time.time()
    limits = {"unifiedWindows": {
        "five_hour": {"utilization": 0.5, "resetsAt": now + 3600},
        "seven_day": {"utilization": 0.9, "resetsAt": now - 10},
    }}
    assert orch.claude_windows(limits) == {"five_hour": {"utilization": 0.5, "resets_at": now + 3600}}
    assert orch.claude_windows(None) == {}


# ─── Streams das CLIs ────────────────────────────────────────────────────────


def test_stream_codex(tmp_path: Path) -> None:
    p = orch.StreamParser("codex-json", tmp_path)
    lines = [
        {"type": "item.started", "item": {"type": "command_execution", "command": "/bin/bash -lc 'pnpm test'"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pnpm test", "exit_code": 1,
                                            "aggregated_output": "a\nb\nc\nFALHOU"}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": str(tmp_path / "src/x.ts"), "kind": "update"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Pronto."}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
    ]
    acts = [a for line in lines for a in p.feed(json.dumps(line))]
    assert acts[0].text == "$ pnpm test"
    assert acts[1].data["exit_code"] == 1 and acts[1].data["tail"].endswith("FALHOU")
    assert acts[2].data == {"path": "src/x.ts", "change": "update"}
    assert p.text() == "Pronto." and p.tokens == 15
    assert p.feed("linha solta")[0].kind == "message"


def test_stream_claude(tmp_path: Path) -> None:
    p = orch.StreamParser("claude-stream", tmp_path)
    p.feed(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Revisando"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": str(tmp_path / "a.py")}},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "TODO"}},
    ]}}))
    p.feed(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {}}}))
    p.feed(json.dumps({"type": "result", "result": "VEREDITO: APROVADO", "total_cost_usd": 0.12,
                       "usage": {"input_tokens": 3, "output_tokens": 4, "cache_creation_input_tokens": 5}}))
    assert p.text() == "VEREDITO: APROVADO"
    assert p.cost_usd == 0.12 and p.tokens == 12 and p.limits == {"unifiedWindows": {}}


def test_stream_agy_registra_acoes_negadas(tmp_path: Path) -> None:
    p = orch.StreamParser("agy-stream", tmp_path)
    acts = p.feed(json.dumps({"event": "step_update", "step_update": {
        "step_type": "tool", "state": "DONE", "tool_name": "replace_file_content",
        "tool_info": {"parameters": {"TargetFile": str(tmp_path / "page.tsx")}}}}))
    assert acts[0].kind == "file" and acts[0].data["path"] == "page.tsx"
    acts = p.feed(json.dumps({"event": "result", "result": {
        "response": "", "denied_actions": [{"action": "run_command"}, {"action": "run_command"}]}}))
    assert p.denied == ["run_command", "run_command"]
    assert acts[0].kind == "warning" and acts[0].text.endswith("run_command")


# ─── Configuração do projeto ─────────────────────────────────────────────────


def _project(tmp_path: Path, data: object) -> Path:
    (tmp_path / ".orchestrator.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def test_config_do_projeto_ausente(tmp_path: Path) -> None:
    assert orch.load_project_config(tmp_path) == ({}, None)


def test_config_do_projeto_valida(tmp_path: Path) -> None:
    data = {"checks": ["pnpm test"], "node": "24", "templates": [{"name": "t", "task": "x"}]}
    loaded, path = orch.load_project_config(_project(tmp_path, data))
    assert loaded == data and path == tmp_path / ".orchestrator.json"


@pytest.mark.parametrize("data, erro", [
    ({"checks": "pnpm test"}, "'checks' precisa ser list"),
    ({"node": 24}, "'node' precisa ser str"),
    ({"templates": [{"name": "sem task"}]}, "templates[0]"),
    ({"forbidden_patterns": ["(aberto"]}, "padrão proibido inválido"),
])
def test_config_do_projeto_invalida(tmp_path: Path, data: dict, erro: str) -> None:
    with pytest.raises(SystemExit) as exc:
        orch.load_project_config(_project(tmp_path, data))
    assert erro in str(exc.value)


def test_config_padrao_tem_os_campos_que_o_codigo_usa() -> None:
    cfg = orch.load_config(orch.DEFAULT_CONFIG)
    for key in ("supervisor", "agents", "timeouts", "quality_gate", "claude_policy", "history"):
        assert key in cfg
    assert set(cfg["agents"]) == set(orch.WORKERS)
    assert "VEREDITO" in cfg["agents"]["claude"]["role"]
    assert "{codex_mcp_off}" in cfg["agents"]["codex"]["direct"]["cmd"]


# ─── Execução de comandos ────────────────────────────────────────────────────


def test_run_command_stdin_e_linhas_ao_vivo(tmp_path: Path) -> None:
    lines: list[tuple[str, str]] = []
    res = orch.run_command([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                           tmp_path, 30, stdin_text="oi", on_line=lambda s, l: lines.append((s, l)))
    assert res.ok and res.stdout == "OI\n" and lines == [("stdout", "OI")]


def test_run_command_tempo_esgotado_mata_o_grupo(tmp_path: Path) -> None:
    # O neto (sleep) também precisa morrer, senão seguraria o pipe.
    res = orch.run_command("sleep 30 & sleep 30", tmp_path, 1, shell=True)
    assert res.timed_out and res.code == 124 and res.seconds < 15


def test_run_command_abortado_por_outra_thread(tmp_path: Path) -> None:
    abort = threading.Event()
    threading.Timer(0.5, abort.set).start()
    res = orch.run_command(["sleep", "30"], tmp_path, 60, abort=abort)
    assert res.code == 130 and "interrompido" in res.stderr


def test_run_command_binario_inexistente(tmp_path: Path) -> None:
    assert orch.run_command(["nao-existe-xyz"], tmp_path, 5).code == 127


# ─── Git ─────────────────────────────────────────────────────────────────────


def test_retratos_e_arquivos_alterados(repo: Path) -> None:
    before = orch.snapshot_tree(repo)
    (repo / "a.txt").write_text("a\nb\n", encoding="utf-8")
    (repo / "novo.txt").write_text("n\n", encoding="utf-8")
    after = orch.snapshot_tree(repo)
    files = {f["path"]: f for f in orch.tree_changes(repo, before, after)}
    assert files["a.txt"] == {"status": "M", "path": "a.txt", "added": 1, "deleted": 0}
    assert files["novo.txt"]["status"] == "A"
    assert orch.tree_changes(repo, after, after) == []
    # O retrato não mexe no stage do usuário.
    assert git(repo, "diff", "--cached", "--name-only") == ""


def _branch_with_change(repo: Path, content: str) -> str:
    git(repo, "checkout", "-q", "-b", "orch/teste")
    (repo / "a.txt").write_text(content, encoding="utf-8")
    git(repo, "commit", "-q", "-am", "orch")
    git(repo, "checkout", "-q", "main")
    return "orch/teste"


def test_aceitar_branch_aplica_em_stage(repo: Path) -> None:
    branch = _branch_with_change(repo, "da branch\n")
    ok, msg = orch.accept_branch(repo, branch)
    assert ok, msg
    assert git(repo, "diff", "--cached", "--name-only").split() == ["a.txt"]
    assert git(repo, "branch", "--list", branch) == ""


def test_aceitar_branch_com_conflito_nao_toca_em_nada(repo: Path) -> None:
    branch = _branch_with_change(repo, "da branch\n")
    (repo / "a.txt").write_text("do main\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "main mudou")
    ok, msg = orch.accept_branch(repo, branch)
    assert not ok and "a.txt" in msg
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "branch", "--list", branch).strip()


def test_descartar_branch(repo: Path) -> None:
    branch = _branch_with_change(repo, "x\n")
    assert orch.discard_branch(repo, branch)[0]
    assert git(repo, "branch", "--list", branch) == ""
    assert not orch.accept_branch(repo, branch)[0]
