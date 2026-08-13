#!/usr/bin/env python3
"""Inverted coding harness.

The model lives in a persistent kernel. It emits XML syscalls, not tool RPCs.
It can grow new tags, rewrite tool descriptions, and edit its own system notes.
Those changes are the next turn's prompt. Frozen: scan, step, complete, and
the five builtin tags.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import re
import subprocess
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TAG_OPEN = re.compile(r"<([A-Za-z_][\w.-]*)((?:\s+[^>]*?)?)>", re.S)
ATTR = re.compile(r'([A-Za-z_][\w.-]*)\s*=\s*"([^"]*)"')
FROZEN = frozenset({"python", "bash", "register", "system", "tool"})
RESULT_CAP = 8000
BASH_TIMEOUT = 60

ABI = """You are a coding agent in a persistent Python kernel, working in the user's cwd.
Text is speech. XML tags are syscalls.

<python>code</python>
exec in the kernel. stdout and the last expression come back. Names persist.

<bash>command</bash>
run a shell command in cwd.

<register name="tag" doc="one-line description">
def handle(body, **attrs):
    ...
</register>
install a new syscall. Then emit <tag attr="v">body</tag>.

<system name="id">note</system>
write a system note. It is injected every later turn. Use this for doctrine,
style, and anything you want your future self to see.
<system name="id" delete="1"/>
drop a note.

<tool name="tag" doc="description"/>
rewrite a tool's description, including builtins. The catalog is the prompt.

