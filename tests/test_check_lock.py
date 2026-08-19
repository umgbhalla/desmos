"""The check runner's whole-run lock: a second acquirer is refused, and the
lock is released on both success and exception (todo 45)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from desmos.checks.runner import _check_lock

_TRY = (
    "from pathlib import Path\n"
    "from desmos.checks.runner import _check_lock\n"
    "with _check_lock(Path({path!r})):\n"
    "    pass\n"
)


def _second_run(path: Path) -> subprocess.CompletedProcess:
    """A real second process contending for the same lock file."""
    return subprocess.run(
        [sys.executable, "-B", "-c", _TRY.format(path=str(path))],
        capture_output=True, text=True, timeout=30,
    )


class CheckLockTest(unittest.TestCase):
    def test_second_process_refused_while_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".desmos" / "check.lock"
            with _check_lock(path):
                proc = _second_run(path)
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            self.assertIn("refusing to overlap", proc.stdout)
            self.assertIn("pid", proc.stdout)  # names the holder

    def test_reentrant_in_process(self) -> None:
        # kernel._check_profiler drives runner.run inside a held run.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".desmos" / "check.lock"
            with _check_lock(path):
                with _check_lock(path):
                    pass

    def test_released_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".desmos" / "check.lock"
            with _check_lock(path):
                pass
            proc = _second_run(path)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_released_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".desmos" / "check.lock"
            with self.assertRaises(RuntimeError):
                with _check_lock(path):
                    raise RuntimeError("suite blew up")
            proc = _second_run(path)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
