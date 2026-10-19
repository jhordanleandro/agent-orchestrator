// Extensão do Orquestrador de Agentes: roda o orchestrator.py com
// `--events jsonl` e mostra cada evento num painel lateral em tempo real.
import { ChildProcess, execFile, spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";

type OrchestratorEvent = { type: string; [key: string]: unknown };

type RunOptions = {
  task: string;
  backend: "direct" | "mco";
  maxLoops: number;
  checks: string[];
  isolation: "" | "worktree" | "none";
  approve: "none" | "first" | "all";
  resume?: string; // log de uma execução anterior para continuar
  repo?: string;
};

type Template = {
  name: string;
  task: string;
  maxLoops?: number;
  approve?: "none" | "first" | "all";
  checks?: string[];
  source?: "projeto" | "geral";
};

// Worktree criada pelo orquestrador para uma execução (ver --isolation).
type Isolation = {
  branch: string;
  worktree: string;
  base: string;
  source_repo: string;
  pending?: boolean;
};

type HistoryItem = {
  file: string;
  date: string;
  task: string;
  status: string;
  steps: number;
  claudeCalls: number;
  mtime: number;
};

// Mensagens que o painel (webview) manda para a extensão.
type WebviewMessage =
  | { type: "ready" }
  | { type: "run"; options: RunOptions }
  | { type: "stop" }
  | { type: "showLog" }
  | { type: "openDiff"; path: string; status: string; isolation?: Isolation }
  | { type: "accept"; isolation: Isolation }
  | { type: "discard"; isolation: Isolation }
  | { type: "openWorktree"; isolation: Isolation }
  | { type: "openFile"; path: string }
  | { type: "loadHistory" }
  | { type: "control"; cmd: Record<string, unknown> }
  | { type: "enqueue"; options: RunOptions }
  | { type: "dequeue"; id: string }
  | { type: "resumeQueue" }
  | { type: "doctor" }
  | { type: "openStepDiff"; repo: string; before: string; after: string; path: string; status: string }
  | { type: "openStepChanges"; repo: string; before: string; after: string; title: string; files: { path: string; status: string }[] }
  | { type: "resume"; file: string; options: RunOptions }
  | { type: "openHistory"; file: string };

const VIEW_ID = "agentOrchestrator.panel";
const GIT_SCHEME = "orch-git";
const STOP_GRACE_MS = 15_000;

function expandHome(p: string): string {
  return p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;
}

function runsDir(): string {
  const cache = process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache");
  return path.join(cache, "agent-orchestrator", "runs");
}

// O editor aberto pelo menu do sistema herda um PATH mínimo; as CLIs dos
// agentes (agy, codex, claude) costumam ficar em ~/.local/bin.
function childEnv(): NodeJS.ProcessEnv {
  const extra = [path.join(os.homedir(), ".local", "bin"), "/usr/local/bin"];
  const current = (process.env.PATH || "").split(path.delimiter);
  const merged = [...extra.filter((p) => !current.includes(p)), ...current];
  return { ...process.env, PATH: merged.join(path.delimiter), PYTHONUNBUFFERED: "1", NO_COLOR: "1" };
}

type ExecResult = { code: number; stdout: string; stderr: string };

function exec(cmd: string, args: string[], cwd: string): Promise<ExecResult> {
  return new Promise((resolve) => {
    execFile(cmd, args, { cwd, env: childEnv(), maxBuffer: 32 * 1024 * 1024 }, (err, stdout, stderr) => {
      const code = err ? (typeof (err as NodeJS.ErrnoException).code === "number" ? Number((err as NodeJS.ErrnoException).code) : 1) : 0;
      resolve({ code, stdout: String(stdout), stderr: String(stderr) });
    });
  });
}

// Conteúdo de um arquivo numa revisão (git show ref:path), para o diff
// "commit base ↔ branch do orquestrador". Arquivo ausente vira documento vazio.
class GitShowProvider implements vscode.TextDocumentContentProvider {
  async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
    const q = new URLSearchParams(uri.query);
    const res = await exec("git", ["show", `${q.get("ref")}:${q.get("path")}`], q.get("repo") || "/");
    return res.code === 0 ? res.stdout : "";
  }
}

