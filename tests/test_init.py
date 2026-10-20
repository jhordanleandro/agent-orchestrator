"""--init: preparar um repositório do zero e detectar a stack."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import decision, git, last_run_log, orch


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # O 1º commit usa a identidade do usuário; nos testes, uma fixa.
    for var, value in (("GIT_AUTHOR_NAME", "Teste"), ("GIT_AUTHOR_EMAIL", "teste@localhost"),
                       ("GIT_COMMITTER_NAME", "Teste"), ("GIT_COMMITTER_EMAIL", "teste@localhost")):
        monkeypatch.setenv(var, value)


def _cfg(root: Path) -> dict:
    return json.loads((root / ".orchestrator.json").read_text(encoding="utf-8"))


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")


def test_pasta_vazia_vira_repositorio_pronto(tmp_path: Path) -> None:
    root = tmp_path / "novo"
    result = orch.init_project(root)

    assert result["ok"] and result["created_repo"] and result["committed"] and result["gitignore"]
    assert result["stack"] is None
    cfg = _cfg(root)
    assert cfg["isolation"] == "worktree" and cfg["checks"] == [] and cfg["setup"] == []
    assert cfg["templates"][0]["name"] == "Estrutura inicial do projeto"
    assert any(r"git\s+push" == p for p in cfg["forbidden_patterns"])
    ignored = (root / ".gitignore").read_text()
    assert ".env" in ignored and ".next/" in ignored and "*.tsbuildinfo" in ignored
    # O 1º commit leva só os arquivos do orquestrador e deixa a árvore limpa.
    assert git(root, "ls-tree", "--name-only", "HEAD").split() == [".gitignore", ".orchestrator.json"]
    assert git(root, "status", "--porcelain") == ""
    assert any("Sem verificações" in w for w in result["warnings"])
    # A configuração gerada passa na validação do próprio orquestrador.
    assert orch.load_project_config(root)[0] == cfg


def test_nao_sobrescreve_sem_force(tmp_path: Path) -> None:
    orch.init_project(tmp_path)
    (tmp_path / ".orchestrator.json").write_text('{"rules": "minha"}', encoding="utf-8")
    result = orch.init_project(tmp_path)
    assert not result["ok"] and "--force" in result["message"]
    assert _cfg(tmp_path) == {"rules": "minha"}


def test_node_com_pnpm_prisma_e_versao(tmp_path: Path) -> None:
    _write(tmp_path, {
        "package.json": json.dumps({
            "scripts": {"typecheck": "tsc --noEmit", "lint": "eslint .", "test": "vitest run", "dev": "next dev"},
            "dependencies": {"@prisma/client": "6"}, "devDependencies": {"prisma": "6"},
        }),
        "pnpm-lock.yaml": "", "tsconfig.json": "{}", ".nvmrc": "v24.1.0\n",
    })
    result = orch.init_project(tmp_path)
    cfg = _cfg(tmp_path)
    assert result["stack"] == "node" and cfg["node"] == "24"
    assert cfg["setup"] == ["pnpm install --frozen-lockfile --prefer-offline", "pnpm exec prisma generate"]
    assert cfg["checks"] == ["pnpm typecheck", "pnpm lint", "pnpm test"]
    assert cfg["env"]["CI"] == "1" and "bloqueado" in cfg["env"]["DATABASE_URL"]
    assert orch.PRISMA_FORBIDDEN[0] in cfg["forbidden_patterns"]
    assert all(t["name"] != "Estrutura inicial do projeto" for t in cfg["templates"])


def test_node_npm_sem_lock_ignora_teste_padrao_do_npm_init(tmp_path: Path) -> None:
    _write(tmp_path, {
        "package.json": json.dumps({
            "scripts": {"test": 'echo "Error: no test specified" && exit 1'}, "engines": {"node": ">=22"},
        }),
        "tsconfig.json": "{}",
    })
    orch.init_project(tmp_path)
    cfg = _cfg(tmp_path)
    assert cfg["setup"] == ["npm install"]
    assert cfg["checks"] == ["npx tsc --noEmit"]
    assert cfg["node"] == "22"


def test_python_com_pytest_e_ruff(tmp_path: Path) -> None:
    _write(tmp_path, {
        "pyproject.toml": '[project]\nname = "x"\n[project.optional-dependencies]\ndev = ["pytest", "ruff"]\n',
        "tests/test_x.py": "",
    })
    orch.init_project(tmp_path)
    cfg = _cfg(tmp_path)
    assert cfg["setup"] == ["python3 -m venv .venv", '.venv/bin/pip install -q -e ".[dev]"']
    assert cfg["checks"] == [".venv/bin/python -m pytest -q", ".venv/bin/python -m ruff check ."]
    assert "node" not in cfg


def test_force_detecta_de_novo_e_mantem_o_que_foi_editado(tmp_path: Path) -> None:
    orch.init_project(tmp_path)
    cfg = _cfg(tmp_path)
    cfg["rules"] = "Regras minhas."
    cfg["templates"].append({"name": "Meu modelo", "task": "x"})
    cfg["env"] = {"API_KEY": None}
    (tmp_path / ".orchestrator.json").write_text(json.dumps(cfg), encoding="utf-8")
    # Os agentes criaram a estrutura (modelo "Estrutura inicial").
    _write(tmp_path, {"package.json": json.dumps({"scripts": {"test": "vitest run"}}), "package-lock.json": "{}"})

    result = orch.init_project(tmp_path, force=True)
    new = _cfg(tmp_path)
    assert result["ok"] and result["message"].startswith("Atualizado")
    assert new["checks"] == ["npm run test"] and new["setup"] == ["npm ci"]
    assert new["rules"] == "Regras minhas."
    assert new["env"] == {"CI": "1", "API_KEY": None}
    assert [t["name"] for t in new["templates"]][-1] == "Meu modelo"
    assert not result["committed"]  # o repositório já tinha commit


def test_arquivos_soltos_ficam_fora_do_primeiro_commit(tmp_path: Path) -> None:
    _write(tmp_path, {"notas.txt": "rascunho", ".env": "SEGREDO=1"})
    result = orch.init_project(tmp_path)
    assert result["committed"]
    assert git(tmp_path, "ls-tree", "--name-only", "HEAD").split() == [".gitignore", ".orchestrator.json"]
    assert any("notas.txt" in w for w in result["warnings"])
    # O .env fica ignorado pelo .gitignore criado.
    assert ".env" not in git(tmp_path, "status", "--porcelain")


def test_cli_init_e_primeira_tarefa_na_worktree(tmp_path: Path, config_path, fakes, isolated_cache) -> None:
    root = tmp_path / "do-zero"
    assert orch.main(["--init", "--repo", str(root)]) == 0
    assert orch.main(["--init", "--repo", str(root)]) == 1  # já existe

    fakes.script("supervisor", [decision("codex", "crie a estrutura"), decision("DONE", summary="estrutura criada")])
    fakes.script("codex", [{"write": {"package.json": "{}\n", "README.md": "# Novo\n"}}])
    assert orch.main(["--config", str(config_path), "--repo", str(root), "Estrutura inicial"]) == 0

    iso = last_run_log(isolated_cache)["isolation"]
    assert iso["pending"] and fakes.calls("codex")[0]["cwd"] == iso["worktree"]
    assert orch.main(["--repo", str(root), "--accept", iso["branch"]]) == 0
    assert sorted(git(root, "diff", "--cached", "--name-only").split()) == ["README.md", "package.json"]


def test_force_exige_init() -> None:
    with pytest.raises(SystemExit):
        orch.parse_args(["--force", "tarefa"])
