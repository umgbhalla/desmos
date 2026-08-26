"""End-to-end: real complete() HTTP against the local Anthropic mock.

Not complete_fn. A subprocess runs `desmos run` with ANTHROPIC_BASE_URL
pointed at MockAnthropic, so a hardcoded api.anthropic.com URL fails this
group. Optional termctrl PTY wrap when the binary is on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from desmos.transport.complete import anthropic_messages_url, complete, text_of
from desmos.transport.mock import MockAnthropic

REPO = Path(__file__).resolve().parents[2]
MARKER = "MOCK_E2E_OK"
BASH_MARKER = "MOCK_BASH_OK"


def _child_env(tmp: Path, base_url: str) -> dict[str, str]:
    env = dict(os.environ)
    settings = tmp / "settings.json"
    settings.write_text(
        json.dumps({"provider": "anthropic", "model": "claude-opus-5", "effort": "low"}),
        encoding="utf-8",
    )
    env.update(
        {
            "HOME": str(tmp),
            "DESMOS_SETTINGS": str(settings),
            "DESMOS_REGISTRY": str(tmp / "registry"),
            "DESMOS_AUTH": str(tmp / "auth.json"),
            "DESMOS_TRAJECTORY": str(tmp / "trajectory"),
            "DESMOS_MODEL": "claude-opus-5",
            "DESMOS_TOOL_SYSCALLS": "0",
            "ANTHROPIC_API_KEY": "mock-key",
            "ANTHROPIC_BASE_URL": base_url,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONPATH": str(REPO) + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )
    env.pop("OPENAI_API_KEY", None)
    return env


def _check_complete_hits_mock() -> None:
    from desmos.transport import complete as complete_mod

    with tempfile.TemporaryDirectory(prefix="desmos-e2e-complete-") as raw:
        tmp = Path(raw)
        old_traj = complete_mod.TRAJECTORY_DIR
        complete_mod.TRAJECTORY_DIR = str(tmp / "trajectory")
        old = {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")}
        try:
            with MockAnthropic([MARKER]) as mock:
                os.environ["ANTHROPIC_API_KEY"] = "mock-key"
                os.environ["ANTHROPIC_BASE_URL"] = mock.url
                assert anthropic_messages_url() == f"{mock.url}/v1/messages"
                out = complete(
                    "claude-opus-5",
                    "abi\n\n# tools\ncat",
                    [{"role": "user", "content": "hi"}],
                    64,
                )
                assert text_of(out) == MARKER, text_of(out)
                assert mock.hits, "complete() never reached the mock"
                hit = mock.hits[0]
                assert hit["path"] == "/v1/messages", hit
                assert hit["has_api_key"] is True, hit
                assert hit["n_messages"] >= 1, hit
        finally:
            complete_mod.TRAJECTORY_DIR = old_traj
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _run_cli(tmp: Path, env: dict[str, str], task: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "desmos",
            "run",
            task,
            "--cwd",
            str(tmp),
            "--max-turns",
            "4",
        ],
        cwd=str(tmp),
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )


def _check_run_cli() -> None:
    with tempfile.TemporaryDirectory(prefix="desmos-e2e-") as raw:
        tmp = Path(raw)
        with MockAnthropic([MARKER]) as mock:
            env = _child_env(tmp, mock.url)
            proc = _run_cli(tmp, env, "say only the marker, nothing else")
            assert proc.returncode == 0, (proc.stdout, proc.stderr)
            assert MARKER in proc.stdout, proc.stdout
            assert mock.hits, "desmos run never POSTed /v1/messages"
            assert all(h["path"] == "/v1/messages" for h in mock.hits), mock.hits


def _check_syscall_roundtrip() -> None:
    xml = f'<exec op="bash">echo {BASH_MARKER}</exec>'
    with tempfile.TemporaryDirectory(prefix="desmos-e2e-sh-") as raw:
        tmp = Path(raw)
        with MockAnthropic([xml, f"done {BASH_MARKER}"]) as mock:
            env = _child_env(tmp, mock.url)
            proc = _run_cli(tmp, env, "run the echo")
            assert proc.returncode == 0, (proc.stdout, proc.stderr)
            assert BASH_MARKER in proc.stdout, proc.stdout
            assert len(mock.hits) >= 2, mock.hits


def _termctrl_bin() -> str | None:
    candidates = [
        os.environ.get("TERMCTRL_BINARY"),
        shutil.which("termctrl"),
        str(Path.home() / ".local" / "bin" / "termctrl"),
    ]
    for raw in candidates:
        if raw and Path(raw).is_file() and os.access(raw, os.X_OK):
            return raw
    return None


def _check_termctrl_pty() -> None:
    binary = _termctrl_bin()
    if binary is None:
        print("[check] e2e: termctrl not on PATH; HTTP path still ran")
        return
    # desmos run is a scrolling log, not an alt-screen TUI. wait-for reads
    # the visible viewport, so a 32-row capture misses MOCK_E2E_OK once the
    # summary JSON fills the frame. logs is the scrollback.
    name = f"desmos-e2e-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="desmos-e2e-tui-") as raw:
        tmp = Path(raw)
        with MockAnthropic([MARKER]) as mock:
            env = _child_env(tmp, mock.url)
            started = subprocess.run(
                [
                    binary,
                    "start",
                    name,
                    "--cols",
                    "120",
                    "--rows",
                    "40",
                    "--cwd",
                    str(tmp),
                    "--",
                    sys.executable,
                    "-B",
                    "-m",
                    "desmos",
                    "run",
                    "say only the marker",
                    "--cwd",
                    str(tmp),
                    "--max-turns",
                    "4",
                ],
                cwd=str(tmp),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            try:
                assert started.returncode == 0, (started.stdout, started.stderr)
                deadline = time.time() + 20
                logs = ""
                while time.time() < deadline:
                    got = subprocess.run(
                        [binary, "logs", name],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    logs = got.stdout or ""
                    if MARKER in logs:
                        break
                    time.sleep(0.1)
                assert MARKER in logs, logs[-4000:]
                assert mock.hits, "termctrl-wrapped desmos run never hit the mock"
            finally:
                subprocess.run(
                    [binary, "stop", name],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )


def check() -> None:
    _check_complete_hits_mock()
    _check_run_cli()
    _check_syscall_roundtrip()
    _check_termctrl_pty()