function gitShowUri(repo: string, ref: string, file: string): vscode.Uri {
  const query = new URLSearchParams({ repo, ref, path: file }).toString();
  return vscode.Uri.from({ scheme: GIT_SCHEME, path: `/${ref.slice(0, 8)}/${file}`, query });
}

async function branchExists(repo: string, branch: string): Promise<boolean> {
  return (await exec("git", ["rev-parse", "--verify", "--quiet", `refs/heads/${branch}`], repo)).code === 0;
}

async function branchChanges(iso: Isolation): Promise<{ status: string; path: string }[]> {
  const res = await exec("git", ["diff", "--name-status", "--no-renames", iso.base, iso.branch], iso.source_repo);
  return res.stdout
    .split("\n")
    .map((line) => line.split("\t"))
    .filter((parts) => parts.length >= 2)
    .map((parts) => ({ status: parts[0].trim(), path: parts[parts.length - 1] }));
}

function scriptPath(): string {
  const cfg = vscode.workspace.getConfiguration("agentOrchestrator");
  return expandHome(cfg.get<string>("scriptPath", "~/tools/agent-orchestrator/orchestrator.py"));
}

function pickRepo(): string | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) return undefined;
  const active = vscode.window.activeTextEditor?.document.uri;
  if (active) {
    const owner = vscode.workspace.getWorkspaceFolder(active);
    if (owner) return owner.uri.fsPath;
  }
  return folders[0].uri.fsPath;
}

class OrchestratorRunner {
  private child: ChildProcess | undefined;
  private stopTimer: NodeJS.Timeout | undefined;
  private sawRunEnd = false;
  events: OrchestratorEvent[] = [];
  repo: string | undefined;

  constructor(
    private readonly output: vscode.OutputChannel,
    private readonly onEvent: (event: OrchestratorEvent) => void,
    private readonly onStateChange: (running: boolean) => void,
  ) {}

  get running(): boolean {
    return this.child !== undefined;
  }

  start(options: RunOptions): void {
    if (this.child) {
      vscode.window.showWarningMessage("Já existe uma orquestração em andamento.");
      return;
    }
    const repo = options.repo || pickRepo();
    if (!repo) {
      vscode.window.showErrorMessage("Abra uma pasta (repositório) antes de rodar o orquestrador.");
      return;
    }
    const cfg = vscode.workspace.getConfiguration("agentOrchestrator");
    const script = scriptPath();
    if (!fs.existsSync(script)) {
      vscode.window.showErrorMessage(`orchestrator.py não encontrado em ${script}. Ajuste agentOrchestrator.scriptPath.`);
      return;
    }

    const args = [
      script,
      "--events", "jsonl",
      "--control", "stdin",
      "--repo", repo,
      "--backend", options.backend,
      "--max-loops", String(Math.max(1, Math.floor(options.maxLoops))),
    ];
    if (options.approve && options.approve !== "none") args.push("--approve", options.approve);
    if (options.resume) args.push("--resume", options.resume);
    const configPath = cfg.get<string>("configPath", "").trim();
    if (configPath) args.push("--config", expandHome(configPath));
    for (const check of options.checks) args.push("--check", check);
    if (options.isolation) args.push("--isolation", options.isolation);
    if (!options.resume) args.push("--", options.task);

    this.events = [];
    this.repo = repo;
    this.sawRunEnd = false;
    this.output.appendLine(`\n=== ${new Date().toLocaleString()} · ${options.resume ? `retomando ${options.resume}` : options.task}`);

    const python = cfg.get<string>("pythonPath", "python3");
    const child = spawn(python, args, { cwd: repo, env: childEnv() });
    this.child = child;
    this.onStateChange(true);

    let buffer = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      buffer += chunk;
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        this.handleLine(buffer.slice(0, newline));
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf("\n");
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => this.output.append(chunk));

