import os
import sys
import tempfile
import traceback
from pathlib import Path


def _workspace() -> Path:
    """A REAL, fully isolated temp workspace: temp HOME, registry, settings.

    The .git marker stops the extension-root walk at the workspace, so no
    ancestor (or the developer's real ~/.desmos) leaks into the trial.
    """
    repo = Path(os.environ.get("DESMOS_REPO") or Path(__file__).resolve().parents[4])
    sys.path.insert(0, str(repo))
    tmp = Path(tempfile.mkdtemp(prefix="desmos-trial-"))
    home = tmp / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    os.environ["DESMOS_REGISTRY"] = str(tmp / "registry")
    settings = home / "settings.json"
    settings.write_text(
        '{"provider": "anthropic", "model": "claude-opus-5", "effort": "low"}',
        encoding="utf-8",
    )
    os.environ["DESMOS_SETTINGS"] = str(settings)
    ws = tmp / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    return ws


def main() -> float:
    ws = _workspace()
    import time

    from desmos.dispatch import dispatch
    from desmos.kernel.loop import new_world
    from desmos.state import persist
    from desmos.state.refine import census, tombstone
    from desmos.types import Block

    world = new_world(ws, state_path=ws / "harness.sqlite3")
    dispatch(
        world,
        Block(
            "harness",
            "def handle(body, **a):\n    return body.upper()\n",
            {"op": "register", "name": "shout", "doc": "uppercase"},
        ),
    )

    n = 3
    for i in range(n):
        out = dispatch(world, Block("shout", f"hi{i}", {}))
        assert out == f"HI{i}", out
        # The same wire event turn() fires for every dispatch; census reads
        # these back as the tool's career (desmos/state/refine.py census).
        persist.record_event(
            world,
            {"ev": "result", "phase": "done", "tag": "shout",
             "text": out, "span_idx": 0},
            ts_ms=int(time.time() * 1000),
            mono_ns=time.monotonic_ns(),
        )

    rows = {r["name"]: r for r in census(world)}
    row = rows.get("shout")
    assert row is not None, rows
    assert row["calls"] == n, row
    assert row["errors"] == 0, row
    assert row["verdict"] == "keep", row

    note = tombstone(world, "shout", "eval retire")
    assert "tombstoned <shout>" in note, note
    row = {r["name"]: r for r in census(world)}["shout"]
    assert row["verdict"] == "tombstoned", row
    assert row["tombstoned_at"], row

    answered = dispatch(world, Block("shout", "hi", {}))
    assert "<shout> was retired" in answered, answered
    assert "revive=shout" in answered, answered
    return 1.0


if __name__ == "__main__":
    try:
        reward = float(main())
    except BaseException:
        traceback.print_exc()
        reward = 0.0
    # Harbor contract: the verifier's only output is a scalar in reward.txt,
    # written to the trial's working directory.
    Path("reward.txt").write_text(f"{reward}\n", encoding="utf-8")
    print(f"reward={reward}")
