#!/usr/bin/env python3
"""Agente falso para os testes: segue um roteiro em vez de chamar um modelo.

Uso (no "cmd" da configuração de teste):
    python3 fake_agent.py <nome> [argumentos que a CLI real receberia...]

Lê o roteiro de $FAKE_DIR/<nome>.json — uma lista de ações, uma por chamada
(a última se repete quando a lista acaba):
    {"out": "texto no stdout", "err": "texto no stderr", "exit": 0,
     "write": {"arquivo.txt": "conteúdo"}, "append": {"a.txt": "linha\\n"}, "sleep": 0}

Cada chamada é registrada em $FAKE_DIR/<nome>.calls.jsonl com argumentos,
stdin, pasta e o node do PATH, para os testes conferirem o que o
orquestrador mandou.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path


def main() -> int:
    name, args = sys.argv[1], sys.argv[2:]
    base = Path(os.environ["FAKE_DIR"])
    script = json.loads((base / f"{name}.json").read_text(encoding="utf-8"))
    counter = base / f"{name}.count"
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    action = script[min(n, len(script) - 1)]

    stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    with (base / f"{name}.calls.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"args": args, "stdin": stdin, "cwd": os.getcwd(), "node": shutil.which("node")}) + "\n")

    for rel, content in action.get("write", {}).items():
        Path(rel).parent.mkdir(parents=True, exist_ok=True)
        Path(rel).write_text(content, encoding="utf-8")
    for rel, content in action.get("append", {}).items():
        with Path(rel).open("a", encoding="utf-8") as fh:
            fh.write(content)
    if action.get("sleep"):
        time.sleep(float(action["sleep"]))
    sys.stdout.write(action.get("out", ""))
    sys.stderr.write(action.get("err", ""))
    return int(action.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
