// Painel do Orquestrador: recebe os eventos do orchestrator.py (via extensão)
// e monta a linha do tempo de forma incremental — cada evento atualiza só o
// pedaço da tela que mudou, para a saída ao vivo não perder a rolagem.
(function () {
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");
  const MAX_LINES = 2000;
  const AGENT_LABEL = { supervisor: "supervisor", agy: "agy", codex: "codex", claude: "claude", opencode: "opencode", DONE: "DONE" };
  const STATUS_LABEL = {
    done: "Concluído", max_loops: "Limite de ciclos", aborted: "Abortado",
    interrupted: "Interrompido", crashed: "Falhou", running: "Em andamento",
    blocked: "Bloqueado (comando proibido)", setup_failed: "Falha na preparação", stopped: "Parado",
  };

  let running = false;
  let viewingHistory = null; // nome do arquivo quando mostra uma execução antiga
  let liveEvents = [];
  let ui = {};
  let cycles = new Map();
  let claudeCalls = 0;
  let templates = [];
  let projectName = null;
  let currentRepo = null; // repositório da execução mostrada (para abrir os diffs)
  let devMode = false;
  const RESUMABLE = ["max_loops", "interrupted", "stopped", "blocked", "crashed", "aborted"];
  const saved = vscode.getState() || {};

  // ── utilitários de DOM ──────────────────────────────────────────────────
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  const badge = (agent) => h("span", { class: `badge agent-${agent}` }, AGENT_LABEL[agent] || agent);
  const firstLine = (text) => String(text || "").split("\n")[0];
  function fmtSeconds(s) {
    s = Math.round(s);
    return s < 60 ? `${s}s` : `${Math.floor(s / 60)}min ${s % 60}s`;
  }
  function details(summary, body, open) {
    return h("details", { open: open || undefined }, h("summary", {}, summary), body);
  }
  function outputBox() {
    return h("pre", { class: "output" });
  }
  function appendLine(pre, line) {
    const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.append(document.createTextNode(line + "\n"));
    while (pre.childNodes.length > MAX_LINES) pre.removeChild(pre.firstChild);
    if (nearBottom) pre.scrollTop = pre.scrollHeight;
  }

  // ── estrutura fixa da tela ──────────────────────────────────────────────
  function buildShell(defaults) {
    const d = { ...defaults, ...(saved.options || {}) };
    ui.template = h("select", {});
    fillTemplates();
    ui.template.addEventListener("change", () => {
      const t = templates[Number(ui.template.value)];
      ui.template.value = "";
      if (t) applyTemplate(t);
    });
    ui.approve = h("select", {},
      h("option", { value: "none" }, "sem pausas"),
      h("option", { value: "first" }, "aprovar o 1º passo"),
      h("option", { value: "all" }, "aprovar cada passo"));
    ui.approve.value = d.approve || "none";
    ui.note = h("input", { type: "text", placeholder: "Nota para o supervisor (ex.: use o componente X)" });
    ui.note.addEventListener("keydown", (e) => { if (e.key === "Enter") sendNote(); });
    ui.noteBox = h("div", { class: "note-box hidden" }, ui.note, h("button", { onclick: sendNote }, "Enviar nota"));
    ui.task = h("textarea", { rows: 4, placeholder: "Descreva a tarefa para os agentes…" });
    ui.task.value = saved.task || "";
    ui.task.addEventListener("input", persist);
    ui.task.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) startRun();
    });
    ui.backend = h("select", {}, h("option", { value: "direct" }, "direct"), h("option", { value: "mco" }, "mco"));
    ui.backend.value = d.backend || "direct";
    ui.maxLoops = h("input", { type: "number", min: 1, max: 20, value: d.maxLoops || 6 });
    ui.checks = h("textarea", { rows: 2, placeholder: "Um comando por linha (somados aos do .orchestrator.json)" });
    ui.checks.value = (d.checks || []).join("\n");
    ui.isolation = h("select", {},
      h("option", { value: "" }, "padrão do projeto"),
      h("option", { value: "worktree" }, "worktree isolada"),
      h("option", { value: "none" }, "direto na pasta"));
    ui.isolation.value = d.isolation || "";
    for (const el of [ui.backend, ui.maxLoops, ui.checks, ui.isolation, ui.approve]) el.addEventListener("change", persist);

    ui.runBtn = h("button", { class: "primary", onclick: startRun }, "▶ Executar");
    ui.stopBtn = h("button", { class: "danger", onclick: () => vscode.postMessage({ type: "stop" }) }, "■ Parar");
    ui.logBtn = h("button", { class: "ghost", onclick: () => vscode.postMessage({ type: "showLog" }) }, "Log bruto");
    ui.queueBtn = h("button", { title: "Roda depois da execução atual (ou já, se nada estiver rodando)", onclick: enqueue }, "＋ Fila");
    ui.doctorBtn = h("button", { class: "ghost", onclick: () => { ui.doctor.classList.remove("hidden"); ui.doctor.replaceChildren(h("div", { class: "muted" }, "Checando o ambiente…")); vscode.postMessage({ type: "doctor" }); } }, "Diagnóstico");
    ui.queue = h("div", { class: "queue hidden" });
    ui.doctor = h("div", { class: "doctor hidden" });

    ui.status = h("div", { class: "status-line" });
    ui.limits = h("div", { class: "limits hidden" });
    ui.historyBanner = h("div", { class: "history-banner hidden" });
    ui.project = h("div", { class: "project hidden" });
    ui.notices = h("div", { class: "notices" });
    ui.timeline = h("div", { class: "timeline" });
    ui.result = h("div", { class: "result hidden" });
    ui.history = h("div", { class: "history-list" });
    ui.week = h("div", { class: "muted small week" });

    app.replaceChildren(
      devMode ? h("div", { class: "dev-badge" }, "🧪 modo desenvolvimento — o painel recarrega sozinho") : null,
      h("section", { class: "form" },
        ui.template,
        h("label", {}, "Tarefa", ui.task),
        h("div", { class: "row" },
          h("label", {}, "Backend", ui.backend),
          h("label", {}, "Máx. ciclos", ui.maxLoops),
        ),
        h("div", { class: "row" },
          h("label", {}, "Onde os agentes trabalham", ui.isolation),
          h("label", {}, "Aprovação", ui.approve),
        ),
        details("Verificações antes do claude", h("label", { class: "checks" }, ui.checks)),
        h("div", { class: "row buttons" }, ui.runBtn, ui.queueBtn, ui.stopBtn, ui.logBtn, ui.doctorBtn),
        h("div", { class: "hint" }, "Ctrl+Enter executa. O repositório é a pasta aberta no editor."),
      ),
      ui.noteBox,
      ui.doctor,
      ui.queue,
      ui.limits,
      ui.status,
      ui.historyBanner,
      ui.project,
      ui.notices,
      ui.timeline,
      ui.result,
      details(
        h("span", {}, "Execuções anteriores ",
          h("a", { href: "#", onclick: (e) => { e.preventDefault(); vscode.postMessage({ type: "loadHistory" }); } }, "(atualizar)")),
        ui.week,
        ui.history,
      ),
    );
    setRunning(running);
  }

  function fillTemplates() {
    if (!ui.template) return;
    const groups = [["projeto", projectName ? `Deste projeto (${projectName})` : "Deste projeto"], ["geral", "Gerais"]];
    const children = [h("option", { value: "" }, templates.length ? "— modelo de tarefa —" : "— sem modelos —")];
    for (const [source, label] of groups) {
      const items = templates.map((t, i) => [t, i]).filter(([t]) => (t.source || "geral") === source);
      if (items.length) children.push(h("optgroup", { label }, items.map(([t, i]) => h("option", { value: String(i) }, t.name))));
    }
    ui.template.replaceChildren(...children);
  }

  // Preenche o formulário e seleciona o primeiro <marcador> para digitar por cima.
  function applyTemplate(t) {
    ui.task.value = t.task;
    if (t.maxLoops) ui.maxLoops.value = t.maxLoops;
    if (t.approve) ui.approve.value = t.approve;
    if (Array.isArray(t.checks)) ui.checks.value = t.checks.join("\n");
    persist();
    ui.task.focus();
    const m = /<[^<>\n]+>/.exec(ui.task.value);
    if (m) ui.task.setSelectionRange(m.index, m.index + m[0].length);
  }

  function persist() {
    saved.task = ui.task.value;
    saved.options = readOptions();
    vscode.setState(saved);
  }

  function readOptions() {
    return {
      backend: ui.backend.value,
      maxLoops: Number(ui.maxLoops.value) || 6,
      checks: ui.checks.value.split("\n").map((s) => s.trim()).filter(Boolean),
      isolation: ui.isolation.value,
      approve: ui.approve.value,
    };
  }

  function startRun() {
    const task = ui.task.value.trim();
    if (!task || running) return;
    persist();
    showLive();
    vscode.postMessage({ type: "run", options: { task, ...readOptions() } });
  }

  function enqueue() {
    const task = ui.task.value.trim();
    if (!task) return;
    persist();
    vscode.postMessage({ type: "enqueue", options: { task, ...readOptions() } });
    ui.task.value = "";
    persist();
  }

  function renderQueue(items, paused) {
    if (!items.length) {
      ui.queue.classList.add("hidden");
      ui.queue.replaceChildren();
      return;
    }
    ui.queue.classList.remove("hidden");
    ui.queue.replaceChildren(
      h("div", { class: "queue-head" },
        h("strong", {}, `Fila (${items.length})`),
        paused
          ? h("span", {}, h("span", { class: "warn" }, " pausada — você parou a última execução "),
              h("button", { class: "ghost", onclick: () => vscode.postMessage({ type: "resumeQueue" }) }, "Retomar fila"))
          : h("span", { class: "muted small" }, " roda em seguida, uma por vez")),
      h("ol", { class: "queue-list" }, items.map((it) =>
        h("li", {},
          h("span", { class: "queue-task", title: it.task }, it.task.split("\n")[0]),
          h("span", { class: "muted small" }, ` · ${it.repo} · ${it.maxLoops} ciclos `),
          h("a", { href: "#", title: "Tirar da fila", onclick: (e) => { e.preventDefault(); vscode.postMessage({ type: "dequeue", id: it.id }); } }, "✖"),
        ))),
    );
  }

  function renderDoctor(checks) {
    const mark = (ok) => (ok === true ? h("span", { class: "ok" }, "✔") : ok === false ? h("span", { class: "fail" }, "✘") : h("span", { class: "warn" }, "•"));
    const failed = checks.filter((c) => c.ok === false).length;
    ui.doctor.classList.remove("hidden");
    ui.doctor.replaceChildren(
      h("div", { class: "queue-head" },
        h("strong", {}, failed ? `Diagnóstico: ${failed} problema(s)` : "Diagnóstico: tudo certo"),
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); ui.doctor.classList.add("hidden"); } }, " fechar")),
      h("ul", { class: "doctor-list" }, checks.map((c) =>
        h("li", {}, mark(c.ok), " ", h("strong", {}, c.name), ": ", c.detail,
          c.hint && c.ok !== true ? h("div", { class: "muted small" }, c.hint) : null))),
    );
  }

  function sendNote() {
    const text = ui.note.value.trim();
    if (!text || !running) return;
    vscode.postMessage({ type: "control", cmd: { cmd: "note", text } });
    ui.note.value = "";
  }

  function resumeRun(file) {
    if (running) return;
    persist();
    showLive();
    vscode.postMessage({ type: "resume", file, options: { task: "", ...readOptions() } });
  }

  function setRunning(value) {
    running = value;
    if (!ui.runBtn) return;
    ui.runBtn.disabled = value;
    ui.stopBtn.disabled = !value;
    ui.task.disabled = value;
    ui.noteBox.classList.toggle("hidden", !value);
    app.classList.toggle("is-running", value);
  }

  // ── linha do tempo ──────────────────────────────────────────────────────
  function resetTimeline() {
    cycles = new Map();
    claudeCalls = 0;
    ui.timeline.replaceChildren();
    ui.result.replaceChildren();
    ui.result.classList.add("hidden");
    ui.status.replaceChildren();
    ui.project.replaceChildren();
    ui.project.classList.add("hidden");
    ui.notices.replaceChildren();
    ui.setup = null;
  }

  function setStatus(text, kind) {
    ui.status.replaceChildren(h("span", { class: `pill ${kind || ""}` }, text));
    if (claudeCalls) ui.status.append(h("span", { class: "muted" }, ` · claude usado ${claudeCalls}x`));
  }

  function cycleCard(n) {
    if (cycles.has(n)) return cycles.get(n);
    const c = {
      card: h("div", { class: "cycle" }),
      supStatus: h("span", { class: "muted" }),
      supOut: outputBox(),
      decision: h("div", { class: "decision" }),
      gate: h("div", { class: "gate" }),
      agent: h("div", { class: "agent hidden" }),
      agentOut: outputBox(),
      approval: null,
      supActivity: h("ul", { class: "activity" }),
      agentActivity: h("ul", { class: "activity" }),
    };
    c.card.append(
      h("div", { class: "cycle-head" }, h("strong", {}, `Ciclo ${n}`), c.supStatus),
      c.supActivity,
      details("Saída do supervisor", c.supOut),
      c.decision,
      c.gate,
      c.agent,
    );
    ui.timeline.append(c.card);
    cycles.set(n, c);
    c.card.scrollIntoView({ block: "nearest" });
    return c;
  }

  function timer(start) {
    return h("span", { class: "timer", "data-start": String(start * 1000) }, "0s");
  }

  const handlers = {
    run_start(e) {
      resetTimeline();
      if (e.repo) currentRepo = e.repo;
      setStatus(`Em andamento · ${e.backend}`, "running");
      renderProject(e.project);
      if (e.claude_limits) renderLimits(e.claude_limits);
      if (e.resumed_from) {
        ui.notices.append(h("div", { class: "muted" }, `↻ Continuando a execução salva (${(e.previous_steps || []).length} passo(s) anteriores).`));
        for (const s of e.previous_steps || []) renderSavedStep(s, true);
        for (const n of e.notes || []) ui.notices.append(h("div", { class: "muted small" }, `💬 nota anterior: ${n}`));
      }
      if (e.approve && e.approve !== "none") {
        ui.notices.append(h("div", { class: "muted small" }, e.approve === "all" ? "✋ Cada passo espera sua aprovação." : "✋ O primeiro passo espera sua aprovação."));
      }
    },
    agent_activity(e) {
      const c = cycleCard(e.cycle);
      const list = e.agent === "supervisor" ? c.supActivity : c.agentActivity;
      // Comando concluído substitui a linha "rodando" do mesmo comando.
      if (e.kind === "command" && !e.running) {
        const pending = Array.from(list.querySelectorAll("li.running")).find((li) => li.getAttribute("data-cmd") === e.command);
        if (pending) pending.remove();
      }
      const icon = { file: "📝", command: "", tool: "🔧", warning: "⚠" }[e.kind] ?? "";
      const li = h("li", { class: `act act-${e.kind}${e.running ? " running" : ""}`, "data-cmd": e.command || null },
        icon ? `${icon} ` : "", e.kind === "file" && e.path ? h("code", {}, e.text) : e.text);
      if (e.tail && e.exit_code !== 0) li.append(h("pre", { class: "output small" }, e.tail));
      list.append(li);
      while (list.childNodes.length > 80) list.removeChild(list.firstChild);
    },
    claude_limits(e) {
      renderLimits(e.windows);
    },
    note_received(e) {
      ui.notices.append(h("div", { class: "note" }, `💬 Nota enviada ao supervisor: ${e.text}`));
    },
    awaiting_approval(e) {
      const c = cycleCard(e.cycle);
      const prompt = h("textarea", { rows: 6 });
      prompt.value = e.prompt;
      const reason = h("input", { type: "text", placeholder: "Motivo da recusa (opcional — vai para o supervisor)" });
      const send = (cmd) => {
        c.approval.querySelectorAll("button, textarea, input").forEach((el) => (el.disabled = true));
        vscode.postMessage({ type: "control", cmd });
      };
      c.approval = h("div", { class: "approval" },
        h("div", {}, "✋ Aprovar este passo? ", badge(e.agent), " ", h("span", { class: "muted" }, e.reason)),
        h("label", {}, "Prompt que será enviado (pode editar)", prompt),
        reason,
        h("div", { class: "row buttons" },
          h("button", { class: "primary", onclick: () => send({ cmd: "approve", prompt: prompt.value }) }, "✔ Aprovar"),
          h("button", { onclick: () => send({ cmd: "reject", reason: reason.value }) }, "↩ Recusar"),
          h("button", { class: "danger", onclick: () => send({ cmd: "stop" }) }, "■ Parar"),
        ));
      c.gate.append(c.approval);
      c.approval.scrollIntoView({ block: "nearest" });
      setStatus(`Ciclo ${e.cycle} · aguardando sua aprovação`, "running");
    },
    approval_result(e) {
      const c = cycleCard(e.cycle);
      if (!c.approval) return;
      const label = { approve: "✔ Aprovado", reject: "↩ Recusado — o supervisor vai decidir de novo", stop: "■ Parado" }[e.verdict] || e.verdict;
      c.approval.replaceChildren(h("div", { class: "muted" }, label));
      c.approval = null;
    },
    isolation_ready(e) {
      ui.project.classList.remove("hidden");
      ui.project.append(h("div", {}, "🌿 Trabalhando na branch ", h("code", {}, e.branch), " (worktree isolada — seu repositório não é tocado)"));
    },
    warning(e) {
      ui.notices.append(h("div", { class: "warn" }, `⚠ ${e.message}`));
    },
    forbidden(e) {
      const c = cycleCard(e.cycle);
      c.gate.append(h("div", { class: "error" },
        `🚫 ${e.agent} rodou algo que bate com /${e.pattern}/`, e.abort ? " — execução será interrompida" : "",
        h("pre", { class: "output small" }, e.line)));
    },
    cycle_start(e) {
      cycleCard(e.cycle);
      setStatus(`Ciclo ${e.cycle}/${e.max}`, "running");
    },
    cycles_extended(e) {
      ui.notices.append(h("div", { class: "info" },
        `➕ A revisão pediu ajustes: limite estendido para ${e.max} ciclos (corrigir e revisar de novo).`));
    },
    supervisor_start(e) {
      const c = cycleCard(e.cycle);
      c.supStatus.replaceChildren(
        h("span", { class: "spinner" }), " supervisor decidindo",
        e.attempt > 1 ? ` (tentativa ${e.attempt})` : "", " · ", timer(e.ts),
      );
    },
    supervisor_invalid(e) {
      const c = cycleCard(e.cycle);
      c.decision.append(h("div", { class: "warn" }, `⚠ Resposta sem JSON válido (tentativa ${e.attempt}).`));
    },
    decision(e) {
      const c = cycleCard(e.cycle);
      c.supStatus.replaceChildren(h("span", { class: "muted" }, "decidido"));
      c.decision.replaceChildren(
        h("div", { class: "decision-line" }, "🧭 ", badge(e.next_agent), " ", e.reason),
        e.next_agent === "claude" ? h("div", { class: "muted small" }, `finalidade: ${e.claude_purpose}`) : null,
      );
    },
    gate_check(e) {
      const c = e.phase === "setup" ? setupCard() : cycleCard(lastCycle());
      const row = h("div", { class: "gate-row", "data-cmd": e.command },
        h("span", { class: "spinner" }), " 🔎 ", h("code", {}, e.command));
      c.gate.append(row);
    },
    gate_result(e) {
      const c = e.phase === "setup" ? setupCard() : cycleCard(lastCycle());
      const rows = c.gate.querySelectorAll(".gate-row");
      const row = Array.from(rows).reverse().find((r) => r.getAttribute("data-cmd") === e.command);
      const content = [
        e.ok ? "✅ " : "❌ ", h("code", {}, e.command), h("span", { class: "muted" }, ` · ${fmtSeconds(e.seconds)}`),
      ];
      if (!e.ok && e.output) content.push(details("saída", h("pre", { class: "output small" }, e.output)));
      if (row) row.replaceChildren(...content);
    },
    gate_blocked(e) {
      const c = cycleCard(e.cycle);
      c.gate.append(h("div", { class: "warn" }, `💸 claude barrado → ${e.target}: ${firstLine(e.reason)}`));
    },
    agent_start(e) {
      const c = cycleCard(e.cycle);
      if (e.agent === "claude") claudeCalls += 1;
      c.agentStatus = h("span", {}, h("span", { class: "spinner" }), " rodando · ", timer(e.ts));
      c.agentOut.replaceChildren();
      c.agentActivity.replaceChildren();
      c.agent.classList.remove("hidden");
      c.agent.replaceChildren(
        h("div", { class: "agent-head" },
          "🤖 ", badge(e.agent),
          e.redirected_from ? h("span", { class: "muted" }, ` (no lugar de ${e.redirected_from})`) : null,
          " ", c.agentStatus),
        h("div", { class: "muted small" }, e.reason),
        details("Prompt enviado", h("pre", { class: "output small" }, e.prompt)),
        c.agentActivity,
        details("Saída ao vivo", c.agentOut, false),
      );
      setStatus(`Ciclo ${e.cycle} · ${e.agent} rodando`, "running");
    },
    agent_output(e) {
      const c = cycleCard(e.cycle);
      appendLine(e.agent === "supervisor" ? c.supOut : c.agentOut, e.stream === "stderr" ? `‖ ${e.line}` : e.line);
    },
    agent_end(e) {
      const c = cycleCard(e.cycle);
      const ok = e.code === 0 && !e.timed_out;
      const why = e.timed_out ? "tempo esgotado" : `exit ${e.code}`;
      if (c.agentStatus) {
        c.agentStatus.replaceChildren(
          h("span", { class: ok ? "ok" : "fail" }, ok ? "✅ concluído" : `❌ falhou (${why})`),
          h("span", { class: "muted" }, ` · ${fmtSeconds(e.seconds)}`),
        );
      }
      if (Array.isArray(e.files) && e.tree_before && e.tree_after) c.agent.append(stepFiles(e));
      else if (e.tree_after) c.agent.append(h("div", { class: "muted small" }, "Nenhum arquivo alterado neste passo."));
      if (e.output) c.agent.append(details("Resposta final", h("pre", { class: "output" }, e.output), !c.agentOut.childNodes.length));
    },
    run_end(e) {
      const kind = e.status === "done" ? "ok" : "fail";
      setStatus(STATUS_LABEL[e.status] || e.status, kind);
      ui.timeline.querySelectorAll(".spinner").forEach((s) => s.remove());
      const files = Array.isArray(e.changed_files) ? e.changed_files : [];
      const iso = e.isolation && e.isolation.pending ? e.isolation : null;
      ui.result.classList.remove("hidden");
      ui.result.replaceChildren(
        h("h3", {}, "🏁 ", STATUS_LABEL[e.status] || e.status),
        e.summary ? h("div", { class: "summary" }, e.summary) : null,
        (e.warnings || []).length
          ? h("div", { class: "warn small" }, (e.warnings || []).map((w) => h("div", {}, `⚠ ${w}`)))
          : null,
        files.length
          ? h("div", {},
              h("div", { class: "muted small" },
                iso ? `Alterações na branch ${iso.branch} (${files.length}) — clique para ver o diff:` : `Arquivos alterados no repositório (${files.length}):`),
              h("ul", { class: "files" }, files.map((f) =>
                h("li", {},
                  h("code", { class: "file-status" }, f.status),
                  h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); vscode.postMessage({ type: "openDiff", path: f.path, status: f.status, isolation: iso || undefined }); } }, f.path),
                ))))
          : h("div", { class: "muted small" }, "Nenhum arquivo alterado."),
        iso ? isolationActions(iso) : null,
        e.isolation && e.isolation.note ? h("div", { class: "muted small" }, e.isolation.note) : null,
        e.stats ? renderStats(e.stats) : null,
        RESUMABLE.includes(e.status) && e.log_path && !viewingHistory
          ? h("div", { class: "row buttons" }, h("button", { onclick: () => resumeRun(e.log_path) }, "↻ Continuar de onde parou"))
          : null,
        e.log_path ? h("div", { class: "muted small" }, `Log: ${e.log_path}`) : null,
      );
    },
    error(e) {
      ui.timeline.append(h("div", { class: "error" }, `⛔ ${e.message}`));
    },
  };

  function fmtTokens(n) {
    return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
  }

  function renderLimits(windows) {
    if (!ui.limits || !windows) return;
    const parts = [];
    for (const [name, label] of [["five_hour", "5h"], ["seven_day", "semana"]]) {
      const w = windows[name];
      if (!w) continue;
      const pct = Math.round(w.utilization * 100);
      const when = new Date(w.resets_at * 1000).toLocaleString("pt-BR", { weekday: "short", hour: "2-digit", minute: "2-digit" });
      parts.push(h("div", { class: "limit", title: `renova ${when}` },
        h("span", { class: "limit-label" }, `claude ${label}`),
        h("span", { class: "bar" }, h("span", { class: `fill ${pct >= 90 ? "hot" : pct >= 70 ? "warm" : ""}`, style: `width:${Math.min(100, pct)}%` })),
        h("span", { class: "limit-pct" }, `${pct}%`)));
    }
    ui.limits.replaceChildren(...parts);
    ui.limits.classList.toggle("hidden", !parts.length);
  }

  function stepFiles(e) {
    const repo = currentRepo;
    const files = e.files;
    if (!files.length) return h("div", { class: "muted small" }, "Nenhum arquivo alterado neste passo.");
    const total = (k) => files.reduce((n, f) => n + (f[k] || 0), 0);
    const open = (f) => vscode.postMessage({ type: "openStepDiff", repo, before: e.tree_before, after: e.tree_after, path: f.path, status: f.status });
    return h("div", { class: "step-files" },
      h("div", { class: "step-files-head" },
        h("span", {}, `📂 ${files.length} arquivo(s) neste passo `),
        h("span", { class: "plus" }, `+${total("added")}`), " ", h("span", { class: "minus" }, `−${total("deleted")}`),
        repo ? h("a", { href: "#", onclick: (ev) => {
          ev.preventDefault();
          vscode.postMessage({ type: "openStepChanges", repo, before: e.tree_before, after: e.tree_after,
            title: `Ciclo ${e.cycle} · ${e.agent}`, files: files.map((f) => ({ path: f.path, status: f.status })) });
        } }, " · ver diff do passo") : null),
      h("ul", { class: "files" }, files.map((f) =>
        h("li", {},
          h("code", { class: "file-status" }, f.status),
          repo ? h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); open(f); } }, f.path) : f.path,
          f.added == null ? h("span", { class: "muted small" }, " binário")
            : h("span", { class: "small" }, " ", h("span", { class: "plus" }, `+${f.added}`), " ", h("span", { class: "minus" }, `−${f.deleted}`)),
        ))));
  }

  function renderStats(stats) {
    const parts = Object.entries(stats).map(([agent, st]) =>
      h("span", { class: "stat" }, badge(agent), ` ${st.calls}× · ${fmtSeconds(st.seconds)}`,
        st.tokens ? ` · ${fmtTokens(st.tokens)} tokens` : "",
        st.cost_usd ? ` · US$ ${st.cost_usd.toFixed(2)}` : ""));
    return parts.length ? h("div", { class: "stats" }, h("span", { class: "muted small" }, "Uso: "), parts) : null;
  }

  function renderSavedStep(s, previous) {
    apply({ type: "cycle_start", cycle: s.cycle, max: s.cycle });
    apply({ type: "decision", cycle: s.cycle, next_agent: s.redirected_from || s.agent, reason: s.reason, claude_purpose: "review" });
    if (s.redirected_from) apply({ type: "gate_blocked", cycle: s.cycle, target: s.agent, reason: "filtro de qualidade local" });
    apply({ type: "agent_start", cycle: s.cycle, agent: s.agent, reason: s.reason, prompt: s.prompt, redirected_from: s.redirected_from, ts: 0 });
    apply({ type: "agent_end", cycle: s.cycle, agent: s.agent, code: s.code, timed_out: false, seconds: s.seconds, output: s.output,
      files: s.files, tree_before: s.tree_before, tree_after: s.tree_after });
    if (previous) cycles.get(s.cycle).card.classList.add("previous");
  }

  function setupCard() {
    if (!ui.setup) {
      ui.setup = { gate: h("div", { class: "gate" }) };
      ui.timeline.prepend(h("div", { class: "cycle setup" }, h("div", { class: "cycle-head" }, h("strong", {}, "Preparação da worktree")), ui.setup.gate));
    }
    return ui.setup;
  }

  function renderProject(project) {
    if (!project) return;
    const items = [];
    items.push(project.config
      ? h("div", {}, "⚙ ", h("code", {}, ".orchestrator.json"), " do projeto carregado")
      : h("div", { class: "muted" }, "Sem .orchestrator.json no projeto (só o padrão global)."));
    if (project.checks && project.checks.length) items.push(h("div", {}, `🔎 Verificações: ${project.checks.join(" · ")}`));
    if (project.env_overrides && project.env_overrides.length) items.push(h("div", {}, `🔒 Variáveis trocadas para os agentes: ${project.env_overrides.join(", ")}`));
    if (project.forbidden_patterns && project.forbidden_patterns.length) items.push(h("div", {}, `🚫 Padrões proibidos (${project.on_forbidden === "abort" ? "interrompe" : "avisa"}): ${project.forbidden_patterns.length}`));
    if (project.rules) items.push(h("div", {}, "📜 Regras do projeto incluídas nos prompts"));
    items.push(h("div", { class: "muted" }, project.isolation === "worktree" ? "Isolamento: worktree" : "Isolamento: nenhum — os agentes alteram a pasta aberta"));
    ui.project.replaceChildren(...items);
    ui.project.classList.remove("hidden");
  }

  function isolationActions(iso) {
    const box = h("div", { class: "iso-actions" });
    const send = (type) => {
      box.querySelectorAll("button").forEach((b) => (b.disabled = true));
      vscode.postMessage({ type, isolation: iso });
    };
    box.setAttribute("data-branch", iso.branch);
    box.append(
      h("div", { class: "muted small" }, "Nada foi aplicado no seu repositório ainda."),
      h("div", { class: "row buttons" },
        h("button", { class: "primary", onclick: () => send("accept") }, "✔ Aceitar (aplica em stage)"),
        h("button", { class: "danger", onclick: () => send("discard") }, "✖ Descartar"),
        h("button", { class: "ghost", onclick: () => vscode.postMessage({ type: "openWorktree", isolation: iso }) }, "Abrir worktree"),
      ),
    );
    return box;
  }

  function lastCycle() {
    let n = 0;
    for (const k of cycles.keys()) n = Math.max(n, k);
    return n || 1;
  }

  function apply(event) {
    const fn = handlers[event.type];
    if (fn) fn(event);
  }

  // ── histórico ───────────────────────────────────────────────────────────
  function renderHistory(items, week) {
    if (week) {
      ui.week.textContent = `Últimos 7 dias: ${week.runs} execução(ões) · claude chamado ${week.claudeCalls}×`;
    }
    if (!items.length) {
      ui.history.replaceChildren(h("div", { class: "muted small" }, "Nenhuma execução ainda."));
      return;
    }
    ui.history.replaceChildren(...items.map((it) =>
      h("a", { class: "history-item", href: "#", onclick: (e) => { e.preventDefault(); vscode.postMessage({ type: "openHistory", file: it.file }); } },
        h("span", { class: `pill tiny ${it.status === "done" ? "ok" : "fail"}` }, STATUS_LABEL[it.status] || it.status),
        h("span", { class: "muted" }, ` ${it.date} · ${it.steps} passo(s)`),
        h("div", { class: "history-task" }, it.task),
      )));
  }

  // Converte o log salvo (RunState) em eventos para reaproveitar a mesma tela.
  function showHistoryRun(file, run, changed) {
    viewingHistory = file;
    ui.historyBanner.classList.remove("hidden");
    ui.historyBanner.replaceChildren(
      h("span", {}, `Visualizando execução salva: ${run.task || file}`),
      h("button", { class: "ghost", onclick: showLive }, "Voltar à execução atual"),
    );
    resetTimeline();
    apply({ type: "run_start", backend: run.backend, repo: run.repo });
    for (const s of run.steps || []) renderSavedStep(s, false);
    apply({ type: "run_end", status: run.status, summary: run.final_summary, changed_files: changed || [],
      isolation: run.isolation, warnings: run.warnings, stats: run.stats });
    if (RESUMABLE.includes(run.status)) {
      ui.result.append(h("div", { class: "row buttons" },
        h("button", { class: "primary", disabled: running || undefined, onclick: () => resumeRun(file) }, "↻ Continuar esta execução")));
    }
    ui.task.value = run.task || ui.task.value;
  }

  function showLive() {
    viewingHistory = null;
    ui.historyBanner.classList.add("hidden");
    resetTimeline();
    liveEvents.forEach(apply);
    if (!liveEvents.length) ui.status.replaceChildren(h("span", { class: "muted" }, "Pronto para executar."));
  }

  // ── cronômetros ─────────────────────────────────────────────────────────
  setInterval(() => {
    document.querySelectorAll(".timer[data-start]").forEach((el) => {
      if (!el.closest(".cycle") || !el.isConnected) return;
      const start = Number(el.getAttribute("data-start"));
      if (start > 0) el.textContent = fmtSeconds((Date.now() - start) / 1000);
    });
  }, 1000);

  // ── mensagens da extensão ───────────────────────────────────────────────
  window.addEventListener("message", (msg) => {
    const data = msg.data;
    switch (data.type) {
      case "init":
        liveEvents = data.events || [];
        templates = data.templates || [];
        projectName = data.projectName || null;
        devMode = !!data.devMode;
        buildShell(data.defaults || {});
        setRunning(!!data.running);
        showLive();
        if (data.claudeLimits) renderLimits(data.claudeLimits);
        break;
      case "event":
        if (data.event.type === "run_start") liveEvents = [];
        liveEvents.push(data.event);
        if (!viewingHistory) apply(data.event);
        break;
      case "state":
        setRunning(data.running);
        break;
      case "history":
        renderHistory(data.items || [], data.week);
        break;
      case "queue":
        renderQueue(data.items || [], !!data.paused);
        break;
      case "doctor":
        renderDoctor(data.checks || []);
        break;
      case "templates":
        templates = data.templates || [];
        projectName = data.projectName || null;
        fillTemplates();
        break;
      case "historyRun":
        showHistoryRun(data.file, data.run, data.changed);
        break;
      case "isolationResolved": {
        const box = document.querySelector(`.iso-actions[data-branch="${CSS.escape(data.branch)}"]`);
        if (!box) break;
        if (data.ok) {
          box.replaceChildren(h("div", { class: "ok" }, data.action === "accept" ? "✔ " : "✖ ", data.message));
        } else {
          box.querySelectorAll("button").forEach((b) => (b.disabled = false));
          box.prepend(h("div", { class: "error" }, data.message));
        }
        break;
      }
    }
  });

  vscode.postMessage({ type: "ready" });
})();
