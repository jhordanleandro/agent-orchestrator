# Orquestrador de Agentes (extensão)

Painel para o `orchestrator.py`: codex supervisiona e implementa (agy só quando pedido) e
claude revisa, com cada ciclo, decisão, verificação e saída dos agentes ao vivo.

Abra pelo ícone **Orquestrador** na barra lateral, ou pelo comando
**Orquestrador: Executar tarefa…**. Para um projeto novo, rode antes
**Orquestrador: Configurar projeto** (o `--init`: git, 1º commit e `.orchestrator.json`
com a stack detectada; sem pasta aberta, ele pergunta qual). Configurações em `agentOrchestrator.*`
(inclusive `templates`, os modelos de tarefa do painel).

## Desenvolvimento

```sh
npm run dev:link   # uma vez: a extensão instalada vira link para esta pasta
npm run dev        # recompila src/ a cada mudança
npm run release    # volta a instalar uma cópia empacotada (.vsix)
```

Em modo desenvolvimento, mudanças em `media/` recarregam o painel sozinhas e
mudanças em `src/` (após compilar) mostram o aviso **Recarregar janela**.
