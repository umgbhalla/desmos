"""Local runner for the harbor-shaped self-improvement trials.

Discovers evals/trials/*/verifier/verify.py, runs each verifier in an
isolated temp cwd (the harbor contract: the verifier writes a scalar to
reward.txt in its working directory), prints one line per trial plus a
total, and exits nonzero if any trial scores 0.

No Docker, no model calls: each verifier drives the desmos SDK directly
against a real temp workspace. evals/adapter.py is the seam where the
real harbor harness plugs in later.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TRIALS = REPO / "evals" / "trials"


def discover() -> list[Path]:
    return sorted(p.parent.parent for p in TRIALS.glob("*/verifier/verify.py"))


def run_trial(trial: Path) -> float:
    verify = trial / "verifier" / "verify.py"
    env = dict(os.environ, DESMOS_REPO=str(REPO))
    with tempfile.TemporaryDirectory(prefix="desmos-eval-") as cwd:
        proc = subprocess.run(
            [sys.executable, "-B", str(verify)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        reward_file = Path(cwd) / "reward.txt"
        if not reward_file.is_file():
            sys.stderr.write(proc.stdout + proc.stderr)
            return 0.0
        try:
            reward = float(reward_file.read_text(encoding="utf-8").strip())
        except ValueError:
            return 0.0
        if reward == 0.0:
            sys.stderr.write(proc.stdout + proc.stderr)
        return reward


def main() -> int:
    trials = discover()
    if not trials:
        print("no trials found under", TRIALS)
        return 1
    total = 0.0
    passed = 0
    for trial in trials:
        reward = run_trial(trial)
        total += reward
        passed += 1 if reward > 0 else 0
        print(f"{trial.name}: reward={reward}")
    print(f"total: {passed}/{len(trials)} trials passed, reward sum {total}")
    return 0 if passed == len(trials) else 1


if __name__ == "__main__":
    raise SystemExit(main())
