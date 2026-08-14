"""desmos console / kernel / check / run / acp."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from desmos.const import MAX_TOKENS


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


def cmd_acp(_args: argparse.Namespace) -> int:
    _on_path()
    from desmos.acp import serve

    cwd = Path(os.environ.get("DESMOS_CWD") or os.environ.get("PWD") or ".").resolve()
    return serve(cwd=cwd)


def cmd_tui(args: argparse.Namespace) -> int:
    _on_path()
    import shutil
    import subprocess

    cargo = shutil.which("cargo")
    if cargo is None:
        print("desmos tui needs cargo")
        return 1
    root = _repo_root()
    cwd = str(Path(args.cwd).resolve())
    grok = root / "vendor" / "grok-build"
    if not grok.is_dir():
        print("clone grok-build into vendor/grok-build")
        return 1
    env = _tui_build_env()
    if getattr(args, "grok", False):
        env["DESMOS_ACP"] = f"{sys.executable} -m desmos acp"
        env["DESMOS_CWD"] = cwd
        cmd = [
            cargo,
            "run",
            "-p",
            "xai-grok-pager-bin",
            *([] if getattr(args, "debug", False) else ["--release"]),
            "--",
            "--minimal",
            "--no-leader",
            "--cwd",
            cwd,
        ]
        return subprocess.call(cmd, cwd=str(grok), env=env)
    release = not getattr(args, "debug", False)
    profile = "release" if release else "debug"
    bin_path = _tui_binary(root, release)
    if bin_path is None:
        print(f"building desmos-tui ({profile})…", file=sys.stderr)
        built = _tui_compile(cargo, root, env, release)
        if built != 0:
            return built
        bin_path = root / "target" / profile / "desmos-tui"
        if not bin_path.is_file():
            print("desmos-tui binary missing after build", file=sys.stderr)
            return 1
    cmd = [str(bin_path), "--python", sys.executable, "--cwd", cwd]
    if getattr(args, "demo", False):
        cmd.append("--demo")
    os.execve(str(bin_path), cmd, env)
    return 1


def _tui_build_cmd(cargo: str, release: bool = True) -> list[str]:
    """Workspace build of desmos-tui only. No --quiet: a pager rebuild must be visible."""
    cmd = [cargo, "build", "-p", "desmos-tui"]
    if release:
        cmd.append("--release")
    return cmd


def _tui_build_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Launch env.

    Do not inject RUSTFLAGS — extra rustc flags hash into every crate
    fingerprint and rebuild the entire grok-build pager graph.

    PROTOC must be an existing absolute file. grok-build's proto script
    emits cargo:rerun-if-changed=$PROTOC; a bare ``protoc`` is a missing
    crate-local path and cargo rebuilds tools-api → pager every launch.
    """
    import shutil

    env = dict(os.environ if base is None else base)
    env["RUSTUP_TOOLCHAIN"] = "1.97.1"
    env.setdefault("COLORTERM", "truecolor")
    env.pop("CARGO_TERM_QUIET", None)
    current = env.get("PROTOC", "")
    if not current or not Path(current).is_file():
        wrapper = _repo_root() / "scripts" / "protoc"
        if wrapper.is_file():
            env["PROTOC"] = str(wrapper)
        else:
            found = shutil.which("protoc")
            if found:
                env["PROTOC"] = found
    return env


def _tui_stabilize_fingerprints(root: Path) -> list[Path]:
    """Pager build.rs emits cargo:rerun-if-changed=.git/HEAD relative to the
    crate, not vendor/grok-build. A missing file makes cargo rebuild the
    pager rlib on every invocation."""
    real = root / "vendor" / "grok-build" / ".git" / "HEAD"
    written: list[Path] = []
    for name in ("xai-grok-pager", "xai-grok-pager-bin"):
        head = (
            root
            / "vendor"
            / "grok-build"
            / "crates"
            / "codegen"
            / name
            / ".git"
            / "HEAD"
        )
        if not head.is_file():
            head.parent.mkdir(parents=True, exist_ok=True)
            if real.is_file():
                head.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                head.write_text("ref: refs/heads/main\n", encoding="utf-8")
        written.append(head)
    return written


