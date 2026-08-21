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
    from desmos.dispatch import dispatch
    from desmos.kernel.loop import new_world
    from desmos.state import memory as memory_mod
    from desmos.types import Block

    def fact(world, body, rid):
        return dispatch(
            world,
            Block("knowledge", body,
                  {"op": "memory", "id": rid, "scope": "repo", "kind": "fact"}),
        )

    w1 = new_world(ws, state_path=ws / "harness.sqlite3")
    fact(w1, "Rust compiler flags for this repo live in .cargo/config.toml.",
         "repo.rust.flags")
    # Distractors: each carries some but not all of the search terms.
    fact(w1, "The rust toolchain is pinned by rust-toolchain.toml.",
         "repo.rust.pin")
    fact(w1, "Compiler warnings are promoted to errors in CI.",
         "repo.ci.warnings")

    # A fresh world: new process state, same workspace on disk.
    w2 = new_world(ws, state_path=ws / "harness2.sqlite3")
    hits = memory_mod.search(w2, "rust compiler flags", mode="any")
    assert hits != "no match", hits
    lines = [ln for ln in hits.splitlines() if ln.strip()]
    ranked = [ln for ln in lines if "repo." in ln]
    assert ranked, hits
    assert "repo.rust.flags" in ranked[0], hits
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
