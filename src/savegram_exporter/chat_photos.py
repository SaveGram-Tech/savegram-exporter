"""The chat list's photos, asked for as their rows come into view, as Telegram's apps do.

Each chat is asked once, a few at a time and spaced out, and none while an export runs: it
downloads through the same account, within the same limits. The photos stay in memory.
"""
import asyncio
from collections import deque
from contextlib import contextmanager

from telethon import errors

# Photos asked for at once, and seconds between two requests: less often than an export asks
# for its files (telegram.FILE_INTERVAL).
PARALLEL = 3
INTERVAL = 0.1
# A FloodWait longer than this ends the list's photos until the next sign-in: the initials stay.
LONGEST_WAIT = 60


class ChatPhotos:
    def __init__(self, arrived):
        self.arrived = arrived  # (key, bytes) once a photo has come.
        self.ready = asyncio.Event()  # Cleared while an export runs.
        self.ready.set()
        self.pace = asyncio.Lock()  # One request at a time waits for its turn.
        self.holds = 0
        self.tasks = set()
        self.reset()

    def reset(self, fetch=None):
        """Forget every chat asked for; ask `fetch` (key -> bytes or None) from now on."""
        self.fetch, self.asked, self.queue = fetch, set(), deque()
        self.next_turn = self.hold_until = 0.0
        self.stopped = False

    def want(self, key):
        """Ask for a chat's photo, unless it was asked for already."""
        if self.fetch is None or self.stopped or key in self.asked:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # Drawn outside the event loop: asked when drawn inside it.
        self.asked.add(key)
        self.queue.append(key)
        self.tasks = {task for task in self.tasks if not task.done()}
        if len(self.tasks) < PARALLEL:
            self.tasks.add(asyncio.create_task(self._work()))

    @contextmanager
    def held(self):
        """No photo is asked for inside this; the ones on their way still come."""
        self.holds += 1
        self.ready.clear()
        try:
            yield
        finally:
            self.holds -= 1
            if not self.holds:
                self.ready.set()

    async def stop(self):
        """Ask for nothing more, and wait until the requests on their way are called off."""
        self.reset()
        tasks, self.tasks = self.tasks, set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _work(self):
        loop = asyncio.get_running_loop()
        fetch = self.fetch
        while self.queue and fetch is self.fetch and not self.stopped:
            await self.ready.wait()
            if self.hold_until > loop.time():
                await asyncio.sleep(self.hold_until - loop.time())
                continue
            # The next turn comes INTERVAL after a request really starts. Turns booked ahead let a
            # request that woke late (the window painting on this loop) start close to the next.
            async with self.pace:
                await asyncio.sleep(max(0.0, self.next_turn - loop.time()))
                self.next_turn = loop.time() + INTERVAL
            if not self.ready.is_set() or self.hold_until > loop.time():
                continue  # An export began, or Telegram asked to wait, meanwhile.
            if not self.queue or fetch is not self.fetch or self.stopped:
                return
            key = self.queue.popleft()
            try:
                data = await fetch(key)
            except errors.FloodError as exc:
                seconds = getattr(exc, "seconds", None)
                if fetch is not self.fetch:
                    return
                if seconds is None or seconds > LONGEST_WAIT:
                    self.stopped = True
                    self.queue.clear()
                    return
                # Telegram refuses every request until then; this chat is asked again after it.
                self.hold_until = max(self.hold_until, loop.time() + seconds + 1)
                self.queue.appendleft(key)
                continue
            except Exception:
                continue  # A picture in a list is never worth an error: the initials stay.
            if data and fetch is self.fetch:
                self.arrived(key, data)