    child.on("error", (err) => {
      this.push({ type: "error", message: `Não foi possível iniciar ${python}: ${err.message}` });
    });
    child.on("close", (code, signal) => {
      if (buffer.trim()) this.handleLine(buffer);
      if (!this.sawRunEnd) {
        this.push({
          type: "run_end",
          status: "crashed",
          summary: `O orquestrador saiu sem concluir (código ${code ?? "?"}${signal ? `, sinal ${signal}` : ""}). Veja o log bruto.`,
          changed_files: [],
        });
      }
      if (this.stopTimer) clearTimeout(this.stopTimer);
      this.child = undefined;
      this.onStateChange(false);
    });
  }

  // Comandos para o orquestrador em execução (nota, aprovar, recusar, parar).
  send(cmd: Record<string, unknown>): void {
    if (this.child?.stdin?.writable) this.child.stdin.write(JSON.stringify(cmd) + "\n");
  }

  // O SIGTERM vira Ctrl+C no orquestrador, que encerra o agente em execução
  // e grava o log. Se não sair a tempo, força.
  stop(): void {
    const child = this.child;
    if (!child) return;
    child.kill("SIGTERM");
    this.stopTimer = setTimeout(() => {
      if (this.child === child) child.kill("SIGKILL");
    }, STOP_GRACE_MS);
  }

  private handleLine(line: string): void {
    const trimmed = line.trim();
    if (!trimmed) return;
    try {
      const event = JSON.parse(trimmed) as OrchestratorEvent;
      if (event && typeof event.type === "string") {
        this.push(event);
        return;
      }
    } catch {
      // linha que não é evento: vai para o log bruto
    }
    this.output.appendLine(trimmed);
  }

  private push(event: OrchestratorEvent): void {
    if (event.type === "run_end") this.sawRunEnd = true;
    this.events.push(event);
    this.onEvent(event);
  }
}

