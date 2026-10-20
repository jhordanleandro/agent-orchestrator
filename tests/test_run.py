"""Execuções completas via main(), com agentes falsos seguindo roteiros."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import decision, git, last_run_log, orch

APROVADO = {"out": "Sem problemas.\n\nVEREDITO: APROVADO"}
AJUSTES = {"out": "Falta tratar X.\n\nVEREDITO: AJUSTES"}


def run(config_path: Path, repo: Path, *extra: str, task: str = "Implementar X") -> int:
    return orch.main(["--config", str(config_path), "--repo", str(repo), *extra, task])


def test_fluxo_feliz_termina_sozinho_apos_aprovado(config_path, repo, fakes, isolated_cache, events) -> None:
    fakes.script("supervisor", [decision("codex", "edite a.txt"), decision("claude", "revise", "review")])
    fakes.script("codex", [{"append": {"a.txt": "b\n"}, "out": "editei a.txt"}])
    fakes.script("claude", [APROVADO])

    assert run(config_path, repo, "--isolation", "none", "--check", "true") == 0

    log = last_run_log(isolated_cache)
    assert log["status"] == "done"
    assert [s["agent"] for s in log["steps"]] == ["codex", "claude"]
    assert "APROVADO" in log["final_summary"]
    # Sem um 3º ciclo do supervisor só para "validar tudo de novo".
    assert len(fakes.calls("supervisor")) == 2
    # O claude fica sabendo que as verificações já passaram e não tenta rodá-las.
    assert "Verificações já feitas" in fakes.calls("claude")[0]["stdin"]
    assert "Verificações já feitas" not in fakes.calls("codex")[0]["stdin"]
    auto = [e for e in events() if e["type"] == "decision" and e.get("auto")]
    assert auto and auto[0]["next_agent"] == "DONE"
    # O passo do codex registra o arquivo que ele alterou.
    assert log["steps"][0]["files"][0]["path"] == "a.txt"


def test_aprovado_sem_verificacoes_nao_encerra_sozinho(config_path, repo, fakes, isolated_cache) -> None:
    """Sem --check, ninguém garantiu que o código revisado passa: decide o supervisor."""
    fakes.script("supervisor", [decision("codex"), decision("claude", purpose="review"), decision("DONE", summary="ok")])
    fakes.script("codex", [{"append": {"a.txt": "b\n"}}])
    fakes.script("claude", [APROVADO])
    assert run(config_path, repo, "--isolation", "none") == 0
    assert len(fakes.calls("supervisor")) == 3
    assert last_run_log(isolated_cache)["final_summary"] == "ok"


def test_ajustes_continua_ate_o_limite(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [decision("codex"), decision("claude", purpose="review"), decision("codex")])
    fakes.script("codex", [{"append": {"a.txt": "b\n"}}])
    fakes.script("claude", [AJUSTES])
    assert run(config_path, repo, "--isolation", "none", "--check", "true", "--max-loops", "3") == 1
    log = last_run_log(isolated_cache)
    assert log["status"] == "max_loops"
    assert [s["agent"] for s in log["steps"]] == ["codex", "claude", "codex"]


def test_supervisor_vazio_passa_logo_para_o_reserva(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [{"out": ""}])
    fakes.script("fallback", [decision("DONE", summary="feito pelo reserva")])
    assert run(config_path, repo, "--isolation", "none") == 0
    # supervisor_retries = 0 no orchestrator.json: uma falha e já vai ao reserva.
    assert len(fakes.calls("supervisor")) == 1 and len(fakes.calls("fallback")) == 1
    log = last_run_log(isolated_cache)
    assert log["final_summary"] == "feito pelo reserva"
    assert any("reserva" in w for w in log["warnings"])
    # O reserva recebe o mesmo prompt, com o aviso de que faltou o JSON.
    assert "ATENÇÃO: sua resposta anterior" in fakes.calls("fallback")[0]["stdin"]


def test_reserva_respeita_o_proprio_tempo_limite(config_path, repo, fakes, isolated_cache) -> None:
    cfg = json.loads(config_path.read_text())
    cfg["supervisor_fallback"]["timeout"] = 1
    config_path.write_text(json.dumps(cfg))
    fakes.script("supervisor", [{"out": ""}])
    fakes.script("fallback", [{"sleep": 30}])
    assert run(config_path, repo, "--isolation", "none") == 1
    log = last_run_log(isolated_cache)
    assert log["status"] == "aborted"
    assert log["stats"]["supervisor"]["seconds"] < 15


def test_sem_decisao_nem_do_reserva_aborta(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [{"out": "não sei"}])
    fakes.script("fallback", [{"out": "também não"}])
    assert run(config_path, repo, "--isolation", "none") == 1
    assert last_run_log(isolated_cache)["status"] == "aborted"


def test_filtro_barra_o_claude_quando_a_verificacao_falha(config_path, repo, fakes, isolated_cache, events) -> None:
    fakes.script("supervisor", [decision("codex"), decision("claude", purpose="review"), decision("DONE")])
    fakes.script("codex", [{"append": {"a.txt": "b\n"}}])
    fakes.script("claude", [APROVADO])
    run(config_path, repo, "--isolation", "none", "--check", "exit 3")
    log = last_run_log(isolated_cache)
    assert fakes.calls("claude") == []
    assert log["steps"][1]["agent"] == "codex" and log["steps"][1]["redirected_from"] == "claude"
    assert "exit 3" in log["steps"][1]["prompt"]
    blocked = [e for e in events() if e["type"] == "gate_blocked"]
    assert blocked and blocked[0]["target"] == "codex"


def test_filtro_exige_alteracoes_antes_da_revisao(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [decision("claude", purpose="review"), decision("DONE")])
    fakes.script("codex", [{"out": "nada"}])
    fakes.script("claude", [APROVADO])
    run(config_path, repo, "--isolation", "none")
    assert fakes.calls("claude") == []
    assert last_run_log(isolated_cache)["steps"][0]["redirected_from"] == "claude"


def test_diagnostico_exige_tentativas_do_codex(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [decision("claude", purpose="diagnosis"), decision("DONE")])
    fakes.script("codex", [{"out": "tentei"}])
    fakes.script("claude", [APROVADO])
    run(config_path, repo, "--isolation", "none")
    step = last_run_log(isolated_cache)["steps"][0]
    assert step["agent"] == "codex" and "2 tentativas" in step["prompt"]


def test_padrao_proibido_interrompe_a_execucao(config_path, repo, fakes, isolated_cache) -> None:
    (repo / ".orchestrator.json").write_text(json.dumps({
        "forbidden_patterns": [r"git\s+push"], "on_forbidden": "abort",
    }), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "config")
    fakes.script("supervisor", [decision("codex"), decision("DONE")])
    fakes.script("codex", [{"out": "rodei git push origin main"}])
    assert run(config_path, repo, "--isolation", "none") == 1
    log = last_run_log(isolated_cache)
    assert log["status"] == "blocked" and len(log["steps"]) == 1


def test_codex_recebe_flags_que_desligam_os_mcp(config_path, repo, fakes, monkeypatch) -> None:
    monkeypatch.setattr(orch, "codex_mcp_servers", lambda: ["hostinger-dns", "outro"])
    fakes.script("supervisor", [decision("codex"), decision("DONE")])
    fakes.script("codex", [{"out": "ok"}])
    run(config_path, repo, "--isolation", "none")
    assert fakes.calls("codex")[0]["args"] == [
        "-c", "mcp_servers.hostinger-dns.enabled=false", "-c", "mcp_servers.outro.enabled=false", "exec", "-",
    ]


def test_prompt_do_agente_traz_regras_e_contexto(config_path, repo, fakes) -> None:
    (repo / ".orchestrator.json").write_text(json.dumps({"rules": "Responda em PT-BR."}), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "config")
    fakes.script("supervisor", [decision("codex", "primeiro"), decision("codex", "segundo"), decision("DONE")])
    fakes.script("codex", [{"out": "SAIDA_DO_PRIMEIRO"}, {"out": "ok"}])
    run(config_path, repo, "--isolation", "none")
    first, second = (c["stdin"] for c in fakes.calls("codex"))
    assert "Responda em PT-BR." in first and "primeiro" in first
    assert "SAIDA_DO_PRIMEIRO" in second
    # O supervisor recebe o estado do git pronto, já que não pode rodar comandos.
    assert "git status --short" in fakes.calls("supervisor")[0]["args"][0]


def test_worktree_isola_e_aceitar_traz_para_o_repositorio(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [decision("codex"), decision("DONE")])
    fakes.script("codex", [{"write": {"novo.txt": "feito\n"}}])
    assert run(config_path, repo) == 0

    log = last_run_log(isolated_cache)
    iso = log["isolation"]
    assert iso["pending"] and iso["branch"].startswith("orch/")
    assert fakes.calls("codex")[0]["cwd"] == iso["worktree"]
    assert not (repo / "novo.txt").exists()

    assert orch.main(["--repo", str(repo), "--accept", iso["branch"]]) == 0
    assert (repo / "novo.txt").read_text() == "feito\n"
    assert git(repo, "diff", "--cached", "--name-only").split() == ["novo.txt"]
    assert not Path(iso["worktree"]).exists()


def test_worktree_sem_alteracoes_e_removida(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [decision("DONE")])
    assert run(config_path, repo) == 0
    iso = last_run_log(isolated_cache)["isolation"]
    assert not iso["pending"] and not Path(iso["worktree"]).exists()
    assert git(repo, "branch", "--list", "orch/*") == ""


def test_retomada_continua_na_mesma_worktree(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [decision("codex"), decision("codex"), decision("DONE")])
    fakes.script("codex", [{"write": {"um.txt": "1\n"}}, {"write": {"dois.txt": "2\n"}}])
    assert run(config_path, repo, "--max-loops", "1") == 1
    first = last_run_log(isolated_cache)
    assert first["status"] == "max_loops"

    log_file = sorted((isolated_cache / "runs").glob("*.json"))[-1]
    assert orch.main(["--config", str(config_path), "--resume", str(log_file), "--max-loops", "2"]) == 0
    second = last_run_log(isolated_cache)
    assert second["resumed_from"] == str(log_file)
    assert [s["cycle"] for s in second["steps"]] == [1, 2]
    assert second["isolation"]["branch"] == first["isolation"]["branch"]
    files = git(repo, "ls-tree", "-r", "--name-only", second["isolation"]["branch"]).split()
    assert {"um.txt", "dois.txt"} <= set(files)


def test_aprovacao_editada_e_recusa(config_path, repo, fakes, isolated_cache, monkeypatch) -> None:
    fakes.script("supervisor", [decision("codex", "original"), decision("codex", "de novo"), decision("DONE")])
    fakes.script("codex", [{"out": "ok"}])
    real_init = orch.Control.__init__

    def scripted(self, on_note):
        real_init(self, on_note)
        self.decisions.put({"cmd": "reject", "reason": "não gostei"})
        self.decisions.put({"cmd": "approve", "prompt": "prompt editado"})

    monkeypatch.setattr(orch.Control, "__init__", scripted)
    monkeypatch.setattr(orch.Control, "start", lambda self: None)
    run(config_path, repo, "--isolation", "none", "--control", "stdin", "--approve", "all")
    steps = last_run_log(isolated_cache)["steps"]
    assert steps[0]["code"] == -1 and "não gostei" in steps[0]["output"]
    assert fakes.calls("codex")[0]["stdin"].count("prompt editado") == 1
    assert "não gostei" in fakes.calls("supervisor")[1]["args"][0]


def test_dois_logs_no_mesmo_segundo_nao_se_sobrescrevem(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [decision("DONE")])
    run(config_path, repo, "--isolation", "none")
    run(config_path, repo, "--isolation", "none")
    assert len(list((isolated_cache / "runs").glob("*.json"))) == 2


@pytest.mark.skipif(orch.resolve_node_bin("24")[0] is None, reason="fnm com Node 24 não instalado")
def test_node_do_projeto_vai_na_frente_do_path(config_path, repo, fakes) -> None:
    (repo / ".orchestrator.json").write_text(json.dumps({"node": "24"}), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "config")
    fakes.script("supervisor", [decision("codex"), decision("DONE")])
    fakes.script("codex", [{"out": "ok"}])
    run(config_path, repo, "--isolation", "none")
    assert "v24." in fakes.calls("codex")[0]["node"]
