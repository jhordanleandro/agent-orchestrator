# agent-orchestrator

Uma instrução em linguagem natural → `codex` (só leitura) supervisiona e delega →
`codex` implementa frontend, backend e testes (o `agy` só quando você pedir) →
`claude` só revisa no fim, atrás de um filtro local.

```
python3 orchestrator.py "Minha necessidade técnica aqui"
```

Só biblioteca padrão (Python 3.9+). Todas as flags das CLIs ficam em
`orchestrator.json`; se uma CLI mudar, ajuste o JSON.

## Arquivos

| Arquivo | O quê |
|---|---|
| `orchestrator.py` | laço supervisor → agente, parser de JSON, filtro pré-claude, logs |
| `orchestrator.json` | prompts de sistema, comandos de cada CLI, timeouts, limites |
| `tests/` | testes (pytest) com agentes falsos — não chamam nenhum modelo |
| `~/.cache/agent-orchestrator/runs/*.json` | log completo de cada execução (prompts e saídas) |

## Permissões de cada agente (padrão do JSON)

| Papel | Comando | Pode alterar arquivos? |
|---|---|---|
| supervisor | `codex exec -s read-only -C <repo> -` | não (só lê e decide) |
| supervisor reserva | `agy --mode plan -p …` (timeout 180 s) | não |
| agy (só quando pedido) | `agy --mode accept-edits -p …` | sim, edições |
| opencode (alternativo, opcional) | `opencode run --format json -m openrouter/… -f <prompt>` | sim; rede, push e pastas externas bloqueados |
| codex (backend) | `codex exec -s workspace-write -C <repo> -` | sim, só dentro do repo (sandbox) |
| claude (revisão) | `claude -p --allowedTools Read,Grep,Glob,Bash(git diff:*)…` | não |

Nenhum agente roda com `--dangerously-*` / `yolo`. Em modo não interativo, o
que exigir aprovação é negado, e não fica esperando.
**Rode sempre num repositório git com a árvore limpa**: assim `git diff` mostra
exatamente o que os agentes mudaram e `git restore .` desfaz tudo.

## Isolamento por worktree (padrão)

Cada execução cria a branch `orch/AAAAMMDD-HHMMSS` numa worktree em
`~/.cache/agent-orchestrator/worktrees/`. Os agentes só mexem lá; seu
repositório e sua árvore de trabalho não são tocados.

- No fim, o trabalho é commitado nessa branch (commit interno, sem hooks).
- **Aceitar:** `orchestrator.py --accept orch/… --repo .` faz `git merge --squash`.
  As alterações ficam **em stage, sem commit**, para você revisar e commitar.
  Antes, um `git merge-tree` de teste garante que não há conflito; se houver, nada é tocado.
- **Descartar:** `orchestrator.py --discard orch/… --repo .` apaga a worktree e a branch.
- Sem alterações, a worktree é removida sozinha.
- A worktree parte do **último commit**: alterações não commitadas não entram (há um aviso).
- Arquivos fora do git (como `.env`) **não existem** na worktree, a menos que estejam em `copy_files`.
- `--isolation none` (ou `"isolation": "none"`) volta ao modo antigo, direto na pasta.

## Projeto do zero (`--init`)

```
python3 orchestrator.py --init --repo ~/projetos/meu-app
```

- Pasta inexistente ou sem git: cria a pasta e roda `git init -b main`.
- Detecta a stack pela raiz, sem rodar nada:
  - **Node:** gerenciador pelo lockfile (pnpm, yarn, bun, npm), setup com instalação
    travada, verificações pelos scripts `typecheck`/`lint`/`test` (ou `tsc --noEmit` se
    houver `tsconfig.json`), versão do Node por `.nvmrc`/`.node-version`/`engines` e
    `CI=1`. Com Prisma: `prisma generate` no setup, `DATABASE_URL` bloqueada e
    comandos de banco proibidos.
  - **Python:** venv (ou `uv sync`) e `pytest`/`ruff`/`mypy` se aparecerem no projeto.
  - **Nada reconhecido** (projeto do zero): sem verificações e com o modelo
    *Estrutura inicial do projeto* em primeiro lugar.
- Cria `.gitignore` (segredos, `node_modules`, `.venv`, saídas de build como `.next/` e `*.tsbuildinfo`) se não houver.
- Repositório sem commits: faz o 1º commit **só** com `.orchestrator.json` e
  `.gitignore` — a worktree isolada parte do HEAD. Outros arquivos da pasta
  ficam de fora, com aviso.