Grow whatever you need. Fix a description when it is wrong. When the task is
done, speak without XML."""


@dataclass
class Block:
    tag: str
    body: str
    attrs: dict[str, str]


@dataclass
class Tool:
    name: str
    doc: str
    source: str | None = None
    handler: Callable[..., Any] | None = None
    frozen: bool = False


@dataclass
class World:
    ns: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Tool] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    cwd: Path = field(default_factory=lambda: Path.cwd())
    state_path: Path | None = None


def scan(text: str) -> list[Block]:
    blocks: list[Block] = []
    pos = 0
    while True:
        m = TAG_OPEN.search(text, pos)
        if not m:
            break
        tag, raw_attrs = m.group(1), m.group(2) or ""
        if raw_attrs.rstrip().endswith("/"):
            attrs = {k: v for k, v in ATTR.findall(raw_attrs)}
            blocks.append(Block(tag, "", attrs))
            pos = m.end()
            continue
        close = f"</{tag}>"
        end = text.find(close, m.end())
        if end < 0:
            pos = m.end()
            continue
        attrs = {k: v for k, v in ATTR.findall(raw_attrs)}
        blocks.append(Block(tag, text[m.end() : end], attrs))
        pos = end + len(close)
    return blocks


def _clip(text: str, cap: int = RESULT_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 24] + f"\n…[{len(text) - cap + 24} chars clipped]"


def _run_python(body: str, ns: dict[str, Any]) -> str:
    src = body.strip()
    if not src:
        return "(empty)"
    buf = io.StringIO()
    try:
        tree = ast.parse(src)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if not tree.body:
                return "ok"
            *head, last = tree.body
            if head:
                exec(compile(ast.Module(head, []), "<python>", "exec"), ns)
            if isinstance(last, ast.Expr):
                val = eval(compile(ast.Expression(last.value), "<python>", "eval"), ns)
                out = buf.getvalue()
                extra = "" if val is None else repr(val)
                return _clip((out + extra).strip() or "ok")
            exec(compile(ast.Module([last], []), "<python>", "exec"), ns)
        return _clip(buf.getvalue().strip() or "ok")
    except Exception:
        return _clip((buf.getvalue() + traceback.format_exc()).strip())


def _run_bash(body: str, cwd: Path) -> str:
    cmd = body.strip()
    if not cmd:
        return "(empty)"
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"timeout after {BASH_TIMEOUT}s"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode:
        return _clip(f"exit {proc.returncode}\n{out}".strip())
    return _clip(out.strip() or "ok")


def _callable_from_source(world: World, source: str, name: str) -> Callable[..., Any]:
    local: dict[str, Any] = {}
    exec(compile(source, f"<register:{name}>", "exec"), world.ns, local)
    fn = local.get("handle") or world.ns.get("handle")
    if not callable(fn):
        for v in local.values():
            if callable(v):
                fn = v
                break
    if not callable(fn):
        raise ValueError("no callable handle")
    world.ns[f"handle_{name}"] = fn
    return fn


def _register(world: World, body: str, name: str, doc: str) -> str:
    if not name or not name.isidentifier():
        return f"register failed: bad name {name!r}"
    if name in FROZEN:
        return f"register failed: {name} is frozen"
    try:
        fn = _callable_from_source(world, body, name)
    except Exception:
        return traceback.format_exc()
    world.tools[name] = Tool(name=name, doc=doc or f"user tag <{name}>", source=body, handler=fn)
    save(world)
    return f"registered <{name}>"


def _system(world: World, body: str, name: str, delete: bool) -> str:
    if not name:
        return "system failed: name required"
    if delete:
        existed = world.notes.pop(name, None)
        save(world)
        return f"deleted note {name}" if existed is not None else f"no note {name}"
    world.notes[name] = body.strip()
    save(world)
    return f"wrote note {name} ({len(world.notes[name])} chars)"


def _tool_doc(world: World, name: str, doc: str) -> str:
    if name not in world.tools:
        return f"unknown tool {name!r}"
    if not doc.strip():
        return "tool failed: doc required"
    world.tools[name].doc = doc.strip()
    save(world)
    return f"updated <{name}> doc"


def dispatch(world: World, block: Block) -> str:
    if block.tag == "python":
        return _run_python(block.body, world.ns)
    if block.tag == "bash":
        return _run_bash(block.body, world.cwd)
    if block.tag == "register":
        return _register(world, block.body, block.attrs.get("name", ""), block.attrs.get("doc", ""))
    if block.tag == "system":
        delete = block.attrs.get("delete", "") in {"1", "true", "yes"}
        return _system(world, block.body, block.attrs.get("name", ""), delete)
    if block.tag == "tool":
        return _tool_doc(world, block.attrs.get("name", ""), block.attrs.get("doc", "") or block.body)
    tool = world.tools.get(block.tag)
    if tool is None or tool.handler is None:
        return f"unknown tag <{block.tag}> — register it first"
    try:
        return _clip(str(tool.handler(block.body, **block.attrs)))
    except TypeError:
        try:
            return _clip(str(tool.handler(block.body)))
        except Exception:
            return traceback.format_exc()
    except Exception:
        return traceback.format_exc()


def ns_names(world: World) -> list[str]:
    skip = {"__builtins__", "handle"}
    return sorted(k for k in world.ns if k not in skip and not k.startswith("_"))


def catalog(world: World) -> str:
    lines = ["# tools"]
    for name in (*sorted(t for t in world.tools if t in FROZEN), *sorted(t for t in world.tools if t not in FROZEN)):
        tool = world.tools[name]
        flag = " frozen" if tool.frozen else ""
        lines.append(f"<{name}>{flag} {tool.doc}")
    if world.notes:
        lines.append("# your notes")
        for key, note in world.notes.items():
            lines.append(f"[{key}]\n{note}")
    return "\n".join(lines)


def system_prompt(world: World) -> str:
    return ABI + "\n\n" + catalog(world)


def header(world: World, task: str) -> str:
    names = ns_names(world)
    return "\n".join(
        [
            f"task: {task}",
            f"cwd: {world.cwd}",
            f"ns: {', '.join(names) if names else '(empty)'}",
        ]
    )


def seed_builtins(world: World) -> None:
    world.tools["python"] = Tool("python", "exec Python in the persistent kernel", frozen=True)
    world.tools["bash"] = Tool("bash", "run a shell command in cwd", frozen=True)
    world.tools["register"] = Tool("register", 'install a tag: name= and doc=, body is def handle(body, **attrs)', frozen=True)
    world.tools["system"] = Tool("system", "write or delete a system note (name=, optional delete=1)", frozen=True)
    world.tools["tool"] = Tool("tool", "rewrite a tool description: name= and doc=", frozen=True)


def state_file(world: World) -> Path:
    if world.state_path:
        return world.state_path
    return world.cwd / ".desmos" / "harness.json"


def save(world: World) -> None:
    path = state_file(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "notes": world.notes,
        "tools": {
            name: {"doc": tool.doc, "source": tool.source}
            for name, tool in world.tools.items()
            if not tool.frozen
        },
        "docs": {name: tool.doc for name, tool in world.tools.items() if tool.frozen},
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(world: World) -> None:
    path = state_file(world)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    notes = data.get("notes")
    if isinstance(notes, dict):
        world.notes = {str(k): str(v) for k, v in notes.items() if isinstance(v, str)}
    docs = data.get("docs")
    if isinstance(docs, dict):
        for name, doc in docs.items():
            if name in world.tools and isinstance(doc, str) and doc.strip():
                world.tools[name].doc = doc
    tools = data.get("tools")
    if isinstance(tools, dict):
        for name, spec in tools.items():
            if name in FROZEN or not isinstance(spec, dict):
                continue
            source = spec.get("source")
            doc = spec.get("doc") or f"user tag <{name}>"
            if not isinstance(source, str) or not isinstance(doc, str):
                continue
            try:
                fn = _callable_from_source(world, source, name)
            except Exception:
                continue
            world.tools[name] = Tool(name=name, doc=doc, source=source, handler=fn)


def complete(model: str, system: str, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise SystemExit(f"Anthropic HTTP {e.code}: {body[:2000]}") from e


def text_of(resp: dict[str, Any]) -> str:
    parts = []
    for block in resp.get("content") or []:
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)


def format_results(results: list[tuple[Block, str]]) -> str:
    chunks = []
    for b, r in results:
        attr = " ".join(f'{k}="{v}"' for k, v in b.attrs.items())
        label = f"<{b.tag} {attr}>".strip() if attr else f"<{b.tag}>"
        chunks.append(f"{label} ->\n{r}")
    return "\n\n".join(chunks)


def step(world: World, model: str, messages: list[dict[str, str]], max_tokens: int) -> tuple[str, list[tuple[Block, str]], bool]:
    resp = complete(model, system_prompt(world), messages, max_tokens)
    speech = text_of(resp)
    world.log.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "usage": resp.get("usage") or {},
            "stop": resp.get("stop_reason"),
            "text": speech,
        }
    )
    blocks = scan(speech)
    results = [(b, dispatch(world, b)) for b in blocks]
    return speech, results, not blocks


def new_world(cwd: Path, state_path: Path | None = None) -> World:
    world = World(cwd=cwd, state_path=state_path)
    world.ns["CWD"] = str(cwd)
    seed_builtins(world)
    load(world)
    return world


def run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    os.chdir(cwd)
    world = new_world(cwd)
    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)
    task = args.task
    messages: list[dict[str, str]] = [{"role": "user", "content": header(world, task) + "\n\n" + task}]

    print(f"model={args.model} max_turns={args.max_turns} cwd={cwd}")
    print(system_prompt(world))
    print("--------------")

    for turn in range(1, args.max_turns + 1):
        print(f"\n===== turn {turn} =====")
        speech, results, done = step(world, args.model, messages, args.max_tokens)
        print(speech)
        last_results = format_results(results) if results else ""
        if last_results:
            print("\n--- results ---")
            print(last_results)

        record = {
            "turn": turn,
            "speech": speech,
            "results": [{"tag": b.tag, "attrs": b.attrs, "body": b.body, "result": r} for b, r in results],
            "ns": ns_names(world),
            "tools": {n: t.doc for n, t in world.tools.items()},
            "notes": world.notes,
        }
        (run_dir / f"turn-{turn:02d}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

        if done:
            print(f"\n[done on turn {turn}]")
            break

        messages.append({"role": "assistant", "content": speech})
        messages.append(
            {
                "role": "user",
                "content": header(world, task) + "\n\nsyscall results:\n" + _clip(last_results, 6000),
            }
        )
    else:
        print(f"\n[hit max_turns={args.max_turns}]")

    summary = {
        "task": task,
        "ns": ns_names(world),
        "tools": {n: t.doc for n, t in world.tools.items()},
        "notes": world.notes,
        "turns": len(list(run_dir.glob("turn-*.json"))),
        "usage": [e.get("usage") for e in world.log],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\n===== summary =====")
    print(json.dumps(summary, indent=2))
    return 0


def _self_check() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp)
        world = new_world(cwd, state_path=cwd / "harness.json")

        blocks = scan('<python>x = 1+1</python>\n<bash>echo hi</bash>')
        assert [b.tag for b in blocks] == ["python", "bash"]
        assert dispatch(world, blocks[0]) == "ok"
        assert world.ns["x"] == 2
        assert dispatch(world, blocks[1]).strip() == "hi"

        out = dispatch(
            world,
            Block("register", "def handle(body, **a):\n    return body.upper()\n", {"name": "echo", "doc": "uppercase"}),
        )
        assert "registered" in out
        assert dispatch(world, Block("echo", "hi", {})) == "HI"
        assert world.tools["echo"].doc == "uppercase"

        assert "frozen" in dispatch(world, Block("register", "def handle(body, **a): return 1\n", {"name": "python"}))

        assert "wrote" in dispatch(world, Block("system", "prefer tests", {"name": "style"}))
        assert "prefer tests" in system_prompt(world)
        assert "updated" in dispatch(world, Block("tool", "", {"name": "bash", "doc": "project shell"}))
        assert "project shell" in world.tools["bash"].doc

        world2 = new_world(cwd, state_path=cwd / "harness.json")
        assert "echo" in world2.tools
        assert world2.notes["style"] == "prefer tests"
        assert world2.tools["bash"].doc == "project shell"
        assert dispatch(world2, Block("echo", "ok", {})) == "OK"

        assert "deleted" in dispatch(world2, Block("system", "", {"name": "style", "delete": "1"}))
        assert "style" not in world2.notes

    print("self-check ok")


def main() -> int:
    p = argparse.ArgumentParser(description="Inverted coding harness")
    p.add_argument("task", nargs="?", default="", help="coding task")
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--max-turns", type=int, default=32)
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--cwd", default=".")
    p.add_argument("--out", default="")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    if args.check:
        _self_check()
        return 0
    if not args.task:
        p.error("task required (or --check)")
    if not args.out:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.out = str(Path("runs") / f"task-{stamp}")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
