#!/usr/bin/env python3
"""Golden-stream recorder (upgrade-paths Phase 5.1).

Runs canned sessions through the real loop (desmos.loop.run_turns with a
stubbed world.complete_fn -- no network, no key) and captures every on_event
dict, one JSON object per line, to golden/<scenario>.jsonl.

    record-golden.py record    rewrite golden/
    record-golden.py compare   re-run and diff; non-zero exit on any difference

normalize_event() is the one normalization function, used at record time here
and importable for compare time elsewhere.

Coverage note: `login` (interactive OAuth, needs a browser) and
`intervention` (a kill_run/rerun op arriving at the bridge; the recorder
captures run_turns' on_event and never routes ops) are the loop/bridge event
kinds the corpus cannot carry — both stay covered by the live-bridge half of
checks/conformance.py, which also covers the `session`-headed, seq/ts-stamped
event-log form (a bridge-writer artifact, so never in these unstamped
fixtures). The terminal `subagent` phase "stopped" likewise only follows a
kill_run; desmos/checks/agents.py drives it through a real kill.
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
# The canned responses speak the prose dialect (XML in speech). With typed
# syscall tools on, the loop rightly refuses that as "XML as speech" -- the
# tool-channel wire has its own check (checks/anthropic_check.py). Pin off,
# same as the check runner does.
os.environ["DESMOS_TOOL_SYSCALLS"] = "0"
# The commit scenario runs real git. Pin it off the machine's config (system
# gitconfig, XDG paths) so hooks, signing, or a renamed default branch cannot
# reshape the recorded output.
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
os.environ["GIT_CONFIG_GLOBAL"] = str(Path(_HOME) / "gitconfig-empty")

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
    # git's commit summary line names the new short sha, which differs on
    # every run: `[main (root-commit) abc1234] msg`. Before the <RID> rule so
    # an 8-char sha cannot be half-claimed by it.
    (re.compile(r"(\[[^\n\]]+ )[0-9a-f]{7,40}(\])"), r"\g<1><SHA>\g<2>"),
    # pending task ids: counter + uuid4().hex[:8] notice id. Before the <RID>
    # rule so the hex suffix cannot be half-claimed by it.
    (re.compile(r"\[t\d+-[0-9a-f]{8}\]"), "[t<N>]"),
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
        # result.repo.committed is a bare short sha with no bracket context
        # for the text scrub to key on.
        if key == "committed":
            return "<SHA>"
        return _scrub(obj)
    if key == "secs" and isinstance(obj, (int, float)):
        return 0.0
    return obj


# --- scripted responses -------------------------------------------------------


def R(*blocks, stop="end_turn"):
    return {"content": list(blocks), "stop_reason": stop, "usage": dict(USAGE)}


def text(t):
    return {"type": "text", "text": t}


def think(t):
    # A signature is what makes assistant_content keep the block as thinking
    # rather than demoting it to text (transport/complete.py).
    return {"type": "thinking", "thinking": t, "signature": "sig"}


def scripted(script, _started=None):
    """A complete_fn that replays `script` in order and fails loud past its end.

    A dict keys separate queues by model name: the levels of a spawn tree run
    concurrently and inherit one complete_fn, so a shared list would let a
    fast child steal a slow sibling's entry. One model per level makes each
    level's consumption order its own.
    """
    if isinstance(script, dict):
        # One Event per level, set the first time that level's model is called.
        # A parent turn can block on a child level's `started` event to pin the
        # interleaving: the child subtree's first envelope always precedes the
        # parent's next post, so the merged stream is the same order every run.
        # This is the sequencing the tree fixture needs -- without it the root's
        # turn-2 post raced the child's first kind:prompt envelope (2-in-6 flake).
        started = {model: threading.Event() for model in script}
        fns = {model: scripted(items) for model, items in script.items()}

        def by_model(model, system, messages, max_tokens):
            if model not in fns:
                raise AssertionError(f"no scripted queue for model {model!r}")
            # Set BEFORE the scripted reply: a model's post event fires before
            # its complete_fn, so once `started[model]` is set that level's
            # first envelope is already on the wire. A parent turn can wait on
            # a child level's event to pin the interleaving deterministically.
            started[model].set()
            return fns[model](model, system, messages, max_tokens)

        by_model.started = started
        return by_model

    calls = {"n": 0}
    lock = threading.Lock()

    def fn(model, system, messages, max_tokens):
        with lock:  # concurrent levels of a tree must not race the index
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
def thinking(tmp):
    # The unstreamed replay path: a stub never streams, so the thinking block
    # is fired whole from thought_blocks (kernel/loop.py), no delta field.
    return {
        "prompt": "Think first, then answer.",
        "script": [R(think("Weighing the greeting."), text("Hello, after some thought."))],
    }


@scenario
def compacted(tmp):
    # A server-side fold: the response carries a compaction block, so the loop
    # fires the compacted event with the block's summary.
    return {
        "prompt": "Carry on after the fold.",
        "script": [
            R(
                {"type": "compaction", "summary": "folded: the early turns greeted and ran setup"},
                text("Continuing from the fold."),
            )
        ],
    }


@scenario
def guidance(tmp):
    # on_continue fires between turns while the model keeps calling syscalls;
    # the reminder goes into the transcript and out as the guidance event.
    return {
        "prompt": "Do one step, take the reminder, then finish.",
        "script": [
            R(text("Step one.\n<python>2 + 2</python>")),
            R(text("Finished after the reminder.")),
        ],
        "on_continue": lambda n: "guidance: stay on the task and finish",
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


def _fff_built() -> bool:
    try:
        import fff  # noqa: F401
    except Exception:
        return False
    return True


# A real <find> through the loop over a live fff engine, path search only. The
# score in the result is fff's own, deterministic for a fixed tree+query. Gated
# on the extension: an absent fff would record the refusal instead, so a machine
# without it must not carry (or compare) this fixture. Registered only when fff
# imports, so record()/compare() drop it cleanly elsewhere; the committed
# find.jsonl still parses in checks/conformance.py (JSON, fff-independent).
if _fff_built():
    @scenario
    def find(tmp):
        (tmp / "src").mkdir()
        (tmp / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
        (tmp / "src" / "other.py").write_text("y = 2\n", encoding="utf-8")
        return {
            "prompt": "Find the main file and report.",
            "script": [
                R(text('Searching.\n<find limit="5">mian.py</find>')),
                R(text("Found src/main.py.")),
            ],
        }


@scenario
def commit(tmp):
    # A real git commit through the real loop: the kernel judges the claim
    # from the command's own output, so the first result done event carries
    # repo.committed (sha normalized to <SHA>) and the second — same command
    # shape, nothing staged, exit 1 — carries no repo field at all.
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp, check=True)
    (tmp / "f.txt").write_text("one\n", encoding="utf-8")
    git_commit = "git -c user.name=g -c user.email=g@x commit"
    return {
        "prompt": "Commit the file, try to commit again, then report.",
        "script": [
            R(text(f"<bash>git add f.txt && {git_commit} -m add-f</bash>")),
            R(text(f"<bash>{git_commit} -m nothing-staged</bash>")),
            R(text("One commit made; the second had nothing to commit.")),
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
            # The child's turns: a thinking block plus a syscall (child kinds
            # thinking and result), then a raising turn (child kind error --
            # the loop turns it into a value and returns the last speech).
            # The syscall must be <bash>: a child <python> queues on the
            # kernel's stdio-redirect lock, which the parent's own <python>
            # holds while it sits in wait(rid), and times out.
            R(think("Choosing a friendly tone."), text("Composing.\n<bash>echo hi</bash>")),
            _raise_turn_two,
            R(text("Child finished; waiting on its notice.")),
            R(text("All done.")),  # after the pending notice resumes the step
        ],
    }


@scenario
def tree(tmp):
    # The depth-2 forest through the real loop: root spawns A with budget=1
    # from its own <python>; A's <python> spawns leaf B handing spawn its own
    # world (`parent=world`, the ns binding bind_step publishes), so B records
    # parent=A at depth 1 with budget 0.
    #
    # Serialization is layered around the global <python> stdio lock. The root
    # cannot poll A's settle inside its own <python> — holding the lock there
    # deadlocks A's <python> for the whole 300s block timeout — so the root
    # spawns, ends its turn, and parks on A's pending notice (the C5 brief);
    # its park lands before A's world finishes building. A CAN poll inside its
    # <python>, because leaf B speaks only <bash>: the poll plus a beat past
    # the state flip keeps B's whole stream inside that one dispatch, where
    # A's next turn cannot race it. Every level speaks its own model: see
    # scripted() on why the queues must not be shared across concurrent levels.
    child_model = "claude-sonnet-5"
    leaf_model = "claude-haiku-4"
    spawn_a = "\n".join(
        [
            "from desmos.subagent import spawn",
            f'print(spawn("relay the greeting one level down", agent="general", model="{child_model}", budget=1))',
            # Block turn-1's result until A's first envelope is on the wire, so
            # A's kind:prompt always precedes root turn-2's post. Gating the
            # result (not complete_fn) is what orders the two turns: a post
            # fires BEFORE its complete_fn, so a complete_fn barrier is too
            # late. A reaches its complete_fn -- where `started` is set --
            # without the stdio lock this <python> holds, so no deadlock.
            f'_started["{child_model}"].wait(30)',
        ]
    )
    spawn_b = "\n".join(
        [
            "import time",
            "import desmos.agents.subagent as S",
            "from desmos.subagent import spawn",
            f'rid = spawn("compose a deep greeting", agent="explore", model="{leaf_model}", parent=world)',
            'while S.RUNS[rid].state in ("pending", "running"):',
            "    time.sleep(0.05)",
            "time.sleep(0.3)  # the terminal subagent event trails the state flip",
            "print(rid)",
        ]
    )
    return {
        "prompt": "Relay a greeting through a child and a grandchild, then report.",
        "script": {
            MODEL: [
                R(text("Planting the tree.\n<python>\n" + spawn_a + "\n</python>")),
                R(text("The relay is running; waiting for its notice.")),
                R(text("All levels reported.")),
            ],
            child_model: [
                R(text("Delegating deeper.\n<python>\n" + spawn_b + "\n</python>")),
                R(text("Waiting on the grandchild.")),
                R(text("Grandchild replied; relay assembled.")),
            ],
            leaf_model: [
                R(text("Composing.\n<bash>echo deep hello</bash>")),
                R(text("A deep hello, composed.")),
            ],
        },
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
    _complete = scripted(spec["script"])
    world.complete_fn = _complete

    lines = []
    lock = threading.Lock()
    stop_flag = {"stop": False}
    settled = threading.Event()

    # Fixtures capture the wire, which is unstamped: seq/ts belong to the
    # bridge-side event-log writer (front/bridge.py _log), never to producers,
    # so the recorder takes no stamping code.
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
    # The per-level `started` map lets a scenario's <python> gate a turn on a
    # child level having begun (see tree()). Absent on flat single-level
    # scenarios, which never reference it.
    world.ns["_started"] = getattr(_complete, "started", {})
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
            on_continue=spec.get("on_continue"),
            max_tokens=8192,
        )
    finally:
        S.set_emitter(None)
        pending.clear(world)
        # Close any live fff engine (the find scenario, or an <edit>'s touch)
        # so its native watch threads do not outlive the scenario or block exit.
        try:
            from desmos.state import find as _find_mod

            _find_mod.reset()
        except Exception:
            pass
    return lines


def record():
    GOLDEN.mkdir(exist_ok=True)
    for name in SCENARIOS:
        lines = run_scenario(name)
        (GOLDEN / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"recorded {name}: {len(lines)} events")
    return 0


# A live fork tree emits from several loop threads through one on_event; the
# interleave is not deterministic and the system promises nothing about it. For
# these, assert the event multiset (order-free over normalized lines) -- a
# missing, extra, or changed event still fails; only a reordering of concurrent
# events is tolerated. Everything else is byte-and-order exact.
ORDER_FREE = {"tree"}


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
        if name in ORDER_FREE:
            from collections import Counter

            if Counter(got) == Counter(want):
                print(f"{name}: ok ({len(got)} events, order-free)")
                continue
            failed = True
            miss = Counter(want) - Counter(got)
            extra = Counter(got) - Counter(want)
            print(f"{name}: MULTISET DIVERGENCE (golden {len(want)}, now {len(got)})")
            for line, n in list(miss.items())[:3]:
                print(f"  golden only x{n}: {line[:160]}")
            for line, n in list(extra.items())[:3]:
                print(f"  now only x{n}: {line[:160]}")
            continue
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