- Já existe `.orchestrator.json`: recusa. `--init --force` detecta de novo `setup`,
  `checks` e `node` e mantém regras, variáveis, padrões proibidos e modelos.

Fluxo para começar do zero: `--init` → tarefa com o modelo *Estrutura inicial* →
aceitar e commitar → `--init --force` (agora com testes/lint nas verificações) →
tarefas normais. O `--doctor` avisa quando o projeto ganhou testes que ainda não
estão nas verificações. Na extensão: **Orquestrador: Configurar projeto**.

## Configuração por projeto (`.orchestrator.json` na raiz do repositório)

```json
{
  "isolation": "worktree",
  "node": "24",
  "max_loops": 6,
  "setup": ["pnpm install --frozen-lockfile --prefer-offline"],
  "checks": ["pnpm typecheck", "pnpm lint", "pnpm test"],
  "env": { "DATABASE_URL": "mysql://bloqueado@127.0.0.1:1/x", "RESEND_API_KEY": null },
  "copy_files": [],
  "rules": "Regras do projeto, incluídas no prompt de todos os agentes.",
  "forbidden_patterns": ["prisma\\s+(db\\s+push|migrate)", "git\\s+push"],
  "on_forbidden": "abort"
}
```

| Campo | Efeito |
|---|---|
| `setup` | comandos rodados na worktree nova, antes do 1º ciclo (falhou → para) |
| `checks` | somados ao `--check`; precisam passar antes do claude |
| `env` | variáveis para agentes, setup e checks; `null` remove a variável |
| `node` | versão do Node (via `fnm`) posta na frente do PATH de agentes, setup e checks; o `--doctor` confere |
| `copy_files` | arquivos copiados do repositório para a worktree (cuidado com segredos) |
| `rules` | texto obrigatório em todos os prompts |
| `forbidden_patterns` | regex procuradas na saída dos agentes; `on_forbidden`: `warn` ou `abort` |
| `templates` | modelos de tarefa do projeto no painel: `name`, `task` e opcionais `maxLoops`, `approve`, `checks` |

**Limite honesto:** `forbidden_patterns` **detecta** o comando na saída, não o
impede antes de rodar. A proteção real vem de a worktree não ter `.env` e de
`env` trocar as credenciais. Evite escrever o comando proibido literalmente em
`rules`: alguns agentes repetem o prompt na saída e isso dispararia o padrão.

## Opções

```
--repo DIR        repositório (padrão: pasta atual)
--backend mco     usa `mco run --providers <agente>` em vez dos binários (ou ORCH_BACKEND=mco)
--check "CMD"     verificação antes do claude, repetível (ex.: --check "pnpm test")
--no-gate         desliga o filtro pré-claude (gasta mais cota)
--max-loops N     padrão 6
--timeout S       timeout por agente (padrão 900 s; supervisor 300 s)
--dry-run         mostra a configuração resolvida e os binários ausentes
--isolation X     worktree (padrão) | none
--accept BRANCH   aplica em stage uma branch orch/… e remove a worktree
--discard BRANCH  descarta uma branch orch/… e a worktree
--resume LOG      continua uma execução salva (mesma worktree, histórico e notas)
--control stdin   aceita comandos JSON no stdin: note / approve / reject / stop
--approve MODO    none | first | all — pausa para aprovar (ou editar) o passo; exige --control
--doctor          checa CLIs, git, projeto, pasta das worktrees e cota do claude (não gasta cota)
--init [--force]  prepara o repositório e gera o .orchestrator.json pela stack (ver abaixo)
```

## Filtro pré-claude (economia de cota)

Quando o supervisor pede o `claude`:

1. **Limite por execução:** `claude_policy.max_calls_per_run` (padrão 2). Esgotou, encerra com DONE.
2. **Diagnóstico** (`claude_purpose: "diagnosis"`): exige que o codex já tenha tentado ≥ 2 vezes; senão, volta para o codex.
3. **Revisão** (`claude_purpose: "review"`):
   - `git status --porcelain` vazio → nada para revisar → volta para o codex;
   - cada comando de `quality_gate.commands` / `--check` roda; o primeiro que falhar
     manda a saída do erro para o **codex**, e o claude não é chamado.