class PanelProvider implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly runner: () => OrchestratorRunner,
    private readonly output: vscode.OutputChannel,
    private readonly devMode: boolean,
  ) {}

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((msg: WebviewMessage) => this.onMessage(msg));
  }

  post(message: unknown): void {
    void this.view?.webview.postMessage(message);
  }

  // Modo desenvolvimento: recarrega o HTML/JS/CSS do painel sem recarregar a
  // janela. O painel pede "ready" de novo e recebe todos os eventos.
  refresh(): void {
    if (!this.view) return;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    this.view.webview.html = this.html(this.view.webview, media) + `<!-- ${Date.now()} -->`;
  }

  private defaults(): Omit<RunOptions, "task"> {
    return settingsOptions();
  }

  // Fila e diagnóstico vivem em activate(); devolve true quando tratou.
  onCustom: ((msg: WebviewMessage) => boolean) | undefined;

  private async onMessage(msg: WebviewMessage): Promise<void> {
    if (this.onCustom?.(msg)) return;
    const runner = this.runner();
    switch (msg.type) {
      case "ready":
        this.post({
          type: "init", defaults: this.defaults(), events: runner.events, running: runner.running,
          repo: pickRepo(), templates: templates(), devMode: this.devMode, claudeLimits: lastClaudeLimits(),
          projectName: projectName(),
        });
        this.post(historyMessage());
        break;
      case "control":
        runner.send(msg.cmd);
        break;
      case "openStepDiff": {
        const left = gitShowUri(msg.repo, msg.before, msg.path);
        const right = gitShowUri(msg.repo, msg.after, msg.path);
        await vscode.commands.executeCommand("vscode.diff", left, right, `${msg.path} (passo)`);
        break;
      }
      case "openStepChanges": {
        // Editor de várias alterações (vscode.changes); sem ele, abre o primeiro arquivo.
        const resources = msg.files.map((f) => [
          vscode.Uri.file(path.join(msg.repo, f.path)),
          gitShowUri(msg.repo, msg.before, f.path),
          gitShowUri(msg.repo, msg.after, f.path),
        ]);
        try {
          await vscode.commands.executeCommand("vscode.changes", msg.title, resources);
        } catch {
          const first = msg.files[0];
          if (first) {
            await vscode.commands.executeCommand(
              "vscode.diff", gitShowUri(msg.repo, msg.before, first.path), gitShowUri(msg.repo, msg.after, first.path), `${first.path} (passo)`,
            );
          }
        }
        break;
      }
      case "resume": {
        const file = path.join(runsDir(), path.basename(msg.file));
        let repo: string | undefined;
        try {
          repo = JSON.parse(fs.readFileSync(file, "utf8")).repo;
        } catch {
          // sem repo no log: usa a pasta aberta
        }
        runner.start({ ...msg.options, resume: file, repo });
        break;
      }
      case "run":
        if (!msg.options.task.trim()) return;
        runner.start(msg.options);
        break;
      case "stop":
        runner.stop();
        break;
      case "showLog":
        this.output.show(true);
        break;
      case "openDiff":
        if (msg.isolation) {
          await openIsolationDiff(msg.isolation, msg.path, msg.status);
          return;
        }
      // sem isolamento: segue para o diff normal do git abaixo
      case "openFile": {
        const repo = runner.repo ?? pickRepo();
        if (!repo) return;
        const uri = vscode.Uri.file(path.join(repo, msg.path));
        if (msg.type === "openDiff" && msg.status !== "??" && msg.status !== "D") {
          try {
            await vscode.commands.executeCommand("git.openChange", uri);
            return;
          } catch {
            // sem a extensão de git: abre o arquivo
          }
        }
        if (fs.existsSync(uri.fsPath)) await vscode.window.showTextDocument(uri);
        break;
      }
      case "loadHistory":
        this.post(historyMessage());
        break;
      case "openHistory": {
        const file = path.join(runsDir(), path.basename(msg.file));
        try {
          const run = JSON.parse(fs.readFileSync(file, "utf8"));
          const iso = run.isolation as Isolation | null;
          let changed: { status: string; path: string }[] = [];
          if (iso?.branch && (await branchExists(iso.source_repo, iso.branch))) {
            iso.pending = true;
            changed = await branchChanges(iso);
          } else if (iso) {
            iso.pending = false;
          }
          this.post({ type: "historyRun", file: msg.file, run, changed });
        } catch (err) {
          vscode.window.showErrorMessage(`Não foi possível ler ${file}: ${(err as Error).message}`);
        }
        break;
      }
      case "accept":
      case "discard": {
        const verb = msg.type === "accept" ? "Aceitar" : "Descartar";
        if (msg.type === "discard") {
          const sure = await vscode.window.showWarningMessage(
            `Descartar tudo o que os agentes fizeram em ${msg.isolation.branch}?`, { modal: true }, "Descartar",
          );
          if (sure !== "Descartar") return;
        }
        const cfg = vscode.workspace.getConfiguration("agentOrchestrator");
        const res = await exec(
          cfg.get<string>("pythonPath", "python3"),
          [scriptPath(), `--${msg.type}`, msg.isolation.branch, "--repo", msg.isolation.source_repo],
          msg.isolation.source_repo,
        );
        let result: { ok: boolean; message: string };
        try {
          result = JSON.parse(res.stdout.trim().split("\n").pop() || "");
        } catch {
          result = { ok: false, message: (res.stderr || res.stdout || `${verb} falhou.`).trim() };
        }
        void (result.ok ? vscode.window.showInformationMessage(result.message) : vscode.window.showErrorMessage(result.message));
        this.post({ type: "isolationResolved", branch: msg.isolation.branch, action: msg.type, ...result });
        if (result.ok && msg.type === "accept") void vscode.commands.executeCommand("workbench.view.scm");
        break;
      }
      case "openWorktree":
        if (fs.existsSync(msg.isolation.worktree)) {
          await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(msg.isolation.worktree), { forceNewWindow: true });
        } else {
          vscode.window.showWarningMessage("A worktree não existe mais.");
        }
        break;
    }
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = Array.from({ length: 32 }, () => Math.floor(Math.random() * 36).toString(36)).join("");
    const css = webview.asWebviewUri(vscode.Uri.joinPath(media, "main.css"));
    const js = webview.asWebviewUri(vscode.Uri.joinPath(media, "main.js"));
    return `<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${css}">
<title>Orquestrador</title>
</head>
<body>
<div id="app"></div>
<script nonce="${nonce}" src="${js}"></script>
</body>
</html>`;
  }
}

function settingsOptions(): Omit<RunOptions, "task"> {
  const cfg = vscode.workspace.getConfiguration("agentOrchestrator");
  return {
    backend: cfg.get<"direct" | "mco">("backend", "direct"),
    maxLoops: cfg.get<number>("maxLoops", 6),
    checks: cfg.get<string[]>("checks", []),
    isolation: cfg.get<"" | "worktree" | "none">("isolation", ""),
    approve: cfg.get<"none" | "first" | "all">("approve", "none"),
  };
}

