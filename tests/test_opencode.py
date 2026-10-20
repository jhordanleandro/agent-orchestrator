"""opencode como agente alternativo: segredos, permissões, stream e fallback."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import decision, last_run_log, orch


def _oc_events(*events: dict) -> dict:
    """Ação do agente falso que imprime eventos no formato `opencode run --format json`."""
    return {"out": "".join(json.dumps(e) + "\n" for e in events)}


def _secrets(tmp_path: Path, text: str = "OPENROUTER_API_KEY=sk-or-teste\n") -> None:
    (tmp_path / "secrets.env").write_text(text, encoding="utf-8")


def test_le_segredos_ignorando_linhas_invalidas(tmp_path: Path) -> None:
    path = tmp_path / "s.env"
    path.write_text("# comentário\nexport A=\"x y\"\nB='z'\nVAZIA=\n1INVALIDA=n\nC=w=v\n", encoding="utf-8")
    assert orch.load_secrets(path) == {"A": "x y", "B": "z", "C": "w=v"}
    assert orch.load_secrets(tmp_path / "nao-existe") == {}


def test_agente_indisponivel_sem_binario_ou_sem_chave() -> None:
    spec = {"direct": {"cmd": ["python3"], "env_from_secrets": ["OPENROUTER_API_KEY"]}}
    assert "OPENROUTER_API_KEY" in orch.agent_unavailable(spec, {})
    assert orch.agent_unavailable(spec, {"OPENROUTER_API_KEY": "x"}) is None
    assert "não está no PATH" in orch.agent_unavailable({"direct": {"cmd": ["nao-existe-xyz"]}}, {})


def test_stream_do_opencode(tmp_path: Path) -> None:
    p = orch.StreamParser("opencode-json", tmp_path)
    acts = []
    for e in (
        {"type": "tool_use", "part": {"tool": "edit", "state": {"status": "completed",
                                                                  "input": {"filePath": str(tmp_path / "src/a.ts")}}}},
        {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed", "input": {"command": "pnpm test"},
                                                                  "metadata": {"exit": 1}, "output": "a\nFALHOU"}}},
        {"type": "tool_use", "part": {"tool": "bash", "state": {"status": "error", "input": {"command": "git push"},
                                                                  "error": "The user has specified a rule which prevents you"}}},
        {"type": "text", "part": {"text": "Pronto."}},
        {"type": "step_finish", "part": {"cost": 0.002, "tokens": {"input": 100, "output": 20}}},
        {"type": "error", "error": {"name": "APIError", "data": {"message": "sem crédito"}}},
    ):
        acts += p.feed(json.dumps(e))
    assert acts[0].data == {"path": "src/a.ts", "change": "edit"}
    assert acts[1].data["exit_code"] == 1 and acts[1].data["tail"].endswith("FALHOU")
    assert acts[2].text.startswith("❌ git push")
    assert acts[-1].kind == "warning" and "sem crédito" in acts[-1].text
    assert p.tokens == 120 and p.cost_usd == 0.002 and p.denied == ["bash"]
    assert p.text() == "Pronto."


def test_erro_do_opencode_vira_a_saida_do_passo(tmp_path: Path) -> None:
    """Teste real (20/10): sem crédito, o passo saía vazio e ninguém via o motivo."""
    p = orch.StreamParser("opencode-json", tmp_path)
    p.feed(json.dumps({"type": "error", "error": {"name": "APIError", "data": {"message": "requires more credits"}}}))
    assert "requires more credits" in p.text()


def test_opencode_recebe_chave_permissoes_e_prompt_anexado(config_path, repo, fakes, isolated_cache, tmp_path) -> None:
    _secrets(tmp_path)
    fakes.script("supervisor", [decision("opencode", "edite a.txt"), decision("codex"), decision("DONE")])
    fakes.script("opencode", [{**_oc_events(
        {"type": "tool_use", "part": {"tool": "write", "state": {"status": "completed",
                                                                  "input": {"filePath": str(repo / "a.txt")}}}},
        {"type": "text", "part": {"text": "Editei a.txt."}},
        {"type": "step_finish", "part": {"cost": 0.01, "tokens": {"input": 50, "output": 5}}},
    ), "append": {"a.txt": "b\n"}}])
    fakes.script("codex", [{"out": "ok"}])
    assert orch.main(["--config", str(config_path), "--repo", str(repo), "--isolation", "none", "Tarefa"]) == 0

    call = fakes.calls("opencode")[0]
    assert call["env"]["OPENROUTER_API_KEY"] == "sk-or-teste"
    # A chave vai só para o agente que a declara.
    assert fakes.calls("codex")[0]["env"]["OPENROUTER_API_KEY"] is None
    # Prompt completo anexado por arquivo (o opencode não lê pastas fora do projeto).
    prompt_file = Path(call["args"][1])
    assert call["args"][0] == "-f" and prompt_file.name.endswith("-opencode-prompt.md")
    # Permissões próprias desta execução, via OPENCODE_CONFIG.
    assert call["env"]["OPENCODE_CONFIG"].endswith("-opencode.json")
    log = last_run_log(isolated_cache)
    assert log["steps"][0]["agent"] == "opencode" and log["steps"][0]["output"] == "Editei a.txt."
    assert log["stats"]["opencode"]["cost_usd"] == 0.01 and log["stats"]["opencode"]["tokens"] == 55
    assert "sk-or-teste" not in json.dumps(log)


def test_permissoes_geradas_bloqueiam_rede_push_e_pastas_externas() -> None:
    cfg = orch.load_config(orch.DEFAULT_CONFIG)
    spec = cfg["agents"]["opencode"]
    perm = spec["direct"]["config_file"]["content"]["permission"]
    assert spec["optional"] is True
    assert perm["webfetch"] == "deny" and perm["external_directory"] == "deny"
    assert perm["bash"]["git push*"] == "deny" and perm["bash"]["curl *"] == "deny"
    assert spec["direct"]["config_file"]["content"]["share"] == "disabled"
    # Títulos de sessão com o mesmo modelo (o padrão era um modelo de imagem).
    model = spec["direct"]["cmd"][spec["direct"]["cmd"].index("-m") + 1]
    assert spec["direct"]["config_file"]["content"]["small_model"] == model
    assert "--auto" not in spec["direct"]["cmd"]


def test_sem_chave_o_passo_vai_para_o_codex(config_path, repo, fakes, isolated_cache) -> None:
    fakes.script("supervisor", [decision("opencode"), decision("DONE")])
    fakes.script("opencode", [{"out": "não deveria rodar"}])
    fakes.script("codex", [{"out": "fiz no lugar"}])
    assert orch.main(["--config", str(config_path), "--repo", str(repo), "--isolation", "none", "Tarefa"]) == 0
    log = last_run_log(isolated_cache)
    assert fakes.calls("opencode") == []
    assert log["steps"][0]["agent"] == "codex"
    assert any("opencode indisponível" in w and "OPENROUTER_API_KEY" in w for w in log["warnings"])


def test_agente_opcional_ausente_nao_impede_a_execucao(config_path) -> None:
    cfg = json.loads(config_path.read_text())
    cfg["agents"]["opencode"]["direct"]["cmd"] = ["nao-existe-xyz"]
    assert orch.check_binaries(cfg, "direct") == []
