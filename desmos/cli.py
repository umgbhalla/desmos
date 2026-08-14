"""desmos console / kernel / check / run."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _on_path() -> None:
    root = str(_repo_root())
    path = os.environ.get("PYTHONPATH", "")
    if root not in path.split(os.pathsep):
        os.environ["PYTHONPATH"] = root + (os.pathsep + path if path else "")
    if root not in sys.path:
        sys.path.insert(0, root)


def cmd_console(args: argparse.Namespace) -> int:
    os.chdir(Path(args.cwd).resolve())
    _on_path()
    os.execv(
        sys.executable,
        [sys.executable, "-m", "IPython", "--ext", "desmos.ext"],
    )


def cmd_kernel(_args: argparse.Namespace) -> int:
    spec_dir = Path.home() / "Library" / "Jupyter" / "kernels" / "desmos"
    if sys.platform != "darwin":
        data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        spec_dir = Path(data) / "jupyter" / "kernels" / "desmos"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "argv": [
            sys.executable,
            "-m",
            "ipykernel_launcher",
            "-f",
            "{connection_file}",
            "--InteractiveShellApp.extensions=desmos.ext",
        ],
        "display_name": "Desmos",
        "language": "python",
        "env": {"PYTHONPATH": str(_repo_root())},
    }
    (spec_dir / "kernel.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    print(f"installed kernelspec: {spec_dir}")
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    _on_path()
    from desmos.check import self_check

    self_check()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _on_path()
    from desmos.loop import run

    return run(args)


def cmd_bridge(args: argparse.Namespace) -> int:
    _on_path()
    from desmos.bridge import serve

    return serve(Path(args.cwd).resolve())


def cmd_tui(args: argparse.Namespace) -> int:
    _on_path()
    import shutil
    import subprocess

    cargo = shutil.which("cargo")
    if cargo is None:
        print("desmos tui needs rustc/cargo (the grok-build inline viewport is Rust)")
        return 1
    root = _repo_root()
    manifest = root / "crates" / "desmos-tui" / "Cargo.toml"
    cmd = [
        cargo,
        "run",
        "--quiet",
        "--manifest-path",
        str(manifest),
        "--",
        "--python",
        sys.executable,
        "--cwd",
        str(Path(args.cwd).resolve()),
    ]
    return subprocess.call(cmd, cwd=str(root))


def main() -> int:
    p = argparse.ArgumentParser(prog="desmos")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("console", help="IPython with step() bound")
    c.add_argument("--cwd", default=".")
    c.set_defaults(func=cmd_console)

    k = sub.add_parser("kernel", help="install a Jupyter kernelspec named Desmos")
    k.set_defaults(func=cmd_kernel)

    ch = sub.add_parser("check", help="run harness self-check")
    ch.set_defaults(func=cmd_check)

    r = sub.add_parser("run", help="one-shot task, no IPython")
    r.add_argument("task")
    r.add_argument("--model", default=os.environ.get("DESMOS_MODEL") or "claude-opus-5")
    r.add_argument("--max-turns", type=int, default=32)
    r.add_argument("--max-tokens", type=int, default=8192)
    r.add_argument("--cwd", default=".")
    r.add_argument("--out", default="")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("bridge", help="JSONL stdio bridge (used by desmos-tui)")
    b.add_argument("--cwd", default=".")
    b.set_defaults(func=cmd_bridge)

    t = sub.add_parser("tui", help="grok-minimal TUI over the kernel")
    t.add_argument("--cwd", default=".")
    t.set_defaults(func=cmd_tui)

    args = p.parse_args()
    if args.cmd == "run" and not args.out:
        from datetime import datetime, timezone

        args.out = str(Path("runs") / f"task-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    return args.func(args)
