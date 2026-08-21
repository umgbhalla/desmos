"""Fast smoke over the harbor-shaped self-improvement trials (evals/).

Runs each trial verifier exactly the way evals/run.py does -- a fresh
subprocess in an isolated temp cwd, reward read back from reward.txt --
so the suite catches a trial rotting without waiting for a harbor run.
Budget: the three verifiers together stay under ~5s.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def check() -> None:
    trials = sorted((REPO / "evals" / "trials").glob("*/verifier/verify.py"))
    assert len(trials) == 3, f"expected 3 trials, found {trials}"
    env = dict(os.environ, DESMOS_REPO=str(REPO))
    for verify in trials:
        name = verify.parent.parent.name
        with tempfile.TemporaryDirectory(prefix="desmos-evalsmoke-") as cwd:
            proc = subprocess.run(
                [sys.executable, "-B", str(verify)],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            reward_file = Path(cwd) / "reward.txt"
            assert reward_file.is_file(), (name, proc.stdout, proc.stderr)
            reward = float(reward_file.read_text(encoding="utf-8").strip())
            assert reward == 1.0, (name, reward, proc.stdout, proc.stderr)
