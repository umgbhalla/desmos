"""Background tasks a step can return from and be resumed by.

A blocking wait inside a syscall spends the model's turn doing nothing: the
transcript stops, the user cannot see partial progress, and a queued follow-up
sits behind a call that is only sleeping. Nothing here blocks the model. A task
is submitted, the turn ends normally, and the step is resumed by the loop when
the task actually produces something.
"""

from __future__ import annotations

import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

_SEQ = itertools.count(1)
_LOCK = threading.Lock()
_BY_WORLD: dict[int, list["Task"]] = {}


@dataclass
class Task:
    id: str
    name: str
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    output: str = ""
    error: str = ""
    delivered: bool = False

    def summary(self) -> str:
        secs = round(time.monotonic() - self.started, 2)
        if self.error:
            return f"{self.name} [{self.id}] failed after {secs}s: {self.error}"
        return f"{self.name} [{self.id}] finished after {secs}s\n{self.output}"


def _bucket(world: Any) -> list[Task]:
    return _BY_WORLD.setdefault(id(world), [])


def submit(world: Any, name: str, fn: Callable[[], Any]) -> Task:
    """Run fn on its own thread; the loop resumes the step when it lands."""
    task = Task(id=f"t{next(_SEQ)}", name=name, started=time.monotonic())

    def body() -> None:
        try:
            task.output = "" if fn is None else str(fn() or "")
        except Exception as exc:  # noqa: BLE001
            task.error = f"{type(exc).__name__}: {exc}"
            task.output = traceback.format_exc(limit=3)
        finally:
            task.done.set()

    with _LOCK:
        _bucket(world).append(task)
    threading.Thread(target=body, name=f"pending-{task.id}", daemon=True).start()
    return task


def register(world: Any, name: str, wait_fn: Callable[[], Any]) -> Task:
    """Adopt work that is already running elsewhere; wait_fn blocks on it."""
    return submit(world, name, wait_fn)


def outstanding(world: Any) -> list[Task]:
    with _LOCK:
        return [t for t in _bucket(world) if not t.delivered]


def count(world: Any) -> int:
    return len(outstanding(world))


def take_done(world: Any) -> list[Task]:
    """Undelivered tasks that have finished. Marks them delivered."""
    ready: list[Task] = []
    with _LOCK:
        for task in _bucket(world):
            if task.delivered or not task.done.is_set():
                continue
            task.delivered = True
            ready.append(task)
    return ready


def clear(world: Any) -> None:
    with _LOCK:
        _BY_WORLD.pop(id(world), None)


def wait_next(
    world: Any,
    *,
    stop: Callable[[], bool] | None = None,
    interrupt: Callable[[], bool] | None = None,
    timeout: float | None = None,
    poll: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Task]:
    """Poll until a task lands, the user interrupts, or the deadline passes.

    Returns the tasks that finished, or an empty list for every other exit.
    Polling is what keeps a stop and a queued follow-up responsive: neither can
    be seen by a thread parked in join().
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        ready = take_done(world)
        if ready:
            return ready
        if not outstanding(world):
            return []
        if stop is not None and stop():
            return []
        if interrupt is not None and interrupt():
            return []
        if deadline is not None and time.monotonic() >= deadline:
            return []
        sleep(poll)


def notice(tasks: list[Task]) -> str:
    """The user-role text a resumed step reads."""
    body = "\n\n".join(task.summary() for task in tasks)
    head = "background task finished" if len(tasks) == 1 else f"{len(tasks)} background tasks finished"
    return f"[{head} while you were away]\n{body}"
