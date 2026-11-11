"""watchdog -> debounced incremental re-index.

A polling observer, because inotify events from the host do not cross Docker
Desktop bind mounts. Any change schedules one sync after a quiet period; the
sync itself compares content hashes, so only files that really changed are
re-embedded (a `git checkout` touching ten files re-embeds only those whose
content differs).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver


class _Handler(FileSystemEventHandler):
    def __init__(self, poke: Callable[[str], None]):
        self._poke = poke

    def on_any_event(self, event: FileSystemEvent) -> None:
        if "/.git" not in str(event.src_path) and "__pycache__" not in str(event.src_path):
            self._poke(str(event.src_path))


class DebouncedWatcher:
    def __init__(self, root: str, on_quiet: Callable[[], None], debounce: Callable[[], float],
                 poll: float, on_error: Callable[[Exception], None]):
        self.root, self._on_quiet, self._debounce = root, on_quiet, debounce
        self._on_error = on_error
        self._last_change: float | None = None
        self._cond = threading.Condition()
        self._observer = PollingObserver(timeout=poll)
        self._observer.schedule(_Handler(self.poke), root, recursive=True)

    def poke(self, _path: str = "") -> None:
        with self._cond:
            self._last_change = time.monotonic()
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._last_change is None:
                    self._cond.wait()
                wait = self._last_change + self._debounce() - time.monotonic()
                if wait > 0:
                    self._cond.wait(wait)
                    continue
                self._last_change = None
            try:
                self._on_quiet()
            except Exception as exc:  # e.g. Ollama down while embedding: retry later
                self._on_error(exc)
                time.sleep(max(self._debounce(), 1.0) * 5)
                self.poke()

    def start(self) -> None:
        self._observer.start()
        threading.Thread(target=self._run, name="reindex", daemon=True).start()