## Atalho no shell

Adicione ao `~/.zshrc` (ou `~/.bashrc`):

```sh
# Orquestrador de agentes: task "descrição da tarefa"
task() {
  python3 "$HOME/tools/agent-orchestrator/orchestrator.py" "$@"
}
# Variante que já roda os testes do projeto antes da revisão do claude
taskt() {
  python3 "$HOME/tools/agent-orchestrator/orchestrator.py" --check "pnpm lint" --check "pnpm test" "$@"
}
```

Depois `source ~/.zshrc` e, dentro do repositório:

```sh
task "Adicionar endpoint GET /api/health com teste unitário"
ORCH_BACKEND=mco task "..."
```

## Exemplo comentado de fluxo

`taskt "Criar página /status em Next.js que consome um endpoint /api/status com uptime e versão, com teste"`

```
Ciclo 1/6
🧭 Supervisor (agy) decidindo…            # agy lê a tarefa e o repo em modo plan
🧭 Decisão: codex — o endpoint vem antes da página
🤖 codex em execução                       # cria src/app/api/status/route.ts + teste
✅ codex concluiu em 140s

Ciclo 2/6
🧭 Decisão: agy — backend pronto, falta a página
🤖 agy em execução                         # cria src/app/status/page.tsx consumindo /api/status
✅ agy concluiu em 95s

Ciclo 3/6
🧭 Decisão: claude — código pronto, pedir revisão (review)
🔎 Filtro pré-claude: pnpm lint            # ✅
🔎 Filtro pré-claude: pnpm test            # ❌ 1 teste falhou
💸 claude bloqueado para economizar cota → codex   # nenhuma cota do claude gasta
🤖 codex em execução — recebe a saída exata do teste que falhou e corrige
✅ codex concluiu em 60s

Ciclo 4/6
🧭 Decisão: claude — revisar
🔎 pnpm lint ✅  pnpm test ✅
🤖 claude em execução                      # só lê o diff, devolve apontamentos
   - route.ts:12 (média) versão lida de package.json a cada request; cachear

Ciclo 5/6
🧭 Decisão: codex — aplicar o apontamento da revisão
🤖 codex em execução
✅ codex concluiu

Ciclo 6/6
🧭 Decisão: DONE
🏁 Status: done · 5 passo(s) · claude usado 1x
```

Saída: exit code 0 em `done`; 1 em `max_loops`, `aborted` (supervisor sem
JSON após 2 tentativas) ou `interrupted` (Ctrl+C, que encerra o agente em
execução junto com os subprocessos dele).

## Limitações conhecidas

- Cada ciclo custa **duas** chamadas: supervisor + agente. O supervisor é barato (só leitura,
  prompt curto: ~25 mil tokens por decisão), bem menos que um passo de implementação.
- O agente não vê o histórico inteiro: recebe o prompt do supervisor, o objetivo geral e o fim da saída do passo anterior.
- O `agy` só aceita o prompt como argumento. Acima de `arg_prompt_limit_chars`, o prompt vai para um arquivo e o agente recebe a instrução de lê-lo.
- As saídas do `mco run --quiet` dependem da versão do mco; se o parser não achar o JSON do supervisor, teste com `--backend direct`.

## Notas, aprovação e retomada

- **Nota ao supervisor** (`{"cmd":"note","text":"…"}` no stdin, ou o campo de nota no painel):
  vale até o fim da execução e entra no prompt do supervisor e dos agentes com prioridade máxima.
- **Aprovação** (`--approve first|all`): antes de cada agente rodar, o orquestrador emite
  `awaiting_approval` e espera `approve` (com prompt editado, opcional), `reject` (o supervisor
  decide de novo, sabendo do motivo) ou `stop`.
- **Retomada** (`--resume LOG`): recupera passos, notas, contagens e a worktree (recria se a pasta
  sumiu e a branch existe). Ganha `--max-loops` ciclos novos, numerados a partir do último.
- O log de cada execução traz `stats`: chamadas e tempo por agente (inclusive o supervisor).

## Saída estruturada e cota do claude

Cada CLI roda no modo de saída JSON (`stream_format` no `orchestrator.json`):

