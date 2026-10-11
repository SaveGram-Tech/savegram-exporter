"""Cancellable, unlimited retry waits shared by an export's concurrent operations."""
import asyncio
from pathlib import Path
import traceback


def retry_seconds(failures):
    return min(30, 2 ** min(max(0, failures - 1), 5))


class RetryState:
    def __init__(self, notify=None):
        self.notify = notify
        self.attempts = 0
        self.waiting = {}
        self.record = None
        # FloodWaits of this run by kind of request: {"request:GetFileRequest": {"count", "seconds"}}.
        self.floods = {}

    def failed(self, error, operation, details=None):
        self.attempts += 1
        seconds = getattr(error, "seconds", None)
        if type(error).__name__.startswith("Flood") and isinstance(seconds, int):
            row = self.floods.setdefault(operation, {"count": 0, "seconds": 0})
            row["count"] += 1
            row["seconds"] += seconds
        if self.record:
            diagnostic = dict(attempt=self.attempts, category=type(error).__name__, operation=operation,
                frames=[dict(file=Path(frame.filename).name, line=frame.lineno, function=frame.name)
                        for frame in traceback.extract_tb(error.__traceback__)])
            if isinstance(getattr(error, "seconds", None), int):
                diagnostic["wait_seconds"] = error.seconds
            if details:
                diagnostic["request_details"] = details
            try:
                self.record(diagnostic)
            except Exception:
                pass  # A full disk must not break the retry loop itself.

    def set_wait(self, token, phase, seconds, category):
        self.waiting[token] = (phase, seconds, category)
        self.report()

    def clear_wait(self, token):
        self.waiting.pop(token, None)
        self.report()

    def report(self):
        if self.notify:
            phase, seconds, category = max(self.waiting.values(), key=lambda value: value[1],
                                          default=(None, 0, ""))
            self.notify(phase, seconds, self.attempts, category)

    async def pause(self, error, failures, phase="network_wait", operation="export", details=None):
        self.failed(error, operation, details=details)
        token = object()
        try:
            for remaining in range(retry_seconds(failures), 0, -1):
                # Never pass exception text: it may contain Telegram or account data.
                self.set_wait(token, phase, remaining, type(error).__name__)
                await asyncio.sleep(1)
        finally:
            self.clear_wait(token)
