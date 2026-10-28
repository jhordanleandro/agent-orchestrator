# Changelog

A versão é a da extensão (`extension/package.json`); o `orchestrator.py` anda junto.

## Não lançado

### Novo
- `--init` (e o comando **Orquestrador: Configurar projeto**): prepara um repositório
  do zero (git init, `.gitignore`, 1º commit só com os arquivos do orquestrador) e gera
  o `.orchestrator.json` pela stack detectada (Node/pnpm/npm/yarn/bun, Prisma, Python).
  `--init --force` detecta de novo e mantém o que você editou; o `--doctor` avisa
  quando o projeto ganhou testes que não estão nas verificações.

### Novo
- Agente alternativo **opencode** com modelo aberto via OpenRouter (padrão
  `qwen/qwen3-coder`): entra quando o codex falha duas vezes ou quando pedido.
  Chave em `~/.config/agent-orchestrator/secrets.env`, entregue só a ele; permissões
  próprias por execução (rede, push e pastas externas bloqueados); prompt anexado;
  opcional (sem binário/chave, o passo vai para o codex). `--doctor` confere a chave.
- `review_followup_cycles` (padrão 2): revisão com AJUSTES perto do fim estende o
  limite uma vez para corrigir e revisar de novo; o supervisor pede a 2ª revisão em
  vez de encerrar após a correção. Evento `cycles_extended`, mostrado no painel.

### Mudou
- O codex é o agente padrão também no frontend; o agy só entra quando a tarefa ou
  uma nota pedir. No teste de projeto do zero (Next.js), o agy estourou os 15 min
  duas vezes (o Gemini levava 2–4 min por resposta) e o codex fez tudo em menos de 5.

### Corrigido
- O painel só despacha eventos de tipos conhecidos: um `type` como `constructor`
  vindo do processo não chama mais um método herdado do objeto.
- O `.gitignore` do `--init` cobre saídas de build (`.next/`, `dist/`, `*.tsbuildinfo`…).
- O supervisor podia encerrar dizendo que o claude aprovou quando o VEREDITO
  tinha sido escrito pelo codex (3ª execução real, `claude_calls` = 0). O resumo
  final agora traz "Conferido pelo orquestrador" (revisão e verificações de fato),
  o campo `verification` no log e em `run_end`, e um aviso quando DONE sai sem
  verificações em dia e revisão aprovada.
- O agente que recebe o trabalho no lugar do claude é avisado de que não é o revisor.

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
