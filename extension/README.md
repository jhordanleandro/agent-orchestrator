# Orquestrador de Agentes (extensão)

Painel para o `orchestrator.py`: agy supervisiona, codex implementa e claude
revisa, com cada ciclo, decisão, verificação e saída dos agentes ao vivo.

Abra pelo ícone **Orquestrador** na barra lateral, ou pelo comando
**Orquestrador: Executar tarefa…**. Configurações em `agentOrchestrator.*`
(inclusive `templates`, os modelos de tarefa do painel).

## Desenvolvimento

```sh
npm run dev:link   # uma vez: a extensão instalada vira link para esta pasta
npm run dev        # recompila src/ a cada mudança
npm run release    # volta a instalar uma cópia empacotada (.vsix)
```

Em modo desenvolvimento, mudanças em `media/` recarregam o painel sozinhas e
mudanças em `src/` (após compilar) mostram o aviso **Recarregar janela**.
