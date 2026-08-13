#!/usr/bin/env python3
"""Inverted harness hunger-games: Opus lives in a kernel, chat can die."""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import os
import re
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# frozen core
# ---------------------------------------------------------------------------

TAG_OPEN = re.compile(r"<([A-Za-z_][\w.-]*)((?:\s+[^>]*?)?)>", re.S)
ATTR = re.compile(r'([A-Za-z_][\w.-]*)\s*=\s*"([^"]*)"')
FROZEN = frozenset({"python", "register"})

ABI = """You live in a persistent Python kernel. Text is speech. XML tags are syscalls.

<python>
code
</python>
executes in the kernel. stdout and the last expression come back. Names persist across turns.

<register name="tag">
def handle(body, **attrs):
    ...
</register>
installs a new syscall. You may then emit <tag attr="v">body</tag>.

There is no other interface. No tools. No shell unless you build one.
When finished, speak without XML."""


@dataclass
class Block:
    tag: str
    body: str
    attrs: dict[str, str]


@dataclass
class World:
    ns: dict[str, Any] = field(default_factory=dict)
    handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    starved: bool = False


def scan(text: str) -> list[Block]:
    """Fence-unaware but nested-same-tag-safe enough: first matching close."""
    blocks: list[Block] = []
    pos = 0
    while True:
        m = TAG_OPEN.search(text, pos)
        if not m:
            break
        tag, raw_attrs = m.group(1), m.group(2) or ""
        if raw_attrs.strip().endswith("/"):
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


RESULT_CAP = 4000


def _clip(text: str, cap: int = RESULT_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 20] + f"\n…[{len(text) - cap + 20} chars clipped]"


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
        err = buf.getvalue()
        return _clip((err + traceback.format_exc()).strip())


def _register(world: World, body: str, name: str) -> str:
    if not name or not name.isidentifier():
        return f"register failed: bad name {name!r}"
    if name in FROZEN:
        return f"register failed: {name} is frozen"
    local: dict[str, Any] = {}
    try:
        exec(compile(body, f"<register:{name}>", "exec"), world.ns, local)
    except Exception:
        return traceback.format_exc()
    fn = local.get("handle") or world.ns.get("handle")
    if not callable(fn):
        for v in local.values():
            if callable(v):
                fn = v
                break
    if not callable(fn):
        return "register failed: no callable handle"
    world.handlers[name] = fn
    world.ns[f"handle_{name}"] = fn
    return f"registered <{name}>"


def dispatch(world: World, block: Block) -> str:
    if block.tag == "python":
        return _run_python(block.body, world.ns)
    if block.tag == "register":
        return _register(world, block.body, block.attrs.get("name", ""))
    fn = world.handlers.get(block.tag)
    if fn is None:
        return f"unknown tag <{block.tag}> — register it first"
    try:
        return str(fn(block.body, **block.attrs))
    except TypeError:
        try:
            return str(fn(block.body))
        except Exception:
            return traceback.format_exc()
    except Exception:
        return traceback.format_exc()


def ns_names(world: World) -> list[str]:
    skip = {"__builtins__", "handle"}
    names = []
    for k, v in world.ns.items():
        if k in skip or k.startswith("_"):
            continue
        names.append(k)
    return sorted(names)


def header(world: World, mission: str) -> str:
    tags = ["python", "register", *sorted(world.handlers)]
    names = ns_names(world)
    lines = [
        f"mission: {mission}",
        f"tags: {', '.join(tags)}",
        f"ns: {', '.join(names) if names else '(empty)'}",
        f"starved: {world.starved}",
    ]
    return "\n".join(lines)


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


def step(
    world: World,
    model: str,
    mission: str,
    messages: list[dict[str, str]],
    max_tokens: int,
) -> tuple[str, list[tuple[Block, str]], bool]:
    resp = complete(model, ABI, messages, max_tokens)
    speech = text_of(resp)
    usage = resp.get("usage") or {}
    world.log.append(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "usage": usage,
            "stop": resp.get("stop_reason"),
            "text": speech,
        }
    )
    blocks = scan(speech)
    results = [(b, dispatch(world, b)) for b in blocks]
    done = not blocks
    return speech, results, done


