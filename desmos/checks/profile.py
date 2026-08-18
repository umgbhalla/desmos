"""Where the floor's seconds go, attributed to the check line that spends them.

The group timings said kernel was 15s of a 33s floor and stopped there: one
number for 1181 lines. cProfile did better and still blamed `select.select`,
which is true and useless -- the question is *which repro* waits, not which
syscall it waits in.

Two wrong turns are worth keeping, because both produced a confident number
that was false:

- **Wrapping the primitives.** The first version spied on `select.select` and
  `time.sleep`. It worked on a hand-built probe and then reported *nothing at
  all* for a kernel group that had just spent 14.7s, with the spies verifiably
  still installed. A wrapper only sees the primitives you thought of.
- **Module-global counters.** The sampler that replaced it kept its tallies in
  this module's globals -- and the kernel group reloads the SDK in the middle
  of its run. Reload re-executes the module body in the same namespace, so the
  counters were rebound to empty dicts two thirds of the way through, and the
  report named the last three samples with perfect confidence: 66.7% and 33.3%
  of a fourteen-second run, from two samples and one.

So the state lives on the sampler object, which a caller holds in a local
frame that no reload can touch, and nothing is counted by category: a daemon
thread reads the main thread's stack every 20ms and credits the sample to the
innermost frame inside `desmos/checks`. It does not care how the time is spent
-- select, waitpid, a lock, a subprocess, pure computation -- which is the
point: the measurement stops depending on the theory it is meant to test.

Two stated limits. Only the main thread is sampled, because summing threads
reports more seconds than the clock has; work on a worker thread shows up at
whatever line waits for it, which is the line you would change anyway. And
20ms is the floor: a repro faster than that is not why the floor is slow.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

#: Sampling period. 20ms over a 30s floor is 1500 samples: ample for a report
#: whose smallest actionable unit is a tenth of a second, and cheap enough that
#: the sampler never shows up in its own numbers.
INTERVAL_S = 0.02


def _blame(frame: Any) -> str:
    """The innermost check frame on this stack; the innermost frame otherwise."""
    fallback = None
    while frame is not None:
        name = str(frame.f_code.co_filename)
        if fallback is None:
            fallback = Path(name).name + ":" + str(frame.f_lineno)
        if "desmos/checks/" in name and not name.endswith("profile.py"):
            return Path(name).name + ":" + str(frame.f_lineno)
        frame = frame.f_back
    return fallback or "?"


class Sampler(threading.Thread):
    """Samples the main thread until halted. Owns its own tallies."""

    def __init__(self, interval: float = INTERVAL_S) -> None:
        super().__init__(name="check-sampler", daemon=True)
        self.interval = float(interval)
        self.samples: dict[str, int] = defaultdict(int)
        self.wall = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started_at = 0.0

    def run(self) -> None:
        target = threading.main_thread().ident
        while not self._stop.is_set():
            frame = sys._current_frames().get(target)
            if frame is not None:
                site = _blame(frame)
                with self._lock:
                    self.samples[site] += 1
            self._stop.wait(self.interval)

    def start(self) -> None:
        self._started_at = time.monotonic()
        super().start()

    def halt(self) -> None:
        self._stop.set()
        self.join(timeout=1.0)
        self.wall = time.monotonic() - self._started_at

    def sites(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            rows = sorted(self.samples.items(), key=lambda kv: -kv[1])[:limit]
            total = sum(self.samples.values())
        if not total:
            return []
        wall = self.wall or (time.monotonic() - self._started_at)
        return [
            {"site": site, "samples": n, "seconds": wall * n / total,
             "share": n / total}
            for site, n in rows
        ]

    def report(self, limit: int = 12) -> str:
        rows = self.sites(limit)
        if not rows:
            return "[profile] no samples"
        with self._lock:
            total = sum(self.samples.values())
        head = "[profile] {:.1f}s of wall clock over {} samples, by the check " \
               "line holding it:".format(self.wall, total)
        lines = [head]
        for row in rows:
            lines.append(
                "  {:6.2f}s  {:5.1f}%  {}".format(
                    row["seconds"], row["share"] * 100, row["site"]
                )
            )
        return chr(10).join(lines)


@contextmanager
def profiling(interval: float = INTERVAL_S) -> Iterator[Sampler]:
    """Sample for the duration and hand back the sampler. It always stops.

    The caller holds the sampler in a local, which is what makes the tallies
    survive a reload of this module partway through the run.
    """
    sampler = Sampler(interval)
    sampler.start()
    try:
        yield sampler
    finally:
        sampler.halt()
