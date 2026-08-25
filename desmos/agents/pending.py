"""Background tasks a step can return from and be resumed by.

A blocking wait inside a syscall spends the model's turn doing nothing: the
transcript stops, the user cannot see partial progress, and a queued follow-up
sits behind a call that is only sleeping. Nothing here blocks the model. A task
is submitted, the turn ends normally, and the step is resumed by the loop when
the task actually produces something.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from desmos.state.persist import atomic_write, save

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
    #: A quiet task lands without waking the step. Its result has already
    #: reached whoever wanted it -- a resident agent's reply is posted to the
    #: channel by the task itself -- so a notice would only be accounting the
    #: chief agent has to spend a turn reading. wait_next takes quiet tasks
    #: and keeps parking; they get no handoff file because there is no notice
    #: to replay.
    quiet: bool = False
    #: Durable handoff file for the root world's tasks: written the moment the
    #: task settles, renamed into delivered/ only after commit() has saved the
    #: transcript that carries its notice. None for a non-persistent world's
    #: tasks. The file stem is the task id, and the id is in the notice text:
    #: that pair is what lets replay() dedupe instead of double-delivering.
    path: Path | None = None

    def summary(self) -> str:
        secs = round(time.monotonic() - self.started, 2)
        if self.error:
            return f"{self.name} [{self.id}] failed after {secs}s: {self.error}"
        return f"{self.name} [{self.id}] finished after {secs}s\n{self.output}"


def _bucket(world: Any) -> list[Task]:
    return _BY_WORLD.setdefault(id(world), [])


def _handoff_dir(world: Any) -> Path | None:
    """Where this world's undelivered notices live on disk, or None.

    Only the ROOT persistent world gets the durable handoff. A child World is
    persist=False by contract: its whole result reaches the parent as a
    pending notice registered ON THE PARENT, so that notice is durable through
    the parent's own handoff -- while a child's inner tasks belong to a
    transcript that dies with the child's process, and a durable file for
    them would be replayed into a session that no longer exists.
    """
    if not getattr(world, "persist", False):
        return None
    cwd = getattr(world, "cwd", None)
    return Path(cwd) / ".desmos" / "pending" if cwd else None


def submit(world: Any, name: str, fn: Callable[[], Any], quiet: bool = False) -> Task:
    """Run fn on its own thread; the loop resumes the step when it lands."""
    # The uuid suffix keeps two processes on one cwd from colliding on the
    # per-process task counter, and doubles as the durable notice id: it is
    # the handoff file's stem AND appears in the notice text, so replay can
    # tell "already in the transcript" from "never delivered".
    task = Task(
        id=f"t{next(_SEQ)}-{uuid.uuid4().hex[:8]}", name=name,
        started=time.monotonic(), quiet=quiet,
    )
    handoff = _handoff_dir(world)

    def body() -> None:
        try:
            task.output = "" if fn is None else str(fn() or "")
        except Exception as exc:  # noqa: BLE001
            task.error = f"{type(exc).__name__}: {exc}"
            task.output = traceback.format_exc(limit=3)
        finally:
            try:
                if handoff is not None and not quiet:
                    # Durable before visible: the file lands before done is
                    # set, so from the first instant a waiter can see this
                    # task, a kill can no longer lose its result -- load
                    # replays the file.
                    path = handoff / f"{task.id}.json"
                    atomic_write(path, json.dumps({"notice": notice([task])}))
                    task.path = path
            finally:
                # A failed disk write still lands the task in memory; the
                # exception escapes to this thread's stderr instead of being
                # swallowed, and done is set on every path so nothing hangs.
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


def labels(world: Any) -> list[str]:
    """Names of the work still outstanding, id included, for the meta pane.

    A bare count is not actionable -- "1 pending" says nothing about whether
    it is a 30s build or a wedged shell. `name [id]` is what the <shell>
    monitor and the resume notice already print, so the pane reads the same.
    """
    return [f"{t.name} [{t.id}]" for t in outstanding(world)]


def _deliver_file(task: Task) -> None:
    """Rename the handoff file into delivered/.

    Never called before the transcript carrying the notice is durably saved:
    commit() and replay() own that ordering. A file in delivered/ means "the
    saved transcript has this notice", always.
    """
    if task.path is None:
        return
    target = task.path.parent / "delivered" / task.path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(task.path, target)
    except FileNotFoundError:
        # Someone removed .desmos out from under a live run. The notice is in
        # this process's hands right now, so nothing is lost -- there is just
        # no file left to commit.
        return
    task.path = target


def take_done(world: Any) -> list[Task]:
    """Undelivered tasks that have finished. Marks them taken in memory.

    Taking is NOT the durable commit. The handoff file stays in pending/
    until the caller has appended notice(tasks) to world.messages and called
    commit(world, tasks), which saves the transcript and only then renames.
    A kill anywhere before that save leaves the file in pending/ for replay
    to deliver; a kill after the save is deduped by the notice id replay
    finds already in the transcript. The in-memory delivered flag only stops
    this process from taking the same task twice.
    """
    ready: list[Task] = []
    with _LOCK:
        for task in _bucket(world):
            if task.delivered or not task.done.is_set():
                continue
            task.delivered = True
            ready.append(task)
    return ready


def commit(world: Any, tasks: list[Task]) -> None:
    """Durably commit delivered notices: save the transcript, then rename.

    The caller has already appended notice(tasks) to world.messages. Save
    first, rename second -- delivered/ is only ever reached after the notice
    is on disk. Killed before the save: every file is still in pending/ and
    replay appends the notice once. Killed between the save and a rename:
    the file is in pending/ but its id is in the saved transcript, so replay
    renames without appending. Either way, exactly once.
    """
    save(world)
    for task in tasks:
        _deliver_file(task)


def replay(world: Any) -> int:
    """Deliver notices a previous process settled but never committed.

    A file still in pending/ is a notice whose delivery was never durably
    committed -- but the transcript may already carry it (a kill between
    commit's save and its rename). Each file's stem is its task id, and the
    notice text carries that id, so: id already in the loaded messages means
    rename only; otherwise append the notice. One save covers every append,
    and every rename runs only after it, so a kill at any point either
    leaves the file in pending/ (redelivered, deduped next load) or in
    delivered/ with the notice saved. Returns how many notices were appended.
    """
    base = _handoff_dir(world)
    if base is None or not base.is_dir():
        return 0
    files = sorted(base.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name))
    if not files:
        return 0
    seen = "\n".join(
        m["content"]
        for m in world.messages
        if m.get("role") == "user" and isinstance(m.get("content"), str)
    )
    appended: list[Path] = []
    already: list[Path] = []
    for path in files:
        if path.stem in seen:
            already.append(path)
            continue
        try:
            text = json.loads(path.read_text(encoding="utf-8")).get("notice")
        except (OSError, ValueError) as exc:
            text = f"[a background task from a previous session finished, but its notice at {path} could not be read: {exc}]"
        if not isinstance(text, str) or not text:
            text = f"[a background task from a previous session finished, but its notice at {path} was empty]"
        world.messages.append({"role": "user", "content": text})
        appended.append(path)
    if appended:
        save(world)
    delivered = base / "delivered"
    delivered.mkdir(parents=True, exist_ok=True)
    for path in already + appended:
        os.replace(path, delivered / path.name)
    return len(appended)


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
        ready = [t for t in take_done(world) if not t.quiet]
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