def starve(world: World, mission: str, last_results: str) -> list[dict[str, str]]:
    world.starved = True
    body = (
        header(world, mission)
        + "\n\nSTARVED. Conversational memory is gone. The kernel is not.\n"
        + "Continue the mission.\n"
    )
    if last_results:
        body += "\nlast syscall results:\n" + _clip(last_results, 6000)
    return [{"role": "user", "content": body}]


def format_results(results: list[tuple[Block, str]]) -> str:
    chunks = []
    for b, r in results:
        chunks.append(f"<{b.tag} {b.attrs}> ->\n{r}")
    return "\n\n".join(chunks)


def run(args: argparse.Namespace) -> int:
    mission = args.mission
    world = World()
    world.ns["MISSION"] = mission
    run_dir = Path(args.out)
    run_dir.mkdir(parents=True, exist_ok=True)

    user0 = header(world, mission) + "\n\n" + mission
    messages: list[dict[str, str]] = [{"role": "user", "content": user0}]
    last_results = ""

    print(f"model={args.model} starve_after={args.starve_after} max_turns={args.max_turns}")
    print(f"cwd={os.getcwd()}")
    print("--- ABI ---")
    print(ABI)
    print("--- mission ---")
    print(mission)
    print("--------------")

    for turn in range(1, args.max_turns + 1):
        if args.starve_after and turn == args.starve_after + 1 and not world.starved:
            messages = starve(world, mission, last_results)
            print(f"\n===== STARVE @ turn {turn} =====\n{messages[0]['content']}\n")

        print(f"\n===== turn {turn} =====")
        speech, results, done = step(world, args.model, mission, messages, args.max_tokens)
        print(speech)
        if results:
            last_results = format_results(results)
            print("\n--- results ---")
            print(last_results)
        else:
            last_results = ""

        record = {
            "turn": turn,
            "starved": world.starved,
            "speech": speech,
            "results": [{"tag": b.tag, "attrs": b.attrs, "body": b.body, "result": r} for b, r in results],
            "ns": ns_names(world),
            "tags": ["python", "register", *sorted(world.handlers)],
        }
        (run_dir / f"turn-{turn:02d}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

        if done:
            print(f"\n[stop: no syscalls on turn {turn}]")
            break

        follow = header(world, mission) + "\n\nsyscall results:\n" + _clip(last_results, 6000)
        if world.starved:
            # hunger: only header + last results exist
            messages = [{"role": "user", "content": follow + "\n\nContinue. Speak with no XML when the mission is done."}]
        else:
            messages.append({"role": "assistant", "content": speech})
            messages.append({"role": "user", "content": follow})
    else:
        print(f"\n[stop: hit max_turns={args.max_turns}]")

    survival = world.ns.get("SURVIVAL")
    summary = {
        "survival": None if survival is None else repr(survival),
        "ns": ns_names(world),
        "tags": list(world.handlers),
        "starved": world.starved,
        "turns": len(list(run_dir.glob("turn-*.json"))),
        "usage": [e.get("usage") for e in world.log],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\n===== summary =====")
    print(json.dumps(summary, indent=2))
    return 0 if survival is not None else 2


def _self_check() -> None:
    w = World()
    blocks = scan('<python>x = 1+1</python>\n<register name="echo">\ndef handle(body, **a):\n    return body.upper()\n</register>')
    assert [b.tag for b in blocks] == ["python", "register"]
    assert dispatch(w, blocks[0]) == "ok"
    assert w.ns["x"] == 2
    assert "echo" in dispatch(w, blocks[1])
    assert dispatch(w, Block("echo", "hi", {})) == "HI"
    print("self-check ok")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="claude-opus-5")
    p.add_argument("--max-turns", type=int, default=10)
    p.add_argument("--starve-after", type=int, default=3, help="wipe chat after this many turns; 0=never")
    p.add_argument("--max-tokens", type=int, default=8192)
    p.add_argument("--out", default="")
    p.add_argument("--check", action="store_true")
    p.add_argument(
        "--mission",
        default=(
            "Discover where you are. Build whatever appendages you need. "
            "Find the one-sentence thesis of the inverted RLM idea in this workspace. "
            "Store that sentence in a kernel variable named SURVIVAL. Then stop."
        ),
    )
    args = p.parse_args()
    if args.check:
        _self_check()
        return 0
    if not args.out:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.out = str(Path("runs") / f"hunger-{stamp}")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
