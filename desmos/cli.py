"""desmos console / kernel install."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def cmd_console(args: argparse.Namespace) -> int:
    os.chdir(Path(args.cwd).resolve())
    root = str(_repo_root())
    path = os.environ.get("PYTHONPATH", "")
    if root not in path.split(os.pathsep):
        os.environ["PYTHONPATH"] = root + (os.pathsep + path if path else "")
    os.execv(
        sys.executable,
        [sys.executable, "-m", "IPython", "--ext", "desmos.ext"],
    )


def cmd_kernel(args: argparse.Namespace) -> int:
    spec_dir = Path.home() / "Library" / "Jupyter" / "kernels" / "desmos"
    if sys.platform != "darwin":
        data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        spec_dir = Path(data) / "jupyter" / "kernels" / "desmos"
    spec_dir.mkdir(parents=True, exist_ok=True)
    root = str(_repo_root())
    env = {"PYTHONPATH": root}
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
        "env": env,
    }
    (spec_dir / "kernel.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    print(f"installed kernelspec: {spec_dir}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="desmos")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("console", help="IPython with step() bound")
    c.add_argument("--cwd", default=".")
    c.set_defaults(func=cmd_console)
    k = sub.add_parser("kernel", help="install a Jupyter kernelspec named desmos")
    k.set_defaults(func=cmd_kernel)
    args = p.parse_args()
    return args.func(args)
