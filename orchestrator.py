#!/usr/bin/env python3
"""Orquestrador de agentes de código via CLI (agy → codex → claude).

Recebe uma instrução em linguagem natural e alterna automaticamente entre
três CLIs já autenticadas, sem API paga:

- agy    (Gemini): supervisor de cada ciclo + arquitetura/frontend.
- codex  (OpenAI): backend, lógica de negócio e testes.
- claude (Claude): só revisão final ou diagnóstico de bug insolúvel,
                   sempre atrás de um filtro de qualidade local.

Uso:
    python3 orchestrator.py "Minha necessidade técnica aqui"
    python3 orchestrator.py --backend mco --check "pnpm test" "..."

Todas as flags das CLIs ficam em orchestrator.json — se uma CLI mudar,
ajuste o JSON, não este arquivo. Só usa a biblioteca padrão do Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "orchestrator.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agent-orchestrator"
RUNS_DIR = CACHE_DIR / "runs"
WORKTREES_DIR = CACHE_DIR / "worktrees"
PROJECT_CONFIG_NAME = ".orchestrator.json"
CLAUDE_LIMITS_FILE = CACHE_DIR / "claude-limits.json"
WORKERS = ("agy", "codex", "claude")


# ─── Saída colorida ──────────────────────────────────────────────────────────


class C:
    """Códigos ANSI; ficam vazios fora de um terminal ou com NO_COLOR."""

    _on = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    RESET = "\033[0m" if _on else ""
    BOLD = "\033[1m" if _on else ""
    DIM = "\033[2m" if _on else ""
    RED = "\033[31m" if _on else ""
    GREEN = "\033[32m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    BLUE = "\033[34m" if _on else ""
    MAGENTA = "\033[35m" if _on else ""
    CYAN = "\033[36m" if _on else ""


AGENT_COLOR = {"agy": C.BLUE, "codex": C.GREEN, "claude": C.MAGENTA, "supervisor": C.CYAN}

# Com --events jsonl o stdout é reservado aos eventos (lidos pela extensão do
# editor) e os logs legíveis vão para o stderr.
_OUTPUT_LOCK = threading.Lock()
_events_enabled = False
_log_stream = sys.stdout


def enable_events() -> None:
    global _events_enabled, _log_stream
    _events_enabled = True
    _log_stream = sys.stderr


def emit(event_type: str, **data: Any) -> None:
    """Uma linha JSON por evento no stdout; nada acontece sem --events jsonl."""
    if not _events_enabled:
        return
    payload = {"type": event_type, "ts": round(time.time(), 3), **data}
    with _OUTPUT_LOCK:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _print(text: str) -> None:
    with _OUTPUT_LOCK:
        print(text, file=_log_stream, flush=True)


def log(icon: str, msg: str, color: str = "") -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    _print(f"{C.DIM}{stamp}{C.RESET} {icon} {color}{msg}{C.RESET}")


def banner(text: str) -> None:
    line = "─" * max(10, min(78, shutil.get_terminal_size((80, 20)).columns - 2))
    _print(f"\n{C.BOLD}{line}\n {text}\n{line}{C.RESET}")


def preview(text: str, limit: int = 600) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n{C.DIM}… (+{len(text) - limit} caracteres){C.RESET}"


# ─── Execução de comandos ────────────────────────────────────────────────────


@dataclass
class CmdResult:
    code: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def run_command(
    cmd: list[str] | str,
    cwd: Path,
    timeout: int,
    stdin_text: str | None = None,
    shell: bool = False,
    on_line: Callable[[str, str], None] | None = None,
    env: dict[str, str] | None = None,
    abort: threading.Event | None = None,
) -> CmdResult:
    """Roda um comando capturando stdout/stderr/exit code, com timeout real.

    Usa Popen em vez de subprocess.run por dois motivos: o run() só mata o
    processo direto no timeout (as CLIs de agente abrem subprocessos — node,
    sandboxes — que ficariam órfãos segurando o terminal; aqui o grupo inteiro
    é encerrado) e só devolve a saída no fim, enquanto `on_line` recebe cada
    linha assim que ela sai, para acompanhar o agente ao vivo. `abort`, quando
    sinalizado de outra thread, encerra o comando antes do timeout.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            shell=shell,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return CmdResult(127, "", f"Comando não encontrado: {exc.filename}", 0.0)

    chunks: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def pump(stream_name: str, pipe: Any) -> None:
        for line in iter(pipe.readline, ""):
            chunks[stream_name].append(line)
            if on_line:
                on_line(stream_name, line.rstrip("\n"))
        pipe.close()

    readers = [
        threading.Thread(target=pump, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=pump, args=("stderr", proc.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    if stdin_text is not None and proc.stdin:
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    timed_out = aborted = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if abort is not None and abort.is_set():
                    aborted = True
                    _kill_group(proc)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _kill_group(proc)
                    break
    except KeyboardInterrupt:
        _kill_group(proc)
        raise
    finally:
        # Um neto que saiu do grupo pode segurar o pipe aberto: não espera para sempre.
        for reader in readers:
            reader.join(timeout=5)

    out, err = "".join(chunks["stdout"]), "".join(chunks["stderr"])
    if timed_out:
        err += f"\n[orquestrador] Tempo esgotado após {timeout}s; processo encerrado."
        return CmdResult(124, out, err, time.monotonic() - started, timed_out=True)
    if aborted:
        err += "\n[orquestrador] Comando interrompido pelo orquestrador."
        return CmdResult(130, out, err, time.monotonic() - started)
    return CmdResult(proc.returncode, out, err, time.monotonic() - started)


def _kill_group(proc: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
            proc.wait(timeout=wait)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            continue


# ─── Parser defensivo de JSON ────────────────────────────────────────────────

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)


def extract_decision(text: str) -> dict[str, Any] | None:
    """Acha o objeto JSON de decisão no meio de logs, markdown e texto livre.

    Ordem: blocos ```json``` primeiro; depois varre o texto tentando decodificar
    a partir de cada "{" (raw_decode tolera texto depois do objeto). Entre os
    candidatos com "next_agent", vale o ÚLTIMO — o modelo às vezes ecoa o
    exemplo do prompt antes da resposta de verdade.
    """
    clean = ANSI_RE.sub("", text)
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []

    for block in FENCE_RE.findall(clean):
        try:
            obj = json.loads(block.strip())
            if isinstance(obj, dict):
                candidates.append(obj)
        except json.JSONDecodeError:
            pass

    for match in re.finditer(r"\{", clean):
        try:
            obj, _ = decoder.raw_decode(clean, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            candidates.append(obj)

    valid = [c for c in candidates if "next_agent" in c]
    return valid[-1] if valid else None


def normalize_decision(raw: dict[str, Any]) -> dict[str, Any]:
    agent = str(raw.get("next_agent", "")).strip().lower()
    if agent in ("done", "fim", "concluido", "concluído", "none", ""):
        agent = "DONE"
    purpose = str(raw.get("claude_purpose") or "").strip().lower()
    return {
        "next_agent": agent,
        "reason": str(raw.get("reason") or "").strip() or "(sem motivo informado)",
        "claude_purpose": purpose if purpose in ("review", "diagnosis") else "review",
        "prompt": str(raw.get("prompt") or "").strip(),
        "summary": str(raw.get("summary") or "").strip(),
    }


# ─── Estado da execução ──────────────────────────────────────────────────────


@dataclass
class Step:
    cycle: int
    agent: str
    reason: str
    prompt: str
    code: int
    output: str
    seconds: float
    redirected_from: str | None = None
    # Retratos (árvores git) antes e depois do passo e o que mudou entre eles.
    tree_before: str | None = None
    tree_after: str | None = None
    files: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunState:
    task: str
    repo: str
    backend: str
    steps: list[Step] = field(default_factory=list)
    claude_calls: int = 0
    final_summary: str = ""
    status: str = "running"
    isolation: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    project_config: str | None = None
    notes: list[str] = field(default_factory=list)
    stats: dict[str, dict[str, float]] = field(default_factory=dict)
    resumed_from: str | None = None

    def count(self, agent: str, seconds: float) -> None:
        entry = self.stats.setdefault(agent, {"calls": 0, "seconds": 0.0})
        entry["calls"] += 1
        entry["seconds"] = round(entry["seconds"] + seconds, 1)

    def attempts(self, agent: str) -> int:
        return sum(1 for s in self.steps if s.agent == agent)


def clip(text: str, limit: int) -> str:
    """Corta preservando começo e fim (erros costumam estar no fim)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n\n[… {len(text) - limit} caracteres omitidos …]\n\n{text[-tail:]}"


# ─── Saída estruturada das CLIs ──────────────────────────────────────────────


@dataclass
class Activity:
    kind: str  # file | command | tool | message | warning
    text: str
    data: dict[str, Any] = field(default_factory=dict)


class StreamParser:
    """Traduz o stream JSON de uma CLI em linhas legíveis e atividades.

    Formatos (stream_format no orchestrator.json):
    - codex-json:    `codex exec --json` (thread/turn/item.*)
    - claude-stream: `claude -p --output-format stream-json --verbose`
    - agy-stream:    `agy --output-format stream-json`
    Linhas que não são JSON passam adiante como texto.
    """

    FILE_KEYS = ("AbsolutePath", "TargetFile", "FilePath", "file_path", "path")
    WRITE_HINT = re.compile(r"write|edit|replace|create|patch|multi_?edit", re.IGNORECASE)

    def __init__(self, fmt: str, repo: Path) -> None:
        self.fmt = fmt
        self.repo = repo
        self.final_text: str | None = None
        self.messages: list[str] = []
        self.tokens = 0
        self.cost_usd: float | None = None
        self.limits: dict[str, Any] | None = None
        self.denied: list[str] = []

    def rel(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.repo.resolve()))
        except (ValueError, OSError):
            return path

    def feed(self, line: str) -> list[Activity]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return [Activity("message", line)] if line.strip() else []
        if not isinstance(event, dict):
            return []
        handler = {"codex-json": self._codex, "claude-stream": self._claude, "agy-stream": self._agy}.get(self.fmt)
        return handler(event) if handler else []

    def text(self) -> str:
        """Resposta final do agente (ou as mensagens, se o fim não veio)."""
        if self.final_text and self.final_text.strip():
            return self.final_text
        return "\n\n".join(self.messages)

    # codex exec --json
    def _codex(self, e: dict[str, Any]) -> list[Activity]:
        kind, item = e.get("type"), e.get("item") or {}
        itype = item.get("type")
        out: list[Activity] = []
        if kind == "turn.completed" and e.get("usage"):
            u = e["usage"]
            self.tokens += int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
        elif itype == "agent_message" and kind == "item.completed":
            text = str(item.get("text", "")).strip()
            if text:
                self.messages.append(text)
                self.final_text = text
                out.append(Activity("message", text))
        elif itype == "file_change" and kind == "item.completed":
            for ch in item.get("changes", []):
                path = self.rel(str(ch.get("path", "")))
                out.append(Activity("file", f"{ch.get('kind', 'edit')} {path}", {"path": path, "change": ch.get("kind")}))
        elif itype == "command_execution":
            cmd = str(item.get("command", ""))
            cmd = re.sub(r"^/\S*?(ba|z)?sh\s+-lc\s+", "", cmd).strip("'\"")
            if kind == "item.started":
                out.append(Activity("command", f"$ {cmd}", {"command": cmd, "running": True}))
            elif kind == "item.completed":
                code = item.get("exit_code")
                tail = str(item.get("aggregated_output", "")).strip().splitlines()[-3:]
                out.append(Activity("command", f"{'✅' if code == 0 else '❌'} {cmd} (exit {code})",
                                    {"command": cmd, "exit_code": code, "tail": "\n".join(tail)}))
        elif itype == "error" and kind == "item.completed":
            out.append(Activity("warning", str(item.get("message", ""))[:300]))
        elif itype and kind == "item.completed" and itype not in ("reasoning",):
            out.append(Activity("tool", f"{itype}", {"item": itype}))
        return out

    # claude -p --output-format stream-json --verbose
    def _claude(self, e: dict[str, Any]) -> list[Activity]:
        kind = e.get("type")
        out: list[Activity] = []
        if kind == "assistant":
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text", "").strip():
                    self.messages.append(c["text"].strip())
                    out.append(Activity("message", c["text"].strip()))
                elif c.get("type") == "tool_use":
                    name, inp = c.get("name", "?"), c.get("input") or {}
                    target = next((str(inp[k]) for k in self.FILE_KEYS if k in inp), inp.get("command") or inp.get("pattern") or "")
                    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        path = self.rel(str(target))
                        out.append(Activity("file", f"{name} {path}", {"path": path, "change": name}))
                    else:
                        shown = self.rel(str(target)) if target else ""
                        out.append(Activity("tool", f"{name} {shown}".strip(), {"tool": name}))
        elif kind == "user":
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "tool_result" and c.get("is_error"):
                    out.append(Activity("warning", f"ferramenta falhou: {str(c.get('content'))[:200]}"))
        elif kind == "rate_limit_event":
            self.limits = e.get("rate_limit_info") or None
        elif kind == "result":
            self.final_text = str(e.get("result") or "")
            u = e.get("usage") or {}
            self.tokens += sum(int(u.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
            if e.get("total_cost_usd") is not None:
                self.cost_usd = float(e["total_cost_usd"])
        return out

    # agy --output-format stream-json
    def _agy(self, e: dict[str, Any]) -> list[Activity]:
        out: list[Activity] = []
        if e.get("event") == "step_update":
            st = e.get("step_update") or {}
            if st.get("step_type") == "tool" and st.get("state") in ("DONE", "ERROR"):
                name = str(st.get("tool_name", "?"))
                params = (st.get("tool_info") or {}).get("parameters") or {}
                target = next((str(params[k]) for k in self.FILE_KEYS if k in params), params.get("CommandLine", ""))
                failed = st.get("state") == "ERROR"
                if self.WRITE_HINT.search(name) and target:
                    path = self.rel(str(target))
                    out.append(Activity("file", f"{name} {path}{' (falhou)' if failed else ''}", {"path": path, "change": name}))
                elif name == "run_command":
                    out.append(Activity("command", f"{'❌' if failed else '✅'} {target}", {"command": target, "exit_code": 1 if failed else 0}))
                else:
                    shown = self.rel(str(target)) if target else ""
                    out.append(Activity("tool", f"{name} {shown}{' (falhou)' if failed else ''}".strip(), {"tool": name}))
            if st.get("step_type") == "agent_response" and st.get("usage"):
                self.tokens += int(st["usage"].get("total_tokens", 0))
        elif e.get("event") == "result":
            r = e.get("result") or {}
            self.final_text = str(r.get("response") or "")
            for d in r.get("denied_actions") or []:
                self.denied.append(str(d.get("action")))
            if self.denied:
                out.append(Activity("warning", "ações negadas em modo não interativo: " + ", ".join(sorted(set(self.denied)))))
        return out


def save_claude_limits(limits: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CLAUDE_LIMITS_FILE.write_text(json.dumps({"saved_at": time.time(), **limits}), encoding="utf-8")
    except OSError:
        pass


def load_claude_limits() -> dict[str, Any] | None:
    try:
        return json.loads(CLAUDE_LIMITS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def claude_windows(limits: dict[str, Any] | None) -> dict[str, dict[str, float]]:
    """Janelas de uso do claude ainda válidas (resetsAt no futuro)."""
    now = time.time()
    windows = (limits or {}).get("unifiedWindows") or {}
    return {
        name: {"utilization": float(w.get("utilization", 0)), "resets_at": float(w.get("resetsAt", 0))}
        for name, w in windows.items()
        if float(w.get("resetsAt", 0)) > now
    }


# ─── Git, worktree e configuração do projeto ─────────────────────────────────


def git(args: list[str], cwd: Path, timeout: int = 120) -> CmdResult:
    return run_command(["git", *args], cwd, timeout)


def git_root(path: Path) -> Path | None:
    res = git(["rev-parse", "--show-toplevel"], path, 30)
    return Path(res.stdout.strip()) if res.ok and res.stdout.strip() else None


def load_project_config(root: Path) -> tuple[dict[str, Any], Path | None]:
    """Lê o .orchestrator.json do projeto (opcional) e valida os tipos."""
    path = root / PROJECT_CONFIG_NAME
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} inválido: {exc}")
    expected = {
        "checks": list, "rules": str, "env": dict, "forbidden_patterns": list,
        "on_forbidden": str, "isolation": str, "copy_files": list, "setup": list, "max_loops": int,
        "templates": list,  # usado só pela extensão (modelos de tarefa do projeto)
        "node": str,  # versão do Node dos agentes e verificações (resolvida via fnm)
    }
    for key, kind in expected.items():
        if key in data and not isinstance(data[key], kind):
            sys.exit(f"{path}: o campo '{key}' precisa ser {kind.__name__}.")
    for i, tpl in enumerate(data.get("templates", [])):
        if not (isinstance(tpl, dict) and isinstance(tpl.get("name"), str) and isinstance(tpl.get("task"), str)):
            sys.exit(f"{path}: templates[{i}] precisa ter 'name' e 'task' (texto).")
    for pattern in data.get("forbidden_patterns", []):
        try:
            re.compile(pattern)
        except re.error as exc:
            sys.exit(f"{path}: padrão proibido inválido {pattern!r}: {exc}")
    return data, path


def resolve_node_bin(version: str) -> tuple[Path | None, str]:
    """Pasta bin do Node pedido no projeto (via fnm). Retorna (pasta, erro)."""
    if not shutil.which("fnm"):
        return None, "fnm não encontrado no PATH"
    res = subprocess.run(
        ["fnm", "exec", f"--using={version}", "node", "-p",
         "require('path').dirname(process.execPath) + '|' + process.version"],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0 or "|" not in res.stdout:
        return None, (res.stderr or res.stdout).strip()[:300] or f"fnm não tem o Node {version}"
    bin_dir, _ = res.stdout.strip().rsplit("|", 1)
    return Path(bin_dir), ""


def codex_mcp_servers() -> list[str]:
    """Nomes dos servidores MCP configurados no codex (para desligá-los por execução)."""
    if not shutil.which("codex"):
        return []
    try:
        res = subprocess.run(["codex", "mcp", "list", "--json"], capture_output=True, text=True, timeout=60)
        data = json.loads(res.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    return [str(item["name"]) for item in data if isinstance(item, dict) and item.get("name")]


REVIEW_VERDICT_RE = re.compile(r"VEREDITO\s*:\s*\**\s*(APROVADO|AJUSTES)", re.IGNORECASE)


def review_verdict(output: str) -> str | None:
    """Último veredito (APROVADO/AJUSTES) que o claude escreveu na revisão."""
    found = REVIEW_VERDICT_RE.findall(output or "")
    return found[-1].upper() if found else None


def snapshot_tree(repo: Path) -> str | None:
    """Retrato do estado atual dos arquivos (respeitando .gitignore) como árvore git.

    Usa um índice temporário: não commita, não mexe no stage do usuário nem na
    branch. Os objetos ficam no repositório até o `git gc` limpar (≥ 2 semanas).
    """
    fd, index = tempfile.mkstemp(prefix="orch-index-")
    os.close(fd)
    os.unlink(index)
    env = {**os.environ, "GIT_INDEX_FILE": index}
    try:
        steps = (["read-tree", "HEAD"], ["add", "-A"], ["write-tree"])
        res = None
        for args in steps:
            res = run_command(["git", *args], repo, 300, env=env)
            if not res.ok and args[0] != "read-tree":  # repositório sem commit: começa vazio
                return None
        return res.stdout.strip() if res else None
    finally:
        Path(index).unlink(missing_ok=True)


def tree_changes(repo: Path, before: str, after: str) -> list[dict[str, Any]]:
    """Arquivos que mudaram entre dois retratos, com linhas adicionadas/removidas."""
    if before == after:
        return []
    status = git(["diff-tree", "-r", "--name-status", "--no-renames", before, after], repo, 120)
    counts = git(["diff-tree", "-r", "--numstat", "--no-renames", before, after], repo, 120)
    numbers: dict[str, tuple[str, str]] = {}
    for line in counts.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            numbers[parts[2]] = (parts[0], parts[1])
    files = []
    for line in status.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            added, deleted = numbers.get(parts[1], ("-", "-"))
            files.append({
                "status": parts[0], "path": parts[1],
                "added": int(added) if added.isdigit() else None,
                "deleted": int(deleted) if deleted.isdigit() else None,
            })
    return files


def worktree_path_for(source: Path, branch: str) -> Path | None:
    res = git(["worktree", "list", "--porcelain"], source, 30)
    current: Path | None = None
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and current:
            return current
    return None


def branch_changes(source: Path, base: str, branch: str) -> list[dict[str, str]]:
    res = git(["diff", "--name-status", "--no-renames", base, branch], source, 60)
    files = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append({"status": parts[0].strip(), "path": parts[-1]})
    return files


def remove_worktree(source: Path, branch: str) -> None:
    path = worktree_path_for(source, branch)
    if path:
        git(["worktree", "remove", "--force", str(path)], source, 120)
    git(["branch", "-D", branch], source, 30)
    git(["worktree", "prune"], source, 30)


def accept_branch(repo: Path, branch: str) -> tuple[bool, str]:
    """Aplica as alterações da branch do orquestrador no repositório, em stage.

    Usa merge --squash: nada é commitado, você revisa e commita. Antes, um
    merge-tree de teste garante que não há conflito — se houver, nada é tocado.
    """
    root = git_root(repo)
    if not root:
        return False, f"{repo} não é um repositório git."
    if not git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], root, 30).ok:
        return False, f"A branch {branch} não existe mais (já aceita ou descartada?)."
    trial = git(["merge-tree", "--write-tree", "--name-only", "HEAD", branch], root, 120)
    if trial.code == 1:
        # Saída: OID da árvore, os arquivos em conflito e, após uma linha vazia, mensagens.
        conflicts = []
        for line in trial.stdout.splitlines()[1:]:
            if not line.strip():
                break
            conflicts.append(line.strip())
        return False, "Conflito com o estado atual do repositório em: " + ", ".join(conflicts[:10])
    merged = git(["merge", "--squash", branch], root, 120)
    if not merged.ok:
        return False, f"git merge --squash falhou: {clip(merged.stderr or merged.stdout, 1500)}"
    remove_worktree(root, branch)
    return True, f"Alterações de {branch} aplicadas em stage no repositório. Revise com git diff --cached e faça o commit."


def discard_branch(repo: Path, branch: str) -> tuple[bool, str]:
    root = git_root(repo)
    if not root:
        return False, f"{repo} não é um repositório git."
    remove_worktree(root, branch)
    return True, f"Worktree e branch {branch} descartadas."


# ─── Canal de controle (extensão → orquestrador) ─────────────────────────────


class Control:
    """Comandos JSON, um por linha, vindos do stdin (--control stdin).

    {"cmd": "note", "text": "..."}       nota para o supervisor (vale até o fim)
    {"cmd": "approve", "prompt": "..."}  aprova o passo pendente (prompt opcional, editado)
    {"cmd": "reject", "reason": "..."}   recusa o passo pendente; o supervisor decide de novo
    {"cmd": "stop"}                      encerra no próximo ponto seguro
    """

    def __init__(self, on_note: Callable[[str], None]) -> None:
        self.on_note = on_note
        self.decisions: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in sys.stdin:
            try:
                cmd = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(cmd, dict):
                continue
            kind = cmd.get("cmd")
            if kind == "note" and str(cmd.get("text", "")).strip():
                self.on_note(str(cmd["text"]).strip())
            elif kind in ("approve", "reject"):
                self.decisions.put(cmd)
            elif kind == "stop":
                self.stop.set()
                self.decisions.put(cmd)


# ─── Orquestrador ────────────────────────────────────────────────────────────


class Orchestrator:
    def __init__(self, config: dict[str, Any], args: argparse.Namespace) -> None:
        self.cfg = config
        self.repo = Path(args.repo).resolve()
        # self.repo é onde os agentes trabalham (vira a worktree com isolamento);
        # self.source_repo é sempre o repositório do usuário.
        self.source_repo = git_root(self.repo) or self.repo
        self.project, project_path = load_project_config(self.source_repo)
        self.backend = args.backend or os.environ.get("ORCH_BACKEND") or config.get("backend", "direct")
        self.max_loops = args.max_loops or self.project.get("max_loops") or config.get("max_loops", 6)
        self.isolation_mode = args.isolation or self.project.get("isolation") or config.get("isolation", "worktree")
        self.env = {**os.environ}
        for key, value in self.project.get("env", {}).items():
            if value is None:
                self.env.pop(key, None)
            else:
                self.env[key] = str(value)
        self.node_note = ""
        node_version = self.project.get("node")
        if node_version:
            bin_dir, err = resolve_node_bin(node_version)
            if bin_dir:
                self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env.get('PATH', '')}"
                self.node_note = f"Node {node_version} ({bin_dir})"
            else:
                self.node_note = f"Node {node_version} pedido no projeto, mas não resolvido: {err}"
        self._codex_mcp_off: list[str] | None = None
        # Árvore git em que as verificações do filtro pré-claude passaram por
        # último: se a revisão aprovar essa mesma árvore, a execução termina.
        self.gate_passed_tree: str | None = None
        self.forbidden = [re.compile(p, re.IGNORECASE) for p in self.project.get("forbidden_patterns", [])]
        self.abort_on_forbidden = self.project.get("on_forbidden", "warn") == "abort"
        self.abort_event = threading.Event()
        self.timeouts = config.get("timeouts", {})
        if args.timeout:
            self.timeouts = {k: args.timeout for k in self.timeouts} | {"check": self.timeouts.get("check", 600)}
        self.hist_cfg = config.get("history", {})
        self.gate_cfg = dict(config.get("quality_gate", {}))
        commands: list[str] = []
        for command in [*self.gate_cfg.get("commands", []), *self.project.get("checks", []), *(args.check or [])]:
            if command not in commands:
                commands.append(command)
        self.gate_cfg["commands"] = commands
        self.gate_enabled = not args.no_gate
        self.claude_policy = config.get("claude_policy", {})
        self.workdir = Path(tempfile.mkdtemp(prefix="orch-"))
        self.state = RunState(
            task=args.task, repo=str(self.source_repo), backend=self.backend,
            project_config=str(project_path) if project_path else None,
        )
        self.approve_mode = args.approve or "none"
        self.approvals_done = 0
        self.notes_lock = threading.Lock()
        self.control = Control(self._add_note) if args.control == "stdin" else None
        self.first_cycle = 1
        self.last_cycle = self.max_loops
        self.last_tree: str | None = None
        self.resume_data: dict[str, Any] | None = getattr(args, "resume_data", None)
        if self.resume_data:
            self._restore(self.resume_data, args.resume)

    def _restore(self, data: dict[str, Any], log_file: str) -> None:
        """Retoma uma execução salva: histórico, notas, contagens e worktree."""
        self.state.steps = [Step(**step) for step in data.get("steps", [])]
        self.state.claude_calls = int(data.get("claude_calls", 0))
        self.state.notes = list(data.get("notes", []))
        self.state.stats = dict(data.get("stats", {}))
        self.state.resumed_from = log_file
        iso = data.get("isolation")
        if iso and iso.get("pending"):
            self.state.isolation = dict(iso)
        last = max((step.cycle for step in self.state.steps), default=0)
        self.first_cycle = last + 1
        self.last_cycle = last + self.max_loops

    def _add_note(self, text: str) -> None:
        with self.notes_lock:
            self.state.notes.append(text)
        log("💬", f"Nota recebida: {text}", C.CYAN)
        emit("note_received", text=text)

    # — invocação das CLIs —

    def _streamer(self, cycle: int, agent: str) -> Callable[[str, str], None]:
        """Repassa cada linha do agente como evento agent_output."""

        def on_line(stream: str, line: str) -> None:
            clean = ANSI_RE.sub("", line)
            emit("agent_output", cycle=cycle, agent=agent, stream=stream, line=clean)
            if agent == "supervisor":
                return
            for pattern in self.forbidden:
                if pattern.search(clean):
                    note = f"{agent} (ciclo {cycle}) — padrão proibido /{pattern.pattern}/ em: {clean.strip()[:200]}"
                    self.state.warnings.append(note)
                    log("🚫", note, C.RED)
                    emit("forbidden", cycle=cycle, agent=agent, pattern=pattern.pattern, line=clean[:500],
                         abort=self.abort_on_forbidden)
                    if self.abort_on_forbidden:
                        self.abort_event.set()
                    break

        return on_line

    def _invoke(
        self,
        spec: dict[str, Any],
        prompt: str,
        timeout: int,
        label: str,
        on_line: Callable[[str, str], None] | None = None,
        agent: str | None = None,
        cycle: int = 0,
    ) -> CmdResult:
        """Monta o comando do backend escolhido e roda o agente."""
        prompt_file = self.workdir / f"{label}-prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        last_msg = self.workdir / f"{label}-last-message.txt"
        last_msg.unlink(missing_ok=True)

        if self.backend == "mco":
            mco = spec["mco"]
            cmd = [
                "mco", "run", "--repo", str(self.repo), "--providers", mco["provider"],
                "--file", str(prompt_file), "--execution-mode", mco.get("execution_mode", "read_only"),
                "--invocation-hard-timeout", str(timeout), "--quiet",
            ]
            return run_command(cmd, self.repo, timeout + 30, on_line=on_line, env=self.env, abort=self.abort_event)

        direct = spec["direct"]
        via = direct.get("prompt_via", "arg")
        arg_prompt = prompt
        limit = self.cfg.get("arg_prompt_limit_chars", 60000)
        if via == "arg" and len(prompt) > limit:
            # Linux recusa argumentos acima de 128 KiB: manda o agente ler o arquivo.
            arg_prompt = (
                f"As instruções completas desta tarefa estão no arquivo {prompt_file}. "
                "Leia o arquivo inteiro e siga-o exatamente, incluindo o formato de resposta pedido."
            )

        values = {
            "{prompt}": arg_prompt,
            "{prompt_file}": str(prompt_file),
            "{prompt_dir}": str(self.workdir),
            "{repo}": str(self.repo),
            "{last_message_file}": str(last_msg),
            "{timeout}": str(timeout),
        }
        cmd: list[str] = []
        for part in direct["cmd"]:
            if part == "{codex_mcp_off}":
                cmd.extend(self._codex_mcp_flags())
            else:
                cmd.append(self._fill(part, values))
        parser = StreamParser(direct["stream_format"], self.repo) if direct.get("stream_format") else None
        if parser:
            on_line = self._parsing_line_handler(parser, on_line, agent or label, cycle)
        result = run_command(
            cmd, self.repo, timeout, stdin_text=prompt if via == "stdin" else None, on_line=on_line,
            env=self.env, abort=self.abort_event,
        )

        if parser:
            result.stdout = parser.text()
            self._absorb(parser, agent or label)

        # Alguns CLIs (codex -o) gravam a resposta final limpa num arquivo,
        # separada do log de execução que sai no stdout.
        out_file = direct.get("output_file")
        if out_file:
            path = Path(self._fill(out_file, values))
            if path.exists() and path.read_text(encoding="utf-8").strip():
                result.stdout = path.read_text(encoding="utf-8")
        return result

    def _parsing_line_handler(
        self,
        parser: StreamParser,
        on_line: Callable[[str, str], None] | None,
        agent: str,
        cycle: int,
    ) -> Callable[[str, str], None]:
        """Com stream JSON, o que vai para a tela é a atividade legível, não o JSON."""

        def handle(stream: str, line: str) -> None:
            if stream != "stdout":
                if on_line:
                    on_line(stream, line)
                return
            for act in parser.feed(line):
                if on_line:
                    on_line("stdout", act.text if act.kind != "message" else f"💬 {act.text}")
                if act.kind != "message":
                    emit("agent_activity", cycle=cycle, agent=agent, kind=act.kind, text=act.text, **act.data)

        return handle

    def _absorb(self, parser: StreamParser, agent: str) -> None:
        """Guarda tokens, custo e a cota do claude informados pelo stream."""
        entry = self.state.stats.setdefault(agent, {"calls": 0, "seconds": 0.0})
        entry["tokens"] = int(entry.get("tokens", 0)) + parser.tokens
        if parser.cost_usd is not None:
            entry["cost_usd"] = round(float(entry.get("cost_usd", 0)) + parser.cost_usd, 4)
        if parser.limits:
            save_claude_limits(parser.limits)
            windows = claude_windows(parser.limits)
            emit("claude_limits", windows=windows)
        if parser.denied:
            self._warn(f"{agent}: ações negadas em modo não interativo ({', '.join(sorted(set(parser.denied)))}).")

    def _codex_mcp_flags(self) -> list[str]:
        """-c que desligam, só nesta execução, os MCP globais do codex (ex.: os
        da Hostinger): deixam cada chamada lenta e dariam acesso à produção."""
        if self._codex_mcp_off is None:
            self._codex_mcp_off = []
            for name in codex_mcp_servers():
                self._codex_mcp_off += ["-c", f"mcp_servers.{name}.enabled=false"]
        return self._codex_mcp_off

    @staticmethod
    def _fill(template: str, values: dict[str, str]) -> str:
        # str.replace em vez de str.format: prompts têm chaves de JSON e código.
        for key, val in values.items():
            template = template.replace(key, val)
        return template

    # — supervisor —

    def _history_text(self) -> str:
        if not self.state.steps:
            return "(nenhum passo executado ainda)"
        per_step = self.hist_cfg.get("step_output_chars", 6000)
        budget = self.hist_cfg.get("budget_chars", 60000)
        blocks: list[str] = []
        used = 0
        # Do mais recente para o mais antigo: os últimos passos ficam inteiros,
        # os antigos viram uma linha quando o orçamento acaba.
        for step in reversed(self.state.steps):
            status = "OK" if step.code == 0 else f"FALHOU (exit {step.code})"
            redirect = f" [redirecionado de {step.redirected_from}]" if step.redirected_from else ""
            full = (
                f"### Passo {step.cycle} — {step.agent}{redirect} — {status}\n"
                f"Motivo: {step.reason}\n"
                f"Prompt enviado:\n{clip(step.prompt, 1500)}\n"
                f"{self._files_line(step)}"
                f"Saída:\n{clip(step.output, per_step)}\n"
            )
            if used + len(full) > budget:
                full = f"### Passo {step.cycle} — {step.agent} — {status} (detalhes omitidos)\n"
            blocks.append(full)
            used += len(full)
        return "\n".join(reversed(blocks))

    @staticmethod
    def _files_line(step: Step) -> str:
        if not step.files:
            return "Arquivos alterados neste passo: nenhum\n" if step.tree_after else ""

        def describe(f: dict[str, Any]) -> str:
            counts = "" if f.get("added") is None else f" +{f['added']}/-{f['deleted']}"
            return f"{f['path']} ({f['status']}{counts})"

        names = ", ".join(describe(f) for f in step.files[:25])
        more = f" e mais {len(step.files) - 25}" if len(step.files) > 25 else ""
        return f"Arquivos alterados neste passo: {names}{more}\n"

    def _repo_state_block(self) -> str:
        """git status e diff --stat já prontos: o supervisor não consegue rodar
        comandos e, quando tenta, volta sem resposta."""
        status = run_command(["git", "status", "--short"], self.repo, 30)
        if status.code != 0:
            return ""
        stat = run_command(["git", "diff", "--stat", "HEAD"], self.repo, 30)
        gate = ""
        if self.gate_passed_tree:
            fresh = self.gate_passed_tree == snapshot_tree(self.repo)
            checks = ", ".join(self.gate_cfg.get("commands", [])) or "nenhuma"
            gate = (f"Verificações locais ({checks}): PASSARAM "
                    + ("no estado atual do código.\n" if fresh else "antes das últimas alterações.\n"))
        return (
            "## Estado atual do repositório (já coletado — não rode comandos)\n"
            f"git status --short:\n{clip(status.stdout.strip() or '(sem alterações)', 3000)}\n"
            f"git diff --stat HEAD:\n{clip(stat.stdout.strip() or '(vazio)', 2000)}\n{gate}\n"
        )

    def _ask_supervisor(self, cycle: int) -> dict[str, Any] | None:
        sup = self.cfg["supervisor"]
        remaining = self.last_cycle - cycle + 1
        claude_left = self.claude_policy.get("max_calls_per_run", 2) - self.state.claude_calls
        prompt = (
            f"{sup['system_prompt']}\n\n"
            f"## Tarefa do usuário\n{self.state.task}\n\n"
            f"{self._rules_block()}{self._notes_block()}"
            f"## Repositório\n{self.repo}\n\n"
            f"{self._repo_state_block()}"
            f"## Orçamento\nCiclos restantes (incluindo este): {remaining}. "
            f"Chamadas restantes ao claude: {max(0, claude_left)}.\n"
            f"Se restar só 1 ciclo, priorize deixar o código funcionando ou encerre com DONE.\n\n"
            f"## Histórico\n{self._history_text()}\n\n"
            "Responda agora APENAS com o JSON de decisão."
        )
        # Com reserva configurado, uma falha do principal já passa para ele: o
        # agy falha quase sempre pelo mesmo motivo (tentou rodar um comando,
        # negado no modo headless), então repetir só gastava mais 5 minutos.
        has_fallback = bool(self.cfg.get("supervisor_fallback")) and self.backend == "direct"
        retries = int(self.cfg.get("supervisor_retries", 0 if has_fallback else 1))
        for attempt in range(1, 2 + retries):
            log("🧭", f"Supervisor ({sup['agent']}) decidindo o próximo passo… (tentativa {attempt})", C.CYAN)
            emit("supervisor_start", cycle=cycle, attempt=attempt, agent=sup["agent"])
            res = self._invoke(
                sup, prompt, self.timeouts.get("supervisor", 300), f"c{cycle}-supervisor-{attempt}",
                on_line=self._streamer(cycle, "supervisor"), agent="supervisor", cycle=cycle,
            )
            self.state.count("supervisor", res.seconds)
            raw = extract_decision(res.stdout) or extract_decision(res.stderr)
            if raw:
                return normalize_decision(raw)
            log("⚠️ ", f"Resposta do supervisor sem JSON válido (exit {res.code}): {preview(res.stdout or res.stderr, 300)}", C.YELLOW)
            emit("supervisor_invalid", cycle=cycle, attempt=attempt, code=res.code,
                 preview=clip(res.stdout or res.stderr, 1000))
            if "ATENÇÃO: sua resposta anterior" not in prompt:
                prompt += "\n\nATENÇÃO: sua resposta anterior não continha o JSON. Responda SOMENTE com o objeto JSON."

        # O agy às vezes volta vazio (ex.: tentou rodar um comando, que é negado em
        # modo não interativo). Em vez de abortar, um supervisor reserva decide.
        fallback = self.cfg.get("supervisor_fallback")
        if fallback and self.backend == "direct":
            self._warn(f"Supervisor principal sem resposta válida no ciclo {cycle}; usando o reserva ({fallback.get('agent', '?')}).")
            emit("supervisor_start", cycle=cycle, attempt=3, agent=fallback.get("agent", "reserva"), fallback=True)
            res = self._invoke(
                fallback, prompt, self.timeouts.get("supervisor", 300), f"c{cycle}-supervisor-fallback",
                on_line=self._streamer(cycle, "supervisor"), agent="supervisor", cycle=cycle,
            )
            self.state.count("supervisor", res.seconds)
            raw = extract_decision(res.stdout) or extract_decision(res.stderr)
            if raw:
                return normalize_decision(raw)
        return None

    # — filtro de qualidade antes do claude —

    def _git_has_changes(self) -> bool | None:
        res = run_command(["git", "status", "--porcelain"], self.repo, 30)
        if res.code != 0:
            return None  # não é repositório git: não dá para exigir alterações
        return bool(res.stdout.strip())

    def _quality_gate(self, decision: dict[str, Any]) -> tuple[bool, str, str]:
        """Decide se o claude pode rodar. Retorna (liberado, alvo_se_bloqueado, motivo)."""
        policy = self.claude_policy
        if self.state.claude_calls >= policy.get("max_calls_per_run", 2):
            return False, "DONE", "Limite de chamadas ao claude nesta execução atingido."

        windows = claude_windows(load_claude_limits())
        for name, limit_key in (("seven_day", "max_weekly_utilization"), ("five_hour", "max_five_hour_utilization")):
            ceiling = policy.get(limit_key)
            used = windows.get(name, {}).get("utilization")
            if ceiling is not None and used is not None and used >= float(ceiling):
                when = datetime.fromtimestamp(windows[name]["resets_at"]).strftime("%d/%m %H:%M")
                label = "semanal" if name == "seven_day" else "de 5 horas"
                return False, "DONE", (
                    f"Cota {label} do claude em {used:.0%} (teto configurado {float(ceiling):.0%}, renova em {when}). "
                    "Revisão pulada para economizar."
                )

        if not self.gate_enabled:
            return True, "", ""

        if decision["claude_purpose"] == "diagnosis":
            needed = policy.get("min_codex_attempts_before_diagnosis", 2)
            if self.state.attempts("codex") < needed:
                return False, "codex", (
                    f"Diagnóstico pelo claude exige ao menos {needed} tentativas do codex "
                    f"(houve {self.state.attempts('codex')}). Tente resolver primeiro."
                )
            return True, "", ""

        if self.gate_cfg.get("require_git_changes", True):
            changed = self._git_has_changes()
            if changed is False:
                return False, "codex", "Não há alterações no git para revisar — nada foi implementado ainda."

        for command in self.gate_cfg.get("commands", []):
            log("🔎", f"Filtro pré-claude: {command}", C.YELLOW)
            emit("gate_check", command=command)
            res = run_command(command, self.repo, self.timeouts.get("check", 600), shell=True, env=self.env)
            output = clip(f"{res.stdout}\n{res.stderr}", 4000)
            emit("gate_result", command=command, ok=res.ok, code=res.code,
                 seconds=round(res.seconds, 1), output=output)
            if not res.ok:
                return False, "codex", f"A verificação local `{command}` falhou (exit {res.code}):\n{output}"
            log("✅", f"Passou: {command} ({res.seconds:.0f}s)", C.GREEN)
        if self.gate_cfg.get("commands"):
            self.gate_passed_tree = snapshot_tree(self.repo)
        return True, "", ""

    # — passos —

    def _notes_block(self) -> str:
        with self.notes_lock:
            notes = list(self.state.notes)
        if not notes:
            return ""
        items = "\n".join(f"- {n}" for n in notes)
        return f"## Notas do usuário durante a execução (prioridade máxima — siga-as)\n{items}\n\n"

    def _rules_block(self) -> str:
        rules = self.project.get("rules", "").strip()
        return f"## Regras deste projeto (obrigatórias)\n{rules}\n\n" if rules else ""

    def _worker_prompt(self, agent: str, instruction: str) -> str:
        role = self.cfg["agents"][agent]["role"]
        context = ""
        if self.state.steps:
            last = self.state.steps[-1]
            context = (
                f"\n\n## Resultado do passo anterior ({last.agent}, exit {last.code})\n"
                f"{clip(last.output, self.hist_cfg.get('worker_context_chars', 3000))}"
            )
        return (
            f"{role}\n\n{self._rules_block()}{self._notes_block()}## Objetivo geral do usuário\n{self.state.task}\n\n"
            f"## Sua tarefa agora\n{instruction}{context}"
        )

    def _run_worker(self, cycle: int, agent: str, reason: str, instruction: str, redirected_from: str | None) -> Step:
        color = AGENT_COLOR.get(agent, "")
        log("🤖", f"{C.BOLD}{agent}{C.RESET}{color} em execução — {reason}", color)
        log("📝", f"Prompt: {preview(instruction, 400)}", C.DIM)
        emit("agent_start", cycle=cycle, agent=agent, reason=reason, prompt=instruction,
             redirected_from=redirected_from)
        spec = self.cfg["agents"][agent]
        timeout = self.timeouts.get(agent, 900)
        res = self._invoke(
            spec, self._worker_prompt(agent, instruction), timeout, f"c{cycle}-{agent}",
            on_line=self._streamer(cycle, agent), agent=agent, cycle=cycle,
        )
        output = res.stdout.strip() or res.stderr.strip()
        if res.code != 0 and res.stderr.strip() and res.stdout.strip():
            output = f"{res.stdout.strip()}\n\n[stderr]\n{res.stderr.strip()}"

        if res.ok:
            log("✅", f"{agent} concluiu em {res.seconds:.0f}s", C.GREEN)
        else:
            why = "tempo esgotado" if res.timed_out else f"exit {res.code}"
            log("❌", f"{agent} falhou ({why}) em {res.seconds:.0f}s", C.RED)
        _print(f"{C.DIM}{preview(output, 900)}{C.RESET}\n")

        before = self.last_tree
        after = snapshot_tree(self.repo) if before else None
        files = tree_changes(self.repo, before, after) if before and after else []
        if after:
            self.last_tree = after
        if files:
            log("📝", f"{len(files)} arquivo(s) alterado(s) neste passo: "
                + ", ".join(f["path"] for f in files[:6]) + ("…" if len(files) > 6 else ""), C.DIM)

        emit("agent_end", cycle=cycle, agent=agent, code=res.code, timed_out=res.timed_out,
             seconds=round(res.seconds, 1), output=clip(output, 20000),
             tree_before=before, tree_after=after, files=files)

        if agent == "claude":
            self.state.claude_calls += 1
        self.state.count(agent, res.seconds)
        return Step(cycle, agent, reason, instruction, res.code, output, round(res.seconds, 1), redirected_from,
                    tree_before=before, tree_after=after, files=files)

    # — laço principal —

    def run(self) -> int:
        banner(f"Orquestrador · backend={self.backend} · repo={self.repo}")
        log("🎯", f"Tarefa: {self.state.task}", C.BOLD)
        emit("run_start", task=self.state.task, repo=str(self.source_repo), backend=self.backend,
             max_loops=self.max_loops, gate=self.gate_cfg if self.gate_enabled else None,
             project={
                 "config": self.state.project_config,
                 "checks": self.gate_cfg.get("commands", []),
                 "rules": bool(self.project.get("rules")),
                 "env_overrides": sorted(self.project.get("env", {})),
                 "forbidden_patterns": [p.pattern for p in self.forbidden],
                 "on_forbidden": "abort" if self.abort_on_forbidden else "warn",
                 "isolation": self.isolation_mode,
             },
             approve=self.approve_mode, resumed_from=self.state.resumed_from,
             claude_limits=claude_windows(load_claude_limits()),
             previous_steps=[
                 {**asdict(step), "output": clip(step.output, 3000), "prompt": clip(step.prompt, 3000)}
                 for step in self.state.steps
             ],
             notes=self.state.notes)

        if self.control:
            self.control.start()
        if self.node_note.startswith("Node") and "não resolvido" in self.node_note:
            self._warn(self.node_note)
        elif self.node_note:
            log("🟢", f"Usando {self.node_note}", C.DIM)
        try:
            ready = self._resume_workspace() if self.state.isolation else self._prepare_workspace()
            if not ready:
                return self._finish()
            self.last_tree = snapshot_tree(self.repo)
            for cycle in range(self.first_cycle, self.last_cycle + 1):
                banner(f"Ciclo {cycle}/{self.last_cycle}")
                emit("cycle_start", cycle=cycle, max=self.last_cycle)
                if self.control and self.control.stop.is_set():
                    self.state.status = "stopped"
                    self.state.final_summary = "Parado pelo usuário."
                    break
                decision = self._ask_supervisor(cycle)
                if decision is None:
                    self.state.status = "aborted"
                    self.state.final_summary = "Supervisor não devolveu uma decisão em JSON após 2 tentativas."
                    log("🛑", self.state.final_summary, C.RED)
                    break

                agent = decision["next_agent"]
                log("🧭", f"Decisão: {C.BOLD}{agent}{C.RESET}{C.CYAN} — {decision['reason']}", C.CYAN)
                emit("decision", cycle=cycle, **decision)

                if agent == "DONE":
                    self.state.status = "done"
                    self.state.final_summary = decision["summary"] or decision["reason"]
                    break
                if agent not in WORKERS:
                    log("⚠️ ", f"Agente desconhecido '{agent}'; tratado como codex.", C.YELLOW)
                    agent = "codex"
                if not decision["prompt"]:
                    decision["prompt"] = f"Continue a tarefa do usuário. Motivo do supervisor: {decision['reason']}"

                redirected_from = None
                if agent == "claude":
                    allowed, target, why = self._quality_gate(decision)
                    if not allowed:
                        log("💸", f"claude bloqueado para economizar cota → {target}. {why.splitlines()[0]}", C.YELLOW)
                        emit("gate_blocked", cycle=cycle, target=target, reason=why)
                        if target == "DONE":
                            self.state.status = "done"
                            self.state.final_summary = f"{why} Último motivo do supervisor: {decision['reason']}"
                            break
                        redirected_from, agent = "claude", target
                        decision["reason"] = "Filtro de qualidade local barrou o claude"
                        decision["prompt"] = (
                            f"{why}\n\nCorrija o problema acima no repositório e confirme que a verificação passa. "
                            f"Contexto do que seria revisado: {decision['prompt']}"
                        )

                if self._needs_approval():
                    verdict, payload = self._wait_approval(cycle, agent, decision)
                    if verdict == "stop":
                        self.state.status = "stopped"
                        self.state.final_summary = "Parado pelo usuário na aprovação do passo."
                        break
                    if verdict == "reject":
                        note = f"Passo recusado pelo usuário{': ' + payload if payload else ''}."
                        self.state.steps.append(
                            Step(cycle, agent, decision["reason"], decision["prompt"], -1, note, 0.0, redirected_from)
                        )
                        log("✋", note, C.YELLOW)
                        continue
                    decision["prompt"] = payload or decision["prompt"]

                step = self._run_worker(cycle, agent, decision["reason"], decision["prompt"], redirected_from)
                self.state.steps.append(step)
                if self._approved_by_review(step):
                    self.state.status = "done"
                    self.state.final_summary = (
                        "Revisão do claude: APROVADO, sobre o mesmo código em que "
                        f"{', '.join(self.gate_cfg['commands'])} passaram.\n\n{clip(step.output, 3000)}"
                    )
                    log("🏁", "Revisão aprovada com as verificações em dia — encerrando sem novo ciclo.", C.GREEN)
                    emit("decision", cycle=cycle, next_agent="DONE", reason="Revisão do claude aprovada",
                         claude_purpose=None, prompt="", summary=self.state.final_summary, auto=True)
                    break
                if self.abort_event.is_set():
                    self.state.status = "blocked"
                    self.state.final_summary = (
                        "Execução interrompida: um agente rodou um comando que bate com forbidden_patterns "
                        f"do {PROJECT_CONFIG_NAME}. Revise as alterações antes de aceitar."
                    )
                    log("🛑", self.state.final_summary, C.RED)
                    break
            else:
                self.state.status = "max_loops"
                self.state.final_summary = (
                    f"Limite de {self.max_loops} ciclos atingido antes do DONE. Dá para continuar de onde parou (--resume)."
                )
        except KeyboardInterrupt:
            self.state.status = "interrupted"
            self.state.final_summary = "Interrompido pelo usuário (Ctrl+C)."
            _print("")

        return self._finish()

    def _approved_by_review(self, step: Step) -> bool:
        """Revisão do claude com VEREDITO: APROVADO sobre a árvore em que as
        verificações passaram — sem isso o supervisor mandava alguém
        "validar tudo de novo" num ciclo inteiro a mais."""
        return (
            step.agent == "claude" and step.code == 0
            and self.gate_passed_tree is not None
            and step.tree_after == self.gate_passed_tree
            and review_verdict(step.output) == "APROVADO"
        )

    def _needs_approval(self) -> bool:
        if self.approve_mode == "all":
            return True
        return self.approve_mode == "first" and self.approvals_done == 0

    def _wait_approval(self, cycle: int, agent: str, decision: dict[str, Any]) -> tuple[str, str]:
        """Pausa até a extensão aprovar, recusar ou parar o passo."""
        assert self.control is not None
        log("✋", f"Aguardando aprovação: {agent} — {decision['reason']}", C.YELLOW)
        emit("awaiting_approval", cycle=cycle, agent=agent, reason=decision["reason"], prompt=decision["prompt"])
        while True:
            try:
                cmd = self.control.decisions.get(timeout=0.5)
            except queue.Empty:
                continue
            kind = cmd.get("cmd")
            emit("approval_result", cycle=cycle, verdict=kind)
            if kind == "approve":
                self.approvals_done += 1
                return "approve", str(cmd.get("prompt") or "").strip()
            if kind == "reject":
                return "reject", str(cmd.get("reason") or "").strip()
            return "stop", ""

    def _resume_workspace(self) -> bool:
        """Na retomada, volta para a worktree da execução anterior (ou a recria)."""
        iso = self.state.isolation
        assert iso is not None
        branch, path = iso["branch"], Path(iso["worktree"])
        if not git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], self.source_repo, 30).ok:
            self._warn(f"A branch {branch} não existe mais (aceita ou descartada); começando numa worktree nova.")
            self.state.isolation = None
            return self._prepare_workspace()
        if not path.exists():
            added = git(["worktree", "add", str(path), branch], self.source_repo, 300)
            if not added.ok:
                self.state.status = "setup_failed"
                self.state.final_summary = f"Não consegui recriar a worktree: {clip(added.stderr, 1500)}"
                return False
            for command in self.project.get("setup", []):
                res = run_command(command, path, self.timeouts.get("check", 600), shell=True, env=self.env)
                if not res.ok:
                    self.state.status = "setup_failed"
                    self.state.final_summary = f"O setup `{command}` falhou na retomada (exit {res.code})."
                    return False
        self.repo = path
        log("🌿", f"Retomando na worktree {path} (branch {branch})", C.CYAN)
        emit("isolation_ready", **iso, resumed=True)
        return True

    def _warn(self, message: str) -> None:
        self.state.warnings.append(message)
        log("⚠️ ", message, C.YELLOW)
        emit("warning", message=message)

    def _prepare_workspace(self) -> bool:
        """Cria a worktree isolada (se configurado), copia arquivos e roda o setup."""
        if self.isolation_mode != "worktree":
            return True
        if git_root(self.source_repo) is None:
            self._warn("Isolamento por worktree pedido, mas a pasta não é um repositório git — rodando direto nela.")
            self.isolation_mode = "none"
            return True
        head = git(["rev-parse", "HEAD"], self.source_repo, 30)
        if not head.ok:
            self._warn("O repositório não tem nenhum commit — rodando sem isolamento.")
            self.isolation_mode = "none"
            return True
        dirty = [l for l in git(["status", "--porcelain"], self.source_repo, 30).stdout.splitlines()
                 if l[3:].strip() != PROJECT_CONFIG_NAME]
        if dirty:
            self._warn("Há alterações não commitadas no repositório: a worktree parte do último commit e não as inclui.")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = f"orch/{stamp}"
        path = WORKTREES_DIR / f"{self.source_repo.name}-{stamp}"
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        added = git(["worktree", "add", "-b", branch, str(path), head.stdout.strip()], self.source_repo, 300)
        if not added.ok:
            self.state.status = "setup_failed"
            self.state.final_summary = f"Não consegui criar a worktree: {clip(added.stderr, 1500)}"
            log("🛑", self.state.final_summary, C.RED)
            return False

        self.state.isolation = {
            "branch": branch, "worktree": str(path), "base": head.stdout.strip(),
            "source_repo": str(self.source_repo), "pending": True,
        }
        self.repo = path
        log("🌿", f"Worktree isolada: {path} (branch {branch})", C.CYAN)
        emit("isolation_ready", **self.state.isolation)

        for rel in self.project.get("copy_files", []):
            src, dst = self.source_repo / rel, path / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                log("📄", f"Copiado para a worktree: {rel}", C.DIM)
            else:
                self._warn(f"copy_files: {rel} não existe no repositório, ignorado.")

        for command in self.project.get("setup", []):
            log("🔧", f"Setup: {command}", C.YELLOW)
            emit("gate_check", command=command, phase="setup")
            res = run_command(command, self.repo, self.timeouts.get("check", 600), shell=True, env=self.env)
            output = clip(f"{res.stdout}\n{res.stderr}", 4000)
            emit("gate_result", command=command, phase="setup", ok=res.ok, code=res.code,
                 seconds=round(res.seconds, 1), output=output)
            if not res.ok:
                self.state.status = "setup_failed"
                self.state.final_summary = f"O setup `{command}` falhou (exit {res.code}):\n{output}"
                log("🛑", f"Setup falhou: {command}", C.RED)
                return False
        return True

    def _close_isolation(self) -> list[dict[str, str]]:
        """Commita o trabalho dos agentes na branch da worktree e lista o que mudou.

        "Mudou" é sempre em relação ao commit base: numa retomada, o trabalho
        dos ciclos anteriores já está commitado na branch e não pode se perder
        só porque os ciclos novos não alteraram nada.
        """
        iso = self.state.isolation
        assert iso is not None
        git(["add", "-A"], self.repo, 120)
        if git(["diff", "--cached", "--quiet"], self.repo, 60).code != 0:
            title = self.state.task.strip().splitlines()[0][:72]
            # Commit interno na branch descartável: --no-verify porque os hooks do
            # projeto rodam quando você commitar o resultado aceito.
            git(["-c", "user.name=agent-orchestrator", "-c", "user.email=orchestrator@localhost",
                 "commit", "--no-verify", "-q", "-m", f"orch: {title}"], self.repo, 120)
        iso["commit"] = git(["rev-parse", "HEAD"], self.repo, 30).stdout.strip()
        changes = branch_changes(self.source_repo, iso["base"], iso["branch"])
        if not changes:
            remove_worktree(self.source_repo, iso["branch"])
            iso["pending"] = False
            iso["note"] = "Nenhuma alteração: worktree removida."
            log("🧹", iso["note"], C.DIM)
        return changes

    def _changed_files(self) -> list[dict[str, str]]:
        """Arquivos alterados no repositório (git status), para a extensão abrir o diff."""
        res = run_command(["git", "status", "--porcelain", "--untracked-files=all"], self.repo, 30)
        if res.code != 0:
            return []
        files = []
        for line in res.stdout.splitlines():
            if len(line) > 3:
                path = line[3:].split(" -> ")[-1].strip().strip('"')
                files.append({"status": line[:2].strip(), "path": path})
        return files

    def _finish(self) -> int:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.json"
        log_path.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(self.workdir, ignore_errors=True)

        changed = self._close_isolation() if self.state.isolation else self._changed_files()
        # Grava o log depois de fechar a worktree, para ele já trazer o commit.
        log_path.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")
        emit("run_end", status=self.state.status, summary=self.state.final_summary,
             claude_calls=self.state.claude_calls, steps=len(self.state.steps),
             log_path=str(log_path), changed_files=changed, isolation=self.state.isolation,
             warnings=self.state.warnings, stats=self.state.stats)

        banner("Resultado")
        color = C.GREEN if self.state.status == "done" else C.YELLOW
        log("🏁", f"Status: {self.state.status} · {len(self.state.steps)} passo(s) · claude usado {self.state.claude_calls}x", color)
        for s in self.state.steps:
            mark = "✅" if s.code == 0 else "❌"
            extra = f" (no lugar de {s.redirected_from})" if s.redirected_from else ""
            _print(f"   {mark} ciclo {s.cycle}: {AGENT_COLOR.get(s.agent, '')}{s.agent}{C.RESET}{extra} — {s.reason} [{s.seconds:.0f}s]")
        if self.state.final_summary:
            _print(f"\n{C.BOLD}Resumo:{C.RESET}\n{self.state.final_summary}\n")
        for warning in self.state.warnings:
            log("⚠️ ", warning, C.YELLOW)
        iso = self.state.isolation
        if iso and iso.get("pending"):
            log("🌿", f"Alterações na branch {iso['branch']} ({len(changed)} arquivo(s)).", C.CYAN)
            log("👉", f"Aceitar:   python3 {Path(__file__).resolve()} --accept {iso['branch']} --repo {self.source_repo}", C.BOLD)
            log("👉", f"Descartar: python3 {Path(__file__).resolve()} --discard {iso['branch']} --repo {self.source_repo}", C.BOLD)
        log("💾", f"Log completo: {log_path}", C.DIM)
        return 0 if self.state.status == "done" else 1


# ─── Diagnóstico do ambiente (--doctor) ──────────────────────────────────────


def _version_tuple(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x) for x in m.groups() if x is not None) if m else ()


def doctor(config: dict[str, Any], repo: Path, backend: str) -> list[dict[str, Any]]:
    """Checa o ambiente sem chamar nenhum modelo (não gasta cota)."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str, hint: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "hint": hint})

    py = sys.version_info
    add("Python", py >= (3, 9), f"{py.major}.{py.minor}.{py.micro}", "Precisa de Python 3.9+.")

    res = run_command(["git", "--version"], repo, 20)
    gv = _version_tuple(res.stdout)
    add("git", res.ok and gv >= (2, 38), res.stdout.strip() or res.stderr.strip(),
        "Aceitar alterações usa `git merge-tree --write-tree` (git 2.38+).")

    binaries: dict[str, str] = {}
    if backend == "mco":
        binaries["mco"] = "mco"
    else:
        binaries[f"supervisor ({config['supervisor'].get('agent', 'agy')})"] = config["supervisor"]["direct"]["cmd"][0]
        for name, spec in config["agents"].items():
            binaries[name] = spec["direct"]["cmd"][0]
        fb = config.get("supervisor_fallback")
        if fb:
            binaries["supervisor reserva"] = fb["direct"]["cmd"][0]
    for label, binary in binaries.items():
        path = shutil.which(binary)
        if not path:
            add(label, False, f"`{binary}` não está no PATH", "Instale a CLI ou ajuste o comando no orchestrator.json.")
            continue
        ver = run_command([binary, "--version"], repo, 20)
        add(label, True, f"{path} · {(ver.stdout or ver.stderr).strip().splitlines()[0] if (ver.stdout or ver.stderr).strip() else 'versão desconhecida'}")

    root = git_root(repo)
    if root is None:
        add("Repositório", None, f"{repo} não é um repositório git", "Sem git não há isolamento por worktree nem diff por passo.")
    else:
        head = git(["rev-parse", "--short", "HEAD"], root, 20)
        add("Repositório", head.ok, f"{root} @ {head.stdout.strip() or 'sem commits'}",
            "" if head.ok else "Faça o primeiro commit: a worktree parte do HEAD.")
        try:
            project, project_path = load_project_config(root)
        except SystemExit as exc:
            add("Configuração do projeto", False, str(exc))
        else:
            if project_path:
                missing = sorted({c.split()[0] for c in [*project.get("setup", []), *project.get("checks", [])]
                                  if c.split() and not shutil.which(c.split()[0])})
                detail = (f"{len(project.get('checks', []))} verificação(ões), {len(project.get('templates', []))} modelo(s), "
                          f"isolamento {project.get('isolation', config.get('isolation', 'worktree'))}")
                add("Configuração do projeto", not missing, detail,
                    f"Comandos não encontrados no PATH: {', '.join(missing)}" if missing else "")
                if project.get("node"):
                    bin_dir, err = resolve_node_bin(project["node"])
                    add(f"Node {project['node']} (projeto)", bin_dir is not None,
                        str(bin_dir) if bin_dir else err,
                        "" if bin_dir else f"Instale com: fnm install {project['node']}")
            else:
                add("Configuração do projeto", None, "sem .orchestrator.json",
                    "Opcional: defina verificações, variáveis bloqueadas e modelos para este projeto.")

    try:
        WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
        probe = WORKTREES_DIR / ".probe"
        probe.write_text("ok")
        probe.unlink()
        add("Pasta das worktrees", True, str(WORKTREES_DIR))
    except OSError as exc:
        add("Pasta das worktrees", False, f"{WORKTREES_DIR}: {exc}")

    windows = claude_windows(load_claude_limits())
    policy = config.get("claude_policy", {})
    if windows:
        parts, over = [], False
        for name, key, label in (("five_hour", "max_five_hour_utilization", "5h"), ("seven_day", "max_weekly_utilization", "semana")):
            if name in windows:
                used = windows[name]["utilization"]
                ceiling = policy.get(key)
                over = over or (ceiling is not None and used >= float(ceiling))
                parts.append(f"{label} {used:.0%}" + (f" (teto {float(ceiling):.0%})" if ceiling is not None else ""))
        add("Cota do claude", not over, " · ".join(parts),
            "Acima do teto, a revisão do claude é pulada até a janela renovar." if over else "")
    else:
        add("Cota do claude", None, "desconhecida", "Aparece depois da primeira chamada ao claude.")

    agy_settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
    allowed: list[str] = []
    try:
        allowed = list(json.loads(agy_settings.read_text(encoding="utf-8")).get("permissions", {}).get("allow", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    add("agy: comandos de terminal", None,
        f"{len(allowed)} regra(s) em permissions.allow" if allowed else "nenhum liberado (são negados em modo não interativo)",
        "" if allowed else "O agy só lê e edita arquivos; as verificações são rodadas pelo orquestrador.")
    return checks


# ─── CLI ─────────────────────────────────────────────────────────────────────


def load_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"Configuração não encontrada: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"orchestrator.json inválido ({path}): {exc}")


def check_binaries(config: dict[str, Any], backend: str) -> list[str]:
    if backend == "mco":
        return [] if shutil.which("mco") else ["mco"]
    needed = {config["supervisor"]["direct"]["cmd"][0]}
    needed |= {spec["direct"]["cmd"][0] for spec in config["agents"].values()}
    return sorted(b for b in needed if not shutil.which(b))


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Orquestra agy (supervisor/frontend), codex (backend/testes) e claude (revisão).",
    )
    p.add_argument("task", nargs="?", help="instrução em linguagem natural")
    p.add_argument("--repo", default=os.getcwd(), help="repositório de trabalho (padrão: pasta atual)")
    p.add_argument("--config", default=os.environ.get("ORCH_CONFIG", str(DEFAULT_CONFIG)))
    p.add_argument("--backend", choices=("direct", "mco"), help="direct = binários; mco = wrapper mco (ou ORCH_BACKEND)")
    p.add_argument("--max-loops", type=int, help="limite de ciclos (padrão do JSON: 6)")
    p.add_argument("--timeout", type=int, help="timeout por agente em segundos (sobrepõe o JSON)")
    p.add_argument("--check", action="append", help="comando de verificação antes do claude (repetível)")
    p.add_argument("--no-gate", action="store_true", help="desliga o filtro pré-claude (gasta mais cota)")
    p.add_argument("--dry-run", action="store_true", help="mostra a configuração resolvida e sai")
    p.add_argument("--events", choices=("jsonl",), help="emite eventos JSON no stdout (usado pela extensão do editor)")
    p.add_argument("--isolation", choices=("worktree", "none"), help="worktree = agentes numa branch isolada (padrão)")
    p.add_argument("--accept", metavar="BRANCH", help="aplica em stage as alterações de uma branch orch/… e a remove")
    p.add_argument("--discard", metavar="BRANCH", help="descarta uma branch orch/… e a worktree dela")
    p.add_argument("--resume", metavar="LOG", help="continua uma execução salva (arquivo de ~/.cache/agent-orchestrator/runs)")
    p.add_argument("--control", choices=("stdin",), help="aceita comandos JSON no stdin (notas, aprovação) — usado pela extensão")
    p.add_argument("--doctor", action="store_true", help="checa o ambiente (CLIs, git, projeto, cota) sem gastar cota")
    p.add_argument("--approve", choices=("none", "first", "all"), help="pausa para aprovar o passo: só o primeiro ou todos (exige --control)")
    args = p.parse_args(argv)
    if not (args.task or args.accept or args.discard or args.resume or args.doctor):
        p.error("informe a tarefa (ou --accept/--discard BRANCH, ou --resume LOG)")
    if args.approve in ("first", "all") and args.control != "stdin":
        p.error("--approve precisa de --control stdin (a aprovação chega por ele)")
    return args


def _raise_interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.events:
        enable_events()
    # O botão Parar da extensão manda SIGTERM: vira o mesmo caminho do Ctrl+C,
    # que encerra o agente em execução (e os filhos dele) antes de sair.
    signal.signal(signal.SIGTERM, _raise_interrupt)
    if args.accept or args.discard:
        branch = args.accept or args.discard
        if not branch.startswith("orch/"):
            sys.exit("Só branches criadas pelo orquestrador (orch/…) podem ser aceitas ou descartadas.")
        fn = accept_branch if args.accept else discard_branch
        ok, message = fn(Path(args.repo).resolve(), branch)
        print(json.dumps({"ok": ok, "message": message}, ensure_ascii=False))
        return 0 if ok else 1

    if args.resume:
        try:
            args.resume_data = json.loads(Path(args.resume).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.exit(f"Não consegui ler o log para retomar ({args.resume}): {exc}")
        args.task = args.resume_data.get("task") or args.task
        if not any(a == "--repo" or a.startswith("--repo=") for a in (argv if argv is not None else sys.argv[1:])):
            args.repo = args.resume_data.get("repo") or args.repo

    config = load_config(Path(args.config))
    if args.doctor:
        backend = args.backend or os.environ.get("ORCH_BACKEND") or config.get("backend", "direct")
        checks = doctor(config, Path(args.repo).resolve(), backend)
        if args.events:
            print(json.dumps({"type": "doctor", "checks": checks}, ensure_ascii=False))
        else:
            for c in checks:
                mark = {True: f"{C.GREEN}✔{C.RESET}", False: f"{C.RED}✘{C.RESET}", None: f"{C.YELLOW}•{C.RESET}"}[c["ok"]]
                print(f"{mark} {C.BOLD}{c['name']}{C.RESET}: {c['detail']}")
                if c["hint"] and c["ok"] is not True:
                    print(f"    {C.DIM}{c['hint']}{C.RESET}")
        return 0 if all(c["ok"] is not False for c in checks) else 1
    if args.max_loops is not None and args.max_loops < 1:
        sys.exit("--max-loops precisa ser >= 1")

    orch = Orchestrator(config, args)
    missing = check_binaries(config, orch.backend)

    if args.dry_run:
        print(json.dumps({
            "backend": orch.backend, "repo": str(orch.repo), "max_loops": orch.max_loops,
            "timeouts": orch.timeouts, "quality_gate": orch.gate_cfg if orch.gate_enabled else "desligado",
            "claude_policy": orch.claude_policy, "binarios_ausentes": missing,
            "isolation": orch.isolation_mode, "project_config": orch.state.project_config,
            "env_overrides": sorted(orch.project.get("env", {})),
            "forbidden_patterns": [p.pattern for p in orch.forbidden],
        }, ensure_ascii=False, indent=2))
        shutil.rmtree(orch.workdir, ignore_errors=True)
        return 0

    if missing:
        shutil.rmtree(orch.workdir, ignore_errors=True)
        message = f"Binário(s) não encontrado(s) no PATH: {', '.join(missing)}"
        emit("error", message=message)
        sys.exit(message)
    return orch.run()


if __name__ == "__main__":
    sys.exit(main())
