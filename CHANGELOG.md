# Changelog

A versão é a da extensão (`extension/package.json`); o `orchestrator.py` anda junto.

## 0.3.0 — 20/10/2026

Fechada depois de duas execuções reais no prof-jhordan-hub
(28 min → 17 min, 7 → 3 chamadas ao supervisor, sem ciclo redundante).

### Mudou
- **codex é o supervisor** (`codex exec -s read-only`, ~11 s por decisão); o agy
  passou a reserva, com `timeout` próprio (180 s). O backend do Gemini às vezes
  para a resposta por 5 min antes de o agy tentar de novo (`stream idle timeout`).
- Fim automático: revisão do claude com `VEREDITO: APROVADO` sobre o mesmo código
  em que as verificações passaram encerra a execução sem perguntar ao supervisor.
- O supervisor recebe `git status`, `git diff --stat` e o estado das verificações
  prontos, em vez de tentar rodar comandos.
- O claude é avisado de que as verificações já passaram e não tenta rodá-las.

### Novo
- Campo `node` no `.orchestrator.json` (resolvido via `fnm`, conferido no `--doctor`).
- MCP globais desligados por execução: `{codex_mcp_off}` no codex e
  `--strict-mcp-config` no claude.
- Suíte de testes (`python3 -m pytest -q`): 59 testes com agentes falsos, sem cota.

### Corrigido
- Duas execuções no mesmo segundo sobrescreviam o log uma da outra e colidiam na
  branch `orch/…`: o carimbo ganhou milissegundos.

## 0.2.0 — 19/10/2026

Worktree isolada com aceitar/descartar, `.orchestrator.json` por projeto
(verificações, regras, variáveis, padrões proibidos, modelos de tarefa), saída
estruturada das três CLIs, diff por passo, notas e aprovação durante a execução,
retomada, fila de tarefas, notificações do sistema e `--doctor`.

## 0.1.0 — 19/10/2026

Primeira versão: laço supervisor → agente, filtro pré-claude com teto de cota e
painel ao vivo no Antigravity.
