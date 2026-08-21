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
    from desmos.kernel.loop import install_resources, new_world
    from desmos.types import Block

    world = new_world(ws, state_path=ws / "harness.sqlite3")
    before = dispatch(world, Block("greet", "world", {}))
    assert "unknown tag <greet>" in before, before

    # "Between turns": the agent writes the extension file.
    ext = ws / ".desmos" / "extensions"
    ext.mkdir(parents=True)
    (ext / "greet.py").write_text(
        "def load(api):\n"
        "    api.register_tool('greet', 'say hello',"
        " lambda body, **a: 'hello ' + body)\n",
        encoding="utf-8",
    )

    # The next turn boundary: turn() calls install_resources with no force;
    # the stat sweep sees the new file and rebuilds (ambient reload).
    install_resources(world)
    after = dispatch(world, Block("greet", "world", {}))
    assert after == "hello world", after
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
