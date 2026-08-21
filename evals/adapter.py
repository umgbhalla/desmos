"""Harbor adapter stub: the seam where the real harbor harness plugs in.

harbor (laude-institute, the terminal-bench substrate) is NOT vendored.
Its documented adapter surface is four methods over a trial directory
(instruction.md, task.toml, an oracle solution, a verifier writing a
scalar to reward.txt). This class maps those four methods onto the local
runner so that, when harbor is installed, replacing the bodies below
with harbor's Trial/Environment API is the entire integration: the trial
dirs under evals/trials/ are already in harbor layout and do not change.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from run import discover, run_trial  # evals/run.py owns execution today


class DesmosHarborAdapter:
    """Four methods: setup / run / verify / teardown."""

    def __init__(self, trial_dir: str | Path) -> None:
        self.trial = Path(trial_dir)
        self.workdir: Path | None = None
        self.reward: float | None = None

    def setup(self) -> None:
        """Harbor: build the task environment. Local: a temp cwd.

        Real harbor would materialize the container/VM described by
        task.toml here.
        """
        self.workdir = Path(tempfile.mkdtemp(prefix="harbor-local-"))

    def run(self) -> None:
        """Harbor: hand instruction.md to the agent under test.

        Local trials embed the oracle gesture inside the verifier (no
        model calls), so run() is a no-op; the real harness replaces
        this with its agent rollout.
        """

    def verify(self) -> float:
        """Harbor: execute the verifier, collect reward.txt's scalar."""
        self.reward = run_trial(self.trial)
        return self.reward

    def teardown(self) -> None:
        """Harbor: destroy the environment. Local temp dirs self-clean."""
        self.workdir = None


def all_adapters() -> list[DesmosHarborAdapter]:
    return [DesmosHarborAdapter(t) for t in discover()]