| CLI | Flag | O que vira atividade |
|---|---|---|
| codex | `exec --json` | arquivo criado/alterado, comando com código de saída, mensagens, tokens |
| claude | `-p --output-format stream-json --verbose` | ferramentas usadas, edições, resposta final, tokens, custo e **cota** |
| agy | `--output-format stream-json` | ferramentas, arquivos, comandos, tokens, ações negadas |

A cada chamada, o claude informa o uso das janelas de **5 horas** e **semanal**.
O orquestrador guarda o último valor em `~/.cache/agent-orchestrator/claude-limits.json`
e, antes de chamar o claude, compara com `claude_policy`:

```json
"claude_policy": { "max_weekly_utilization": 0.9, "max_five_hour_utilization": 0.95 }
```

Acima do teto, a revisão é pulada (vale mesmo com `--no-gate`).

**agy em modo não interativo não roda comandos de terminal:** a permissão é negada e a
resposta volta vazia. Os prompts do supervisor e do agy pedem só leitura e edição de
arquivos. Para liberar comandos específicos, adicione regras em
`~/.gemini/antigravity-cli/settings.json` → `permissions.allow` (ex.: `command(git status)`).

## Diff por passo

Antes do primeiro ciclo e depois de cada agente, o orquestrador tira um retrato dos
arquivos (`git add -A` + `git write-tree` num **índice temporário**: não commita, não
mexe no stage nem na branch; respeita o `.gitignore`). Cada passo do log guarda
`tree_before`, `tree_after` e `files` (status, caminho, +linhas/−linhas).

- O supervisor recebe no histórico a lista de arquivos alterados em cada passo.
- No painel, cada passo mostra os arquivos com +/−; o clique abre o diff do arquivo
  naquele passo e **ver diff do passo** abre todos no editor de várias alterações.
- Os retratos são objetos soltos do git: o `git gc` os remove depois de ~2 semanas,
  então diffs de execuções antigas podem deixar de abrir (o log continua legível).

## Modelos de tarefa por projeto

No `.orchestrator.json`:

```json
"templates": [
  { "name": "Corrigir bug com teste", "task": "Corrigir o bug: <sintoma>.\nPronto quando: …", "maxLoops": 5 },
  { "name": "Revisão de segurança", "task": "Revisar <rota> …", "maxLoops": 4, "approve": "first" }
]
```

O painel mostra primeiro os modelos **deste projeto** e depois os **gerais**
(`agentOrchestrator.templates`). Escolher um modelo preenche a tarefa, os ciclos e,
se definidos, a aprovação e as verificações; o primeiro `<marcador>` do texto já
vem selecionado para você digitar por cima. A lista se atualiza sozinha quando o
arquivo muda ou quando você troca de projeto no editor.

## Supervisor reserva

Se o supervisor principal (codex) devolver resposta sem JSON, o `supervisor_fallback`
(padrão: `agy --mode plan`) decide aquele ciclo já na 1ª falha, com tempo-limite
próprio (`timeout`, padrão 180 s). `supervisor_retries` (padrão 0) dá novas chances
ao principal antes disso. Remova `supervisor_fallback` para desligar o reserva.

Os papéis eram o contrário até 20/10/2026. Na 2ª execução real o agy levou ~5 min
por decisão (e 501 s para responder "ok" fora do orquestrador): o log dele mostra
`stream idle timeout (5m0s)` — o backend do Gemini para a resposta e o agy só tenta
de novo depois de 5 min. O codex decidiu o mesmo ciclo em 11 s. O prompt do
supervisor já leva `git status --short`, `git diff --stat` e se as verificações
passaram no código atual, então nenhum dos dois precisa rodar comandos.

## Agente alternativo: opencode com modelo aberto

O `opencode` usa um modelo aberto pelo OpenRouter (padrão `openrouter/qwen/qwen3-coder`;
troque no `-m` do `orchestrator.json`). O supervisor o chama quando o codex falha duas
vezes seguidas no mesmo problema, ou quando a tarefa/uma nota pede o opencode.

1. Crie a chave em openrouter.ai (em *Settings → Privacy*, desligue os provedores que
   treinam com seus dados — o código vai para o provedor do modelo).
2. Salve-a fora de qualquer repositório, sem ecoar no terminal:
   ```
   mkdir -p ~/.config/agent-orchestrator && read -rs "k?Chave OpenRouter: " && printf 'OPENROUTER_API_KEY=%s\n' "$k" > ~/.config/agent-orchestrator/secrets.env && chmod 600 ~/.config/agent-orchestrator/secrets.env && unset k
   ```
