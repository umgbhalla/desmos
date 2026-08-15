#!/usr/bin/env python3
"""Golden-stream recorder (upgrade-paths Phase 5.1).

Runs canned sessions through the real loop (desmos.loop.run_turns with a
stubbed world.complete_fn -- no network, no key) and captures every on_event
dict, one JSON object per line, to golden/<scenario>.jsonl.

    record-golden.py record    rewrite golden/
    record-golden.py compare   re-run and diff; non-zero exit on any difference

normalize_event() is the one normalization function, used at record time here
and importable for compare time elsewhere.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "golden"
sys.path.insert(0, str(REPO))

# Hermetic before any desmos import: const.py reads DESMOS_* at import time,
# skills/extensions/settings read $HOME. Everything lands in throwaway dirs
# whose names carry the desmos-golden- marker the normalizer scrubs.
for _key in [k for k in os.environ if k.startswith("DESMOS_")]:
    del os.environ[_key]
_HOME = tempfile.mkdtemp(prefix="desmos-golden-home-")
os.environ["HOME"] = _HOME
os.environ["DESMOS_SETTINGS"] = str(Path(_HOME) / "settings.json")

MODEL = "claude-opus-5"
OPENAI_MODEL = "gpt-5.6-sol"
USAGE = {"input_tokens": 11, "output_tokens": 7}

# --- normalization ------------------------------------------------------------

_SCRUBS = [
    # The checkout root. The catalog's runtime block prints sdk/docs/skills
    # paths verbatim, so without this rule every fixture embeds the absolute
    # repo path (and the username) and compare fails on any other clone.
    (re.compile(re.escape(str(REPO))), "<ROOT>"),
    # The user's home directory reaches the catalog via user/shared skill roots.
    (re.compile(re.escape(str(Path.home()))), "<HOME>"),
    # ISO timestamps
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?\b"), "<TS>"),
    # uuids, hyphenated and hex32
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<UUID>"),
    (re.compile(r"\b[0-9a-f]{32}\b"), "<UUID>"),
    # every temp dir this script makes carries this marker in its name
    (re.compile(r"/[^\s\"':]*?desmos-golden-[0-9A-Za-z_-]+"), "<TMP>"),
    # subagent run ids (uuid4().hex[:8])
    (re.compile(r"\b[0-9a-f]{8}\b"), "<RID>"),
    # durations in pending summaries / settle lines
    (re.compile(r"\bafter \d+(?:\.\d+)?s\b"), "after <SECS>s"),
    # pending task ids from the global counter
    (re.compile(r"\[t\d+\]"), "[t<N>]"),
    (re.compile(r"\bpid[ =]\d+\b"), "pid <PID>"),
    # the CWD ns entry's length is the temp path's length
    (re.compile(r"CWD: str, \d+ chars"), "CWD: str, <N> chars"),
]


def _scrub(text: str) -> str:
    for pattern, repl in _SCRUBS:
        text = pattern.sub(repl, text)
    return text


def normalize_event(ev):
    """Stable placeholders for timestamps, uuids, temp paths, durations, pids.

    Everything else passes through byte-exact. Used at record time and at
    compare time -- one function, imported by both.
    """
    return _walk(ev, None)


def _walk(obj, key):
    if isinstance(obj, dict):
        return {k: _walk(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, None) for v in obj]
    if isinstance(obj, str):
        return _scrub(obj)
    if key == "secs" and isinstance(obj, (int, float)):
        return 0.0
    return obj


# --- scripted responses -------------------------------------------------------


def R(*blocks, stop="end_turn"):
    return {"content": list(blocks), "stop_reason": stop, "usage": dict(USAGE)}


def text(t):
    return {"type": "text", "text": t}


def scripted(script):
    """A complete_fn that replays `script` in order and fails loud past its end."""
    calls = {"n": 0}

    def fn(model, system, messages, max_tokens):
        i = calls["n"]
        calls["n"] += 1
        if i >= len(script):
            raise AssertionError(f"scripted complete_fn exhausted at call {i + 1}")
        item = script[i]
        if callable(item):
            item = item()  # may raise: that IS the scripted behaviour
        # turn() mutates the response (pops _request, keeps content refs), so
        # every call hands out a fresh copy.
        return json.loads(json.dumps(item))

    return fn


def _raise_turn_two():
    raise RuntimeError("scripted turn 2 failure")


# --- scenarios ------------------------------------------------------------------

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@scenario
def plain(tmp):
    return {
        "prompt": "Say hello and finish.",
        "script": [R(text("Hello. Done."))],
    }


@scenario
def multi(tmp):
    return {
        "prompt": "Run a python line and a bash line, then report.",
        "script": [
            R(text('Running both.\n<python>print("alpha")</python>\n<bash>echo beta</bash>')),
            R(text("Both ran: alpha and beta.")),
        ],
    }


@scenario
def edit(tmp):
    (tmp / "target.txt").write_text("hello world\n", encoding="utf-8")
    return {
        "prompt": "Change hello to goodbye in target.txt.",
        "script": [
            R(text('<edit path="target.txt">hello\n---\ngoodbye</edit>')),
            R(text("Replaced hello with goodbye.")),
        ],
    }


@scenario
def spawn(tmp):
    # wait(rid) serializes the child's whole run inside this one <python>
    # dispatch; _settled (set by the recorder when the terminal subagent event
    # lands) closes the last gap -- _execute flips run.state before its finally
    # emits, so wait() alone could return a beat ahead of the final event.
    body = "\n".join(
        [
            "from desmos.subagent import spawn, wait",
            'rid = spawn("compose a greeting", agent="explore", model="claude-opus-5")',
            "wait(rid)",
            "_settled.wait(10)",
            'print("child settled")',
        ]
    )
    return {
        "prompt": "Spawn a child that composes a greeting, then report.",
        "script": [
            R(text("Spawning.\n<python>\n" + body + "\n</python>")),
            R(text("Hello from the child.")),  # the child's one turn
            R(text("Child finished; waiting on its notice.")),
            R(text("All done.")),  # after the pending notice resumes the step
        ],
    }


@scenario
def error(tmp):
    return {
        "prompt": "Do a little work.",
        "script": [
            R(text("Working.\n<python>1 + 1</python>")),
            _raise_turn_two,
        ],
    }


@scenario
def stop(tmp):
    return {
        "prompt": "Run two commands.",
        "script": [R(text('<python>"first"</python>\n<bash>echo second</bash>'))],
        "stop_after_first_result": True,
    }


@scenario
def openai(tmp):
    return {
        "model": OPENAI_MODEL,
        "prompt": "Echo ok and report.",
        "script": [
            R(
                text("Checking."),
                {
                    "type": "custom_tool_call",
                    "name": "syscall",
                    "call_id": "call_001",
                    "input": "<bash>echo ok</bash>",
                },
            ),
            R(text("ok came back. Done.")),
        ],
    }


# --- runner ---------------------------------------------------------------------


def run_scenario(name):
    from desmos import pending
    from desmos.loop import new_world, run_turns
    import desmos.subagent as S

    spec_fn = SCENARIOS[name]
    tmp = Path(tempfile.mkdtemp(prefix="desmos-golden-")).resolve()
    os.chdir(tmp)  # subagent._persist writes .desmos/subagents relative to cwd
    spec = spec_fn(tmp)

    world = new_world(tmp, persist=False)
    world.model = spec.get("model", MODEL)
    world.thinking = "low"
    world.complete_fn = scripted(spec["script"])

    lines = []
    lock = threading.Lock()
    stop_flag = {"stop": False}
    settled = threading.Event()

    def emit(ev):
        if ev.get("ev") == "result" and ev.get("phase") == "done":
            stop_flag["stop"] = True
        if ev.get("ev") == "subagent" and ev.get("phase") in {"done", "failed", "stopped"}:
            settled.set()
        # Normalize and serialize at capture time: later mutation of shared
        # dicts cannot rewrite an already-recorded line.
        line = json.dumps(normalize_event(ev), sort_keys=True, ensure_ascii=True)
        with lock:
            lines.append(line)

    world.ns["_settled"] = settled
    S.bind(world)
    S.set_emitter(emit)
    should_stop = (lambda: stop_flag["stop"]) if spec.get("stop_after_first_result") else None
    try:
        run_turns(
            world,
            spec["prompt"],
            quiet=True,
            on_event=emit,
            should_stop=should_stop,
            max_tokens=8192,
        )
    finally:
        S.set_emitter(None)
        pending.clear(world)
    return lines


def record():
    GOLDEN.mkdir(exist_ok=True)
    for name in SCENARIOS:
        lines = run_scenario(name)
        (GOLDEN / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"recorded {name}: {len(lines)} events")
    return 0


def compare():
    failed = False
    for name in SCENARIOS:
        path = GOLDEN / f"{name}.jsonl"
        if not path.is_file():
            print(f"{name}: MISSING fixture {path}")
            failed = True
            continue
        want = path.read_text(encoding="utf-8").splitlines()
        got = run_scenario(name)
        if got == want:
            print(f"{name}: ok ({len(got)} events)")
            continue
        failed = True
        for i, (w, g) in enumerate(zip(want, got)):
            if w != g:
                print(f"{name}: FIRST DIVERGENCE at line {i + 1}")
                print(f"  golden: {w}")
                print(f"  now:    {g}")
                break
        else:
            i = min(len(want), len(got))
            print(f"{name}: LENGTH DIVERGENCE at line {i + 1} (golden {len(want)}, now {len(got)})")
            extra = (want[i] if len(want) > len(got) else got[i]) if max(len(want), len(got)) > i else ""
            if extra:
                side = "golden" if len(want) > len(got) else "now"
                print(f"  {side} only: {extra}")
    return 1 if failed else 0


def main(argv):
    if len(argv) != 2 or argv[1] not in {"record", "compare"}:
        print(__doc__.strip())
        return 2
    return record() if argv[1] == "record" else compare()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