// Modelos do .orchestrator.json do projeto aberto vêm primeiro; depois os
// gerais (configuração agentOrchestrator.templates).
function projectName(): string | undefined {
  const repo = pickRepo();
  return repo ? path.basename(repo) : undefined;
}

function projectTemplates(repo: string | undefined): Template[] {
  if (!repo) return [];
  try {
    const data = JSON.parse(fs.readFileSync(path.join(repo, ".orchestrator.json"), "utf8"));
    if (!Array.isArray(data.templates)) return [];
    return data.templates
      .filter((t: Template) => t && typeof t.name === "string" && typeof t.task === "string")
      .map((t: Template) => ({ ...t, source: "projeto" as const }));
  } catch {
    return [];
  }
}

function templates(repo: string | undefined = pickRepo()): Template[] {
  const general = vscode.workspace
    .getConfiguration("agentOrchestrator")
    .get<Template[]>("templates", [])
    .map((t) => ({ ...t, source: "geral" as const }));
  return [...projectTemplates(repo), ...general];
}

const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

// Última cota do claude vista pelo orquestrador (o stream do claude informa a
// cada chamada): só as janelas que ainda não renovaram.
function lastClaudeLimits(): Record<string, { utilization: number; resets_at: number }> | null {
  try {
    const file = path.join(path.dirname(runsDir()), "claude-limits.json");
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    const now = Date.now() / 1000;
    const out: Record<string, { utilization: number; resets_at: number }> = {};
    for (const [name, w] of Object.entries((data.unifiedWindows ?? {}) as Record<string, { utilization: number; resetsAt: number }>)) {
      if (w.resetsAt > now) out[name] = { utilization: Number(w.utilization), resets_at: Number(w.resetsAt) };
    }
    return Object.keys(out).length ? out : null;
  } catch {
    return null;
  }
}

function historyMessage() {
  const items = readHistory();
  const since = Date.now() - WEEK_MS;
  const recent = items.filter((it) => it.mtime >= since);
  return {
    type: "history",
    items,
    week: {
      runs: recent.length,
      claudeCalls: recent.reduce((sum, it) => sum + it.claudeCalls, 0),
    },
  };
}

// Diff "commit base ↔ resultado do agente": à direita, o arquivo da worktree
// (editável) enquanto ela existir; senão, a versão commitada na branch.
async function openIsolationDiff(iso: Isolation, file: string, status: string): Promise<void> {
  const left = gitShowUri(iso.source_repo, iso.base, file);
  const onDisk = path.join(iso.worktree, file);
  const right = status !== "D" && fs.existsSync(onDisk) ? vscode.Uri.file(onDisk) : gitShowUri(iso.source_repo, iso.branch, file);
  await vscode.commands.executeCommand("vscode.diff", left, right, `${file} (base ↔ ${iso.branch})`);
}

function readHistory(): HistoryItem[] {
  const dir = runsDir();
  let files: string[];
  try {
    files = fs.readdirSync(dir).filter((f) => f.endsWith(".json")).sort().reverse().slice(0, 100);
  } catch {
    return [];
  }
  const items: HistoryItem[] = [];
  for (const file of files) {
    try {
      const run = JSON.parse(fs.readFileSync(path.join(dir, file), "utf8"));
      const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(file);
      items.push({
        file,
        date: m ? `${m[3]}/${m[2]} ${m[4]}:${m[5]}` : file,
        task: String(run.task ?? ""),
        status: String(run.status ?? "?"),
        steps: Array.isArray(run.steps) ? run.steps.length : 0,
        claudeCalls: Number(run.claude_calls) || 0,
        mtime: fs.statSync(path.join(dir, file)).mtimeMs,
      });
    } catch {
      // log corrompido: ignora
    }
  }
  return items;
}

type QueueItem = { id: string; options: RunOptions; repo: string; added: number };

// Notificação do sistema (notify-send) quando o editor está sem foco.
function notifySystem(title: string, body: string): void {
  const cfg = vscode.workspace.getConfiguration("agentOrchestrator");
  if (!cfg.get<boolean>("systemNotifications", true) || vscode.window.state.focused) return;
  execFile("notify-send", ["-a", "Orquestrador", title, body], () => undefined);
}