3. `--doctor` confere o binário e a presença da chave (nunca o valor).

- **Segredos:** `secrets.env` (CHAVE=valor) é lido pelo orquestrador; cada agente
  recebe só as chaves listadas em `env_from_secrets`. Nada disso vai para log ou evento.
- **Permissões:** a cada execução o orquestrador grava a configuração do opencode
  (`config_file` → `OPENCODE_CONFIG`): edição liberada; `webfetch`, pastas fora do
  projeto, `git push`, `curl`/`wget`, `ssh` e afins bloqueados. Nunca usa `--auto`.
  Atenção: sem essa configuração, o `opencode run` executa comandos sem perguntar.
- **Prompt:** vai anexado com `-f` (lido pela CLI) — o agente não lê pastas externas.
- **Opcional:** sem o binário ou sem a chave, o passo vai para o codex com um aviso, e
  a execução não falha. O painel mostra arquivos, comandos, tokens e custo do opencode.

## 2ª revisão depois de AJUSTES

Se a revisão do claude termina com `VEREDITO: AJUSTES` e restam menos de
`review_followup_cycles` ciclos (padrão 2), o limite é estendido **uma vez** para
caber "corrigir" + "revisar de novo" — desde que ainda haja chamadas ao claude
(`claude_policy.max_calls_per_run`). O supervisor pede a 2ª revisão em vez de
encerrar logo após a correção. O painel mostra o aviso da extensão; `0` desliga.

Motivo: no teste da calculadora (Next.js), com 4 ciclos, implementar → revisar →
corrigir esgotou o limite e o código corrigido saiu sem revisão — o claude tinha
achado um bug real de ponto flutuante, e a correção só foi conferida à mão.

## Fim automático após a revisão

O claude termina a revisão com `VEREDITO: APROVADO` ou `VEREDITO: AJUSTES`
(apontamentos fora do escopo vão numa seção à parte e não impedem o APROVADO). Se
for APROVADO **e** o código é o mesmo em que as `checks` acabaram de passar, a
execução termina ali, sem perguntar ao supervisor — antes ele às vezes mandava o
codex "validar tudo de novo" num ciclo inteiro a mais.

## MCP globais desligados nos agentes

MCP configurados globalmente (ex.: os da Hostinger) deixam cada chamada lenta e dão
ao agente acesso à produção. No codex, o item `"{codex_mcp_off}"` do comando vira
um `-c mcp_servers.<nome>.enabled=false` por servidor, só naquela execução; o
claude roda com `--strict-mcp-config` (nenhum MCP). O agy não tem opção por
execução: só `agy mcp disable <nome>`, que é global.

## Na extensão: fila, diagnóstico e notificações

- **＋ Fila:** enfileira a tarefa com as opções do formulário; as tarefas rodam uma por
  vez, cada uma na sua worktree. Parar uma execução pausa a fila (**Retomar fila**).
  A fila sobrevive a recarregar a janela.
- **Diagnóstico** (ou o comando *Orquestrador: Diagnóstico do ambiente*): o `--doctor` no painel.
- **Notificação do sistema** (`notify-send`) quando uma execução termina ou espera
  aprovação com o editor sem foco (`agentOrchestrator.systemNotifications`).

## Testes

```
python3 -m pytest -q
```

Rodam em ~10 s sem gastar cota: `tests/fake_agent.py` substitui agy, codex e
claude e segue um roteiro por teste (saída, arquivos que edita, exit code),
registrando o que recebeu. Logs e worktrees vão para uma pasta temporária,
nunca para o `~/.cache` real.

- `test_unit.py`: parser da decisão, veredito, streams das CLIs, `.orchestrator.json`,
  `run_command` (timeout mata o grupo, abort) e git (retratos, aceitar com e sem conflito).
- `test_init.py`: `--init` em pasta vazia, Node, Python, `--force` e a 1ª tarefa
  numa worktree de um repositório recém-criado.
- `test_run.py`: execuções completas via `main()` — fim automático após APROVADO,
  AJUSTES até o limite, supervisor reserva, filtro barrando o claude, padrão proibido,
  flags de MCP do codex, worktree + aceitar, retomada, aprovação/recusa e Node do projeto
  (pulado se o `fnm` não tiver o Node 24).

## Licença

Distribuído sob a [licença MIT](LICENSE).