def _tui_compile(
    cargo: str, root: Path, env: dict[str, str], release: bool = True
) -> int:
    import subprocess

    _tui_stabilize_fingerprints(root)
    # Hash what cargo is about to read, not what is on disk when it finishes —
    # an edit landing mid-build must not be stamped as already compiled.
    before = _tui_hash(root)
    build = _tui_build_cmd(cargo, release)
    built = subprocess.call(build + ["--offline"], cwd=str(root), env=env)
    if built != 0:
        built = subprocess.call(build, cwd=str(root), env=env)
    if built == 0:
        stamp = _tui_stamp_path(root, release)
        if stamp.parent.is_dir():
            stamp.write_text(before, encoding="utf-8")
    return built


def _tui_watch_roots(root: Path) -> list[Path]:
    """Our crates only. Never walk vendor/grok-build — that is why launch was slow."""
    return [
        root / "crates" / "desmos-tui",
        root / "crates" / "xai-grok-markdown",
        root / "crates" / "xai-grok-markdown-core",
        root / "Cargo.toml",
        root / "Cargo.lock",
    ]


def _tui_sources(root: Path) -> list[Path]:
    """Every file whose bytes go into the binary we build."""
    files: list[Path] = []
    for path in _tui_watch_roots(root):
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(
                f
                for f in path.rglob("*")
                if f.is_file() and ".git" not in f.parts and "target" not in f.parts
            )
    return sorted(files)


def _tui_hash(root: Path) -> str:
    """Content hash of our sources.

    mtime is the wrong question: `git checkout`, a formatter run, or an editor
    save-with-no-change all bump it and cost a multi-minute pager rebuild for a
    binary that is already correct. Hash the bytes instead — same bytes, same
    binary, no rebuild.
    """
    import hashlib

    h = hashlib.sha256()
    for f in _tui_sources(root):
        h.update(str(f.relative_to(root)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def _tui_stamp_path(root: Path, release: bool) -> Path:
    profile = "release" if release else "debug"
    return root / "target" / profile / ".desmos-tui.hash"


def _tui_write_stamp(root: Path, release: bool) -> None:
    stamp = _tui_stamp_path(root, release)
    if stamp.parent.is_dir():
        stamp.write_text(_tui_hash(root), encoding="utf-8")


def _tui_stale(root: Path, binary: Path, release: bool = True) -> bool:
    """True when the binary was built from different source bytes than we have."""
    stamp = _tui_stamp_path(root, release)
    if not stamp.is_file():
        # First launch after an out-of-band `cargo build` — fall back to mtime
        # so an already-current binary is not rebuilt for want of a stamp.
        newest = max(
            (f.stat().st_mtime for f in _tui_sources(root)), default=0.0
        )
        if newest <= binary.stat().st_mtime:
            _tui_write_stamp(root, release)
            return False
        return True
    return stamp.read_text(encoding="utf-8").strip() != _tui_hash(root)


def _tui_binary(root: Path, release: bool = True) -> Path | None:
    """Reuse the last build unless our sources changed. Do not invoke cargo."""
    profile = "release" if release else "debug"
    binary = root / "target" / profile / "desmos-tui"
    if not binary.is_file():
        return None
    if _tui_stale(root, binary, release):
        return None
    return binary


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
    r.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    r.add_argument("--cwd", default=".")
    r.add_argument("--out", default="")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("bridge", help="JSONL stdio bridge (used by desmos-tui)")
    b.add_argument("--cwd", default=".")
    b.set_defaults(func=cmd_bridge)

    t = sub.add_parser("tui", help="three-pane TUI: trajectory | calls | input")
    t.add_argument("--cwd", default=".")
    t.add_argument("--demo", action="store_true", help="seed a fake turn (no API)")
    t.add_argument("--grok", action="store_true", help="launch grok-build pager via ACP instead")
    t.add_argument("--debug", action="store_true", help="dev build (default is release)")
    t.set_defaults(func=cmd_tui)

    a = sub.add_parser("acp", help="ACP stdio server for grok-build's pager")
    a.set_defaults(func=cmd_acp)

    args = p.parse_args()
    if args.cmd == "run" and not args.out:
        from datetime import datetime, timezone

        args.out = str(Path("runs") / f"task-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    return args.func(args)