async function runDoctor(repo: string | undefined): Promise<unknown[]> {
  const cfg = vscode.workspace.getConfiguration("agentOrchestrator");
  const args = [scriptPath(), "--doctor", "--events", "jsonl", "--repo", repo || os.homedir()];
  const configPath = cfg.get<string>("configPath", "").trim();
  if (configPath) args.push("--config", expandHome(configPath));
  const res = await exec(cfg.get<string>("pythonPath", "python3"), args, repo || os.homedir());
  try {
    return (JSON.parse(res.stdout.trim().split("\n").pop() || "") as { checks: unknown[] }).checks;
  } catch {
    return [{ name: "Orquestrador", ok: false, detail: (res.stderr || res.stdout || "sem resposta").trim().slice(0, 500), hint: "Confira agentOrchestrator.pythonPath e scriptPath." }];
  }
}

// A extensão está em "modo desenvolvimento" quando a pasta instalada é um
// link para o código-fonte (scripts/dev-link.sh).
function realExtensionPath(context: vscode.ExtensionContext): string {
  try {
    return fs.realpathSync(context.extensionPath);
  } catch {
    return context.extensionPath;
  }
}

function watchDev(
  context: vscode.ExtensionContext,
  realPath: string,
  onMedia: () => void,
  onCode: () => void,
): void {
  const debounce = (fn: () => void, ms: number) => {
    let t: NodeJS.Timeout | undefined;
    return () => {
      if (t) clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  };
  const media = debounce(onMedia, 300);
  const code = debounce(onCode, 800);
  for (const [dir, fn, filter] of [
    [path.join(realPath, "media"), media, (f: string) => /\.(js|css|svg)$/.test(f)],
    [path.join(realPath, "out"), code, (f: string) => f === "extension.js"],
  ] as const) {
    try {
      const watcher = fs.watch(dir, (_event, file) => {
        if (file && filter(String(file))) fn();
      });
      context.subscriptions.push({ dispose: () => watcher.close() });
    } catch {
      // pasta ainda não existe (ex.: out antes do primeiro build)
    }
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const output = vscode.window.createOutputChannel("Orquestrador de Agentes");
  const realPath = realExtensionPath(context);
  const devMode = realPath !== context.extensionPath;
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  status.command = `${VIEW_ID}.focus`;

  let cycleText = "";
  let agentText = "";
  const refreshStatus = (running: boolean) => {
    if (!running) {
      status.hide();
      return;
    }
    status.text = `$(sync~spin) Orquestrador${cycleText ? ` · ${cycleText}` : ""}${agentText ? ` · ${agentText}` : ""}`;
    status.tooltip = "Abrir o painel do orquestrador";
    status.show();
  };

  // Criado depois do runner (um depende do outro); só é usado nos callbacks.
  let provider: PanelProvider;
  const runner = new OrchestratorRunner(
    output,
    (event) => {
      if (event.type === "run_start") {
        cycleText = "";
        agentText = "";
      } else if (event.type === "cycle_start") {
        cycleText = `ciclo ${event.cycle}/${event.max}`;
        agentText = "supervisor";
      } else if (event.type === "agent_start") {
        agentText = String(event.agent);
      } else if (event.type === "run_end") {
        lastStatus = String(event.status);
        const ok = event.status === "done";
        const msg = `Orquestração ${ok ? "concluída" : `encerrada (${event.status})`}.`;
        void (ok ? vscode.window.showInformationMessage(msg) : vscode.window.showWarningMessage(msg));
        notifySystem(msg, String(event.summary || "").slice(0, 200));
      } else if (event.type === "awaiting_approval") {
        notifySystem("Aguardando sua aprovação", `Ciclo ${event.cycle}: ${event.agent} — ${String(event.reason || "").slice(0, 150)}`);
      }
      if (event.type !== "agent_output") refreshStatus(runner.running);
      provider.post({ type: "event", event });
    },
    (running) => {
      refreshStatus(running);
      provider.post({ type: "state", running });
      if (!running) {
        provider.post(historyMessage());
        // Parada pelo usuário pausa a fila; qualquer outro fim segue para a próxima.
        if (queue.length && (lastStatus === "stopped" || lastStatus === "interrupted")) {
          queuePaused = true;
          postQueue();
        } else {
          setTimeout(startNext, 1500);
        }
      }
    },
  );

  // ── fila de tarefas ──
  let lastStatus: string | undefined;
  let queue: QueueItem[] = context.globalState.get<QueueItem[]>("queue", []);
  let queuePaused = false;
  const postQueue = () =>
    provider.post({
      type: "queue",
      paused: queuePaused,
      items: queue.map((q) => ({ id: q.id, task: q.options.task, repo: path.basename(q.repo), maxLoops: q.options.maxLoops })),
    });
  const saveQueue = () => {
    void context.globalState.update("queue", queue);
    postQueue();
  };
  const startNext = () => {
    if (runner.running || queuePaused || !queue.length) return;
    const item = queue.shift()!;
    saveQueue();
    runner.start({ ...item.options, repo: item.repo });
  };
  provider = new PanelProvider(context, () => runner, output, devMode);
  provider.onCustom = (msg) => {
    switch (msg.type) {
      case "ready":
        setTimeout(postQueue, 0); // depois do init
        return false;
      case "enqueue": {
        const repo = pickRepo();
        if (!repo || !msg.options.task.trim()) return true;
        queue.push({ id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, options: msg.options, repo, added: Date.now() });
        saveQueue();
        startNext();
        return true;
      }
      case "dequeue":
        queue = queue.filter((q) => q.id !== msg.id);
        saveQueue();
        return true;
      case "resumeQueue":
        queuePaused = false;
        postQueue();
        startNext();
        return true;
      case "doctor":
        void runDoctor(pickRepo()).then((checks) => provider.post({ type: "doctor", checks }));
        return true;
      default:
        return false;
    }
  };

  let lastRepo = pickRepo();
  const pushTemplates = () => provider.post({ type: "templates", templates: templates(), projectName: projectName() });
  const projectWatcher = vscode.workspace.createFileSystemWatcher("**/.orchestrator.json");
  context.subscriptions.push(
    projectWatcher,
    projectWatcher.onDidChange(pushTemplates),
    projectWatcher.onDidCreate(pushTemplates),
    projectWatcher.onDidDelete(pushTemplates),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("agentOrchestrator.templates")) pushTemplates();
    }),
    vscode.window.onDidChangeActiveTextEditor(() => {
      const repo = pickRepo();
      if (repo !== lastRepo) {
        lastRepo = repo;
        pushTemplates();
      }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(pushTemplates),
  );

  if (devMode) {
    output.appendLine(`[dev] Modo desenvolvimento: código em ${realPath}`);
    watchDev(
      context,
      realPath,
      () => provider.refresh(),
      async () => {
        const warning = runner.running ? " A orquestração em andamento será interrompida." : "";
        const pick = await vscode.window.showInformationMessage(
          `Orquestrador: código da extensão atualizado.${warning}`, "Recarregar janela",
        );
        if (pick) await vscode.commands.executeCommand("workbench.action.reloadWindow");
      },
    );
  }

  context.subscriptions.push(
    output,
    status,
    vscode.workspace.registerTextDocumentContentProvider(GIT_SCHEME, new GitShowProvider()),
    vscode.window.registerWebviewViewProvider(VIEW_ID, provider, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.commands.registerCommand("agentOrchestrator.run", async () => {
      const task = await vscode.window.showInputBox({
        prompt: "O que os agentes devem fazer?",
        placeHolder: "Ex.: Adicionar endpoint GET /api/health com teste unitário",
      });
      if (!task?.trim()) return;
      await vscode.commands.executeCommand(`${VIEW_ID}.focus`);
      runner.start({ task: task.trim(), ...settingsOptions() });
    }),
    vscode.commands.registerCommand("agentOrchestrator.stop", () => runner.stop()),
    vscode.commands.registerCommand("agentOrchestrator.showLog", () => output.show(true)),
    vscode.commands.registerCommand("agentOrchestrator.doctor", async () => {
      await vscode.commands.executeCommand(`${VIEW_ID}.focus`);
      provider.post({ type: "doctor", checks: await runDoctor(pickRepo()) });
    }),
    { dispose: () => runner.stop() },
  );
}

export function deactivate(): void {
  // o runner é encerrado pelo dispose registrado em activate
}
