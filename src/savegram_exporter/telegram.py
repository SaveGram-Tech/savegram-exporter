"""Network audit boundary. All Telethon client operations are listed here.

Authentication changes the application's authorization. Export operations only
read Telegram data; no send/edit/delete/join/read-acknowledgement methods exist.
An export opens and finishes a takeout session (account.initTakeoutSession,
account.finishTakeoutSession) to read history pages through it; nothing else changes.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import Context, ContextVar
from datetime import datetime, timedelta, timezone
import errno
import math
import platform
import weakref

from telethon import TelegramClient, errors, functions, types, utils
from telethon.network import ConnectionTcpFull
from telethon.sessions import StringSession

from . import __version__
from .models import AppRejected
from .storage import validate_credentials
from .retry import RetryState

DOWNLOAD_IDLE_SECONDS = 180
FILE_REQUEST_SECONDS = 60
# The most Telegram serves per file request: a file takes fewer paced round trips than at
# Telethon's own, smaller parts.
PART_SIZE_KB = 512
# Seconds between history pages of 100: slow enough that Telegram does not ask the export to
# wait. A FloodWait holds every other request of the export too.
HISTORY_INTERVAL = 1.2
# Messages asked for at each end of the history to find its first and last one. Telegram may put a
# deleted message (MessageEmpty) there, which Telethon drops: asked for one, the export would get none
# and take the chat for empty.
BOUND_MESSAGES = 20
# Seconds between history pages read through a takeout session, Telegram's own way to export an
# account's data, which serves pages faster.
TAKEOUT_HISTORY_INTERVAL = 0.6
# What a takeout session is opened for: the messages of every kind of chat. Files are read as before.
TAKEOUT_SCOPE = dict(message_users=True, message_chats=True, message_megagroups=True, message_channels=True)
# A recent login gets no takeout session at once: Telegram sends the person a "Data export request"
# and waits a day, unless they press "Allow" in it on their phone. Then the next ask opens one at
# once. While Telegram waits, an export asks again this often, between history pages; asking never
# holds a page up.
TAKEOUT_ASK_SECONDS = 60
TAKEOUT_ASK_TIMEOUT = 15
# Before an export the window asks for the session itself, so that Telegram's message arrives while the
# window shows what it is, and then this often while the person may be pressing "Allow". An ask after the
# first sends no message of its own.
TAKEOUT_WAIT_ASK_SECONDS = 15
# Seconds between file requests, a file's start and each of its parts alike: slow enough that
# Telegram does not ask the export to wait.
FILE_INTERVAL = 0.07
# Parts of one file in flight at once: more of them come no faster.
PARALLEL_PARTS = 4
# Server responses (-5xx, TIMEOUT, FLOOD_WAIT) for one file part, without any
# byte progress in between. Telegram answers this way when its storage cannot
# serve a specific file; retrying that part forever never succeeds.
FILE_UNAVAILABLE_AFTER = 3
# A kind of request slowed by a FloodWait halves its interval again this long after its
# pace last changed, while requests keep succeeding: from the 30 s ceiling back in ~90 s.
RECOVER_SECONDS = 10
# A file part comes in a second or two. One connection to a DC can answer every part many times
# slower than a new one to the same address would, and each of its files then holds a download
# slot that long while the history waits for them. This many parts in a row this slow open the
# connection anew; the parts in flight are sent again over the new one.
SLOW_PART_SECONDS = 10
SLOW_PARTS = 3
# A connection opened anew for slow parts is not reopened again sooner than this.
REOPEN_SECONDS = 120
# How long a connection to another DC waits for an address to answer before the next one is tried.
DC_CONNECT_SECONDS = 10
# A chat list photo that has not come in this long is given up: its row keeps the initials.
CHAT_PHOTO_SECONDS = 20
# The peer photo location or entity snapshot is stale: refreshing can help. FILE_ID_INVALID is a photo
# deleted after the profile was read.
AVATAR_REFRESH_ERRORS = (errors.LocationInvalidError, errors.PeerIdInvalidError, errors.PhotoInvalidError,
                         errors.FileIdInvalidError)
# Telegram refusing the application, not the person: an API ID and API Hash it does not know or no longer
# accepts. An unknown pair still connects; Telegram refuses it at the first QR code.
APP_REFUSALS = (errors.ApiIdInvalidError, errors.ApiIdPublishedFloodError, errors.ConnectionApiIdInvalidError)
_active_gateway = ContextVar("export_gateway", default=None)
_timeouts = ContextVar("export_timeouts", default=())
_file_download = ContextVar("file_download", default=None)
# Collects the profiles a response carries while a history page is read.
_page_users = ContextVar("page_users", default=None)
# Set while one message is asked for through a takeout session to learn whether it gives this chat whole.
_takeout_trial = ContextVar("takeout_trial", default=False)


class FileUnavailable(Exception):
    """Telegram responds, but does not provide this file now. Never a connection problem.

    `details` holds only error classes, codes and location metadata: no IDs, hashes or references.
    """
    def __init__(self, category, **details):
        super().__init__(category)
        self.details = dict(category=category, **details)

    @classmethod
    def from_error(cls, error, **details):
        if isinstance(error, errors.RPCError):
            details.setdefault("rpc_code", error.code or type(error).code)
        return cls(type(error).__name__, **details)


class SlowConnection(Exception):
    """File parts came too slowly over one connection, which was opened anew. Diagnostics only."""


class RequestDropped(ConnectionError):
    """Telethon cancelled a request in flight: its connection was closed without an error, as a
    reconnect after another request's timeout closes it, borrowed connections to other DCs too.
    A connection failure, not the person's Cancel."""


def request_dropped():
    """A CancelledError nobody asked this task for: the request's own future was cancelled."""
    task = asyncio.current_task()
    return task is not None and not task.cancelling()


class ConnectionState:
    """How file parts fare over one connection: see SLOW_PART_SECONDS."""
    def __init__(self):
        self.opened = 0  # Times it was opened anew for slow parts.
        self.slow = 0  # Slow parts in a row since then.
        self.reopened_at = -math.inf


@asynccontextmanager
async def request_timeout(seconds):
    async with asyncio.timeout(seconds) as watchdog:
        token = _timeouts.set((*_timeouts.get(), (asyncio.current_task(), watchdog)))
        try:
            yield watchdog
        finally:
            _timeouts.reset(token)


@contextmanager
def suspend_timeouts():
    # Server-mandated waits and the request queue are not network inactivity.
    # Preserve only this task's budgets; child tasks inherit ContextVars.
    loop = asyncio.get_running_loop()
    paused = []
    for owner, watchdog in _timeouts.get():
        deadline = watchdog.when()
        if owner is asyncio.current_task() and deadline is not None and not watchdog.expired():
            paused.append((watchdog, max(0, deadline - loop.time())))
            watchdog.reschedule(None)
    try:
        yield
    finally:
        for watchdog, remaining in paused:
            watchdog.reschedule(loop.time() + remaining)


# Telegram's layer 229 lists a community among the chats: dialogCommunity, with no peer and no latest
# message. Telethon 1.45 reads both of every dialog, and the whole chat list failed with AttributeError.
# A community is no chat to export: an entry naming no one the page brought takes its place, which
# Telethon skips, and the page still counts every dialog it holds.
UNLISTED = types.PeerUser(0)


def listable(result):
    """A page of the chat list Telethon can read: its communities set aside."""
    if isinstance(result, (types.messages.Dialogs, types.messages.DialogsSlice, types.messages.PeerDialogs)):
        result.dialogs = [types.Dialog(UNLISTED, 0, 0, 0, 0, 0, 0, 0, dialog.notify_settings)
                          if isinstance(dialog, types.DialogCommunity) else dialog for dialog in result.dialogs]
    return result


class DcConnection(ConnectionTcpFull):
    """A connection that gives up on an address that does not answer, so the next one gets its turn.

    Telethon connects to another DC without a time limit of its own and tries the same address several
    times in a row: each try then lasts as long as Windows waits for an answer. Here such a try ends
    after DC_CONNECT_SECONDS, and the tries that follow at once fail without waiting. The session's own
    connection comes with Telethon's time limit and is left to it.
    """
    _silent_until = -math.inf

    async def connect(self, timeout=None, ssl=None):
        if timeout is not None:
            return await super().connect(timeout=timeout, ssl=ssl)
        loop = asyncio.get_running_loop()
        if loop.time() < self._silent_until:
            raise ConnectionError("The address did not answer")
        try:
            await super().connect(timeout=DC_CONNECT_SECONDS, ssl=ssl)
        except (OSError, asyncio.TimeoutError):
            self._silent_until = loop.time() + DC_CONNECT_SECONDS
            raise


class ExportTelegramClient(TelegramClient):
    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None, takeout=True):
        return listable(await self._paced_call(sender, request, ordered, flood_sleep_threshold, takeout))

    async def _paced_call(self, sender, request, ordered=False, flood_sleep_threshold=None, takeout=True):
        call = super()._call
        gateway = _active_gateway.get()
        takeout_id = gateway.takeout_for(request) if gateway is not None and takeout else None
        sent = request
        if takeout_id is not None:
            # As Telethon's own takeout client does: the wrapper does not resolve what it carries.
            await request.resolve(self, utils)
            sent = functions.InvokeWithTakeoutRequest(takeout_id, request)
        def operation():
            return call(sender, sent, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)
        if gateway is None:
            return await operation()
        # Includes SDK-internal getFile chunks, avatars and CDN requests. Retrying
        # here retains the iterator offset and already-written file contents.
        if isinstance(request, (functions.upload.GetFileRequest, functions.upload.GetCdnFileRequest)):
            state = _file_download.get()
            if state is not None and state["owner"] is asyncio.current_task():
                # Only borrowed senders carry dc_id; the main sender serves the session's DC.
                dc_id = getattr(sender, "dc_id", None)
                state["dc_id"] = dc_id if isinstance(dc_id, int) else self.session.dc_id
            # Each file RPC has its own deadline. Outer file timers must not
            # cancel the iterator while this same chunk is being recovered.
            with suspend_timeouts():
                return await gateway._rpc_call(request, lambda: self._file_part(sender, operation, gateway))
        try:
            result = await gateway._rpc_call(request, operation, "request:" if sent is request else "takeout:")
        except (errors.BadRequestError, errors.ForbiddenError) as exc:
            if sent is request:
                raise
            # Refused inside the takeout session, e.g. when another export opened one since. The
            # same request outside it tells whether the session is to blame.
            result = await self._call(sender, request, ordered, flood_sleep_threshold, takeout=False)
            gateway.takeout_refused(exc)
        page = _page_users.get()
        if page is not None:
            page.update((user.id, user) for user in getattr(result, "users", None) or ()
                        if isinstance(user, types.User))
        return result

    def _connection_state(self, sender):
        if "_connections" not in vars(self):
            # Keyed by the sender itself: a borrowed one lives as long as the client keeps it.
            self._connections = weakref.WeakKeyDictionary()
        return self._connections.setdefault(sender, ConnectionState())

    async def _file_part(self, sender, operation, gateway):
        """One file part, timed against the connection it travels over."""
        loop = asyncio.get_running_loop()
        opened, started = self._connection_state(sender).opened, loop.time()
        try:
            result = await operation()
        except errors.RPCError:
            raise  # Telegram answered, if not with the part: nothing about the connection's speed.
        except BaseException:
            # No answer at all, e.g. the request deadline: slow if it waited that long.
            self._part_timed(sender, opened, loop.time() - started, False, gateway)
            raise
        self._part_timed(sender, opened, loop.time() - started, True, gateway)
        return result

    def _part_timed(self, sender, opened, seconds, answered, gateway):
        """Count a slow part; SLOW_PARTS of them in a row open the connection anew."""
        state = self._connection_state(sender)
        if state.opened != opened:
            return  # Asked before the connection was opened anew: its time is the old one's.
        if seconds < SLOW_PART_SECONDS:
            if answered:
                state.slow = 0
            return
        state.slow += 1
        now = asyncio.get_running_loop().time()
        if state.slow < SLOW_PARTS or now - state.reopened_at < REOPEN_SECONDS or not sender.is_connected():
            return
        state.slow, state.opened, state.reopened_at = 0, state.opened + 1, now
        dc_id = getattr(sender, "dc_id", None)
        gateway.retries.failed(SlowConnection(), "request:GetFileRequest", details=dict(
            dc_id=dc_id if isinstance(dc_id, int) else self.session.dc_id, part_seconds=round(seconds),
            error_origin="local"))
        # What Telethon does when a connection drops: a new one, the requests in flight sent again
        # over it. An empty context keeps this download's deadlines out of Telethon's tasks.
        Context().run(sender._start_reconnect, ConnectionError("file parts too slow over this connection"))

    async def _download_file(self, *args, part_size_kb=None, **kwargs):
        return await super()._download_file(*args, part_size_kb=part_size_kb or PART_SIZE_KB, **kwargs)

    async def _get_cdn_client(self, cdn_redirect):
        client = await super()._get_cdn_client(cdn_redirect)
        client.flood_sleep_threshold = 0
        client._request_retries = 0
        client._raise_last_call_error = True
        return client

    async def _borrow_exported_sender(self, dc_id):
        # Telethon's cached _ExportState (1.42.0 to 1.45.0) can still say "connected" after
        # MTProtoSender exhausted auto-reconnect and disconnected itself. Repair
        # that flag under the same lock the pinned SDK uses for its sender pool.
        async with self._borrow_sender_lock:
            state, sender = self._borrowed_senders.get(dc_id, (None, None))
            if sender is not None and not sender.is_connected():
                state._connected = False
        try:
            return await super()._borrow_exported_sender(dc_id)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            # The next attempt goes to the next address Telegram lists for this DC (_get_dc).
            turns = self.__dict__.setdefault("_dc_turns", {})
            turns[dc_id] = turns.get(dc_id, 0) + 1
            raise

    async def _get_dc(self, dc_id, cdn=False):
        # Telethon takes the first address Telegram lists for a DC, every time. Some networks reach only
        # some of them, and an export would wait for good on one that never answers. After a connection
        # to a DC fails, the next of its addresses is taken, round and round.
        chosen = await super()._get_dc(dc_id, cdn)
        if cdn:
            return chosen
        options, seen = [], set()
        for option in type(self)._config.dc_options:
            if (option.id == dc_id and bool(option.ipv6) == bool(chosen.ipv6) and not option.cdn
                    and (option.ip_address, option.port) not in seen):
                seen.add((option.ip_address, option.port))
                options.append(option)
        turn = self.__dict__.get("_dc_turns", {}).get(dc_id, 0)
        return options[turn % len(options)] if options else chosen


class Telegram:
    def __init__(self, api_id: int, api_hash: str, session: str = ""):
        validate_credentials(api_id, api_hash)
        self._client = ExportTelegramClient(
            StringSession(session), api_id, api_hash, device_model="SaveGram Exporter",
            system_version=f"Windows {platform.release()}", app_version=__version__,
            connection=DcConnection,
            request_retries=0, connection_retries=2, flood_sleep_threshold=0, raise_last_call_error=True,
            # QR login needs updates; no event handlers perform account actions.
            receive_updates=True,
        )
        self.downloaded_bytes, self.files_in_flight = 0, 0  # Also set by _runtime, for an instance made without __init__.

    def _runtime(self):
        if not hasattr(self, "retries"):
            self.retries = RetryState()
        if not hasattr(self, "downloaded_bytes"):
            # The export window's download speed: the bytes of file parts received so far, and how
            # many files are on their way now.
            self.downloaded_bytes, self.files_in_flight = 0, 0
        if not hasattr(self, "_connect_lock"):
            self._connect_lock = asyncio.Lock()
            self._connection_generation = 0
            self._needs_reconnect = False
            self._next_request = {}
            self._flood_until = 0
            self._flood_category = ""
            # Seconds between requests of each kind. Profiles known only by ID are resolved in batches,
            # paced well apart from the start.
            self._intervals = {"history": HISTORY_INTERVAL, "takeout": TAKEOUT_HISTORY_INTERVAL, "details": 0.35,
                               "reactions": 0.1, "rpc": 0.1, "download": FILE_INTERVAL, "profiles": 2.0}
            # The pace each kind returns to once a FloodWait is over.
            self._bases = dict(self._intervals)
            self._flood_streaks = {}
            # When the cooldown of each kind's current FloodWait ends, and when its pace last changed.
            self._episodes, self._eased_at = {}, {}
            # This export may read history through a takeout session; the one it opened; until when
            # Telegram asked this login to wait before one; when to ask next; whether one was opened
            # after such a wait, i.e. the person allowed it.
            self._takeout_wanted, self._takeout_id, self._takeout_after = False, None, -math.inf
            self._takeout_next, self._takeout_allowed = -math.inf, False
            # Until when Telegram's FloodWait on an ask holds the next one: asked before an export too.
            self._takeout_hold = -math.inf
            # Whether the session gave a message of this export's chat that someone else wrote, and why
            # the chat is read without it: see _try_takeout.
            self._takeout_proven, self._takeout_unfit = False, None

    def rate_limited(self):
        """True while a server cooldown is still running for every export request."""
        self._runtime()
        return self._flood_until > asyncio.get_running_loop().time()

    def _flood(self, exc, kind, details=None):
        self.retries.failed(exc, kind, details=details)
        self._flood_category = type(exc).__name__
        now = asyncio.get_running_loop().time()
        # Requests already out when Telegram asks to wait are refused together, one after
        # another: one episode, which slows its kind once however many were out.
        if now >= self._episodes.get(kind, 0):
            self._flood_streaks[kind] = self._flood_streaks.get(kind, 0) + 1
            self._intervals[kind] = min(30.0, max(0.5, self._intervals[kind] * 2))
        # Repeated immediate rejections need more recovery time than the same
        # minimum wait over and over. Never shorten the server's deadline.
        margin = min(300, 5 * 2 ** min(self._flood_streaks.get(kind, 1) - 1, 6))
        self._flood_until = max(self._flood_until, now + exc.seconds + margin)
        self._episodes[kind] = self._eased_at[kind] = self._flood_until  # Recovery counts from here.

    def _succeeded(self, kind):
        """A success ends a kind's streak; a kind slowed by a FloodWait speeds up a step for
        every RECOVER_SECONDS since the cooldown without another one.

        By the clock, not by a count of successes, which would take that many turns of the
        slowed pace.
        """
        self._flood_streaks[kind] = 0
        now = asyncio.get_running_loop().time()
        base = self._bases.get(kind, 0.1)
        if self._intervals.get(kind, base) > base and now - self._eased_at.get(kind, 0) >= RECOVER_SECONDS:
            self._intervals[kind] = max(base, self._intervals[kind] / 2)
            self._eased_at[kind] = now

    async def _rpc_call(self, request, operation, prefix="request:"):
        """One request, paced and retried by its kind: `prefix` and its SDK class name, "takeout:"
        for one sent through a takeout session."""
        self._runtime()
        requests = request if isinstance(request, (list, tuple)) else [request]
        # Only SDK class names, never request arguments, enter diagnostics.
        kinds = sorted({prefix + type(item).__name__ for item in requests})
        for kind in kinds:
            default = (TAKEOUT_HISTORY_INTERVAL if kind == "takeout:GetHistoryRequest" else
                       HISTORY_INTERVAL if kind.endswith("GetHistoryRequest") else
                       FILE_INTERVAL if kind.endswith(("GetFileRequest", "GetCdnFileRequest")) else 0.1)
            self._intervals.setdefault(kind, default)
            self._bases.setdefault(kind, default)
        failures = 0
        file_request = isinstance(request, (functions.upload.GetFileRequest, functions.upload.GetCdnFileRequest))
        while True:
            for kind in kinds:
                await self._before_read(kind)
            try:
                async with request_timeout(FILE_REQUEST_SECONDS if file_request else None):
                    result = await operation()
            except errors.TakeoutInitDelayError:
                raise  # Its seconds are a day to wait for a takeout session, not a cooldown: _open_takeout.
            except errors.FloodError as exc:
                if not hasattr(exc, "seconds"):
                    raise
                kind = prefix + type(exc.request).__name__
                if kind not in kinds:
                    kind = kinds[0]
                unavailable = file_request and self._file_rejected(request, exc)
                # Honor the cooldown for every later request, even if this file is given up.
                self._flood(exc, kind, self._request_details(request, exc))
                if unavailable:
                    raise unavailable
            except (errors.ServerError, errors.TimedOutError, TimeoutError) as exc:
                failures += 1
                phase = "server_wait" if isinstance(exc, errors.RPCError) else "network_wait"
                # Server TIMEOUT is a response, not proof of a broken transport.
                # A local deadline (no response at all) is never blamed on the file.
                if file_request and isinstance(exc, errors.RPCError):
                    if unavailable := self._file_rejected(request, exc):
                        self.retries.failed(exc, kinds[0], details=self._request_details(request, exc))
                        raise unavailable
                with suspend_timeouts():
                    await self.retries.pause(exc, failures, phase=phase, operation=kinds[0],
                                             details=self._request_details(request, exc))
            except asyncio.CancelledError:
                if not request_dropped():
                    raise
                # Sent again over the new connection; a file keeps the parts it already has.
                failures += 1
                with suspend_timeouts():
                    await self.retries.pause(RequestDropped(), failures, phase="network_wait", operation=kinds[0])
            else:
                if file_request:
                    self._file_progressed()
                    self.downloaded_bytes += len(getattr(result, "bytes", b"") or b"")
                for kind in kinds:
                    self._succeeded(kind)
                return result

    @staticmethod
    def _request_details(request, error):
        details = dict(location_type=type(getattr(request, "location", None)).__name__,
                       offset=getattr(request, "offset", 0),
                       error_origin="telegram" if isinstance(error, errors.RPCError) else "local")
        if isinstance(error, errors.RPCError):
            details["rpc_code"] = error.code
        state = _file_download.get()
        if state is not None and state["owner"] is asyncio.current_task():
            details.update(dc_id=state["dc_id"], file_rejections=state["rejections"])
        return details

    @staticmethod
    def _file_rejected(request, error):
        """Count a server rejection of this file part; return FileUnavailable once it is given up."""
        state = _file_download.get()
        if state is None or state["owner"] is not asyncio.current_task():
            return None  # Not an export file download: keep the unlimited retry policy.
        offset = getattr(request, "offset", 0)
        if offset != state["offset"]:
            state["offset"], state["rejections"] = offset, 0
        state["rejections"] += 1
        if state["rejections"] < FILE_UNAVAILABLE_AFTER:
            return None
        # The connection works (Telegram keeps answering), but this part does not come.
        return FileUnavailable.from_error(error, location_type=type(getattr(request, "location", None)).__name__,
                                          offset=offset, dc_id=state["dc_id"])

    @staticmethod
    def _file_progressed():
        state = _file_download.get()
        if state is not None and state["owner"] is asyncio.current_task():
            state["rejections"] = 0

    async def _before_read(self, kind):
        self._runtime()
        with suspend_timeouts():
            await self._wait_for_read(kind)

    async def _wait_for_read(self, kind):
        # Every kind keeps its own pace, and kinds wait side by side: a history page never
        # queues behind a reaction list's pause. A server cooldown still holds every request.
        loop = asyncio.get_running_loop()
        token = object()
        try:
            while True:
                while self._flood_until > loop.time():
                    seconds = max(1, math.ceil(self._flood_until - loop.time()))
                    self.retries.set_wait(token, "flood_wait", seconds, self._flood_category)
                    await asyncio.sleep(1)
                # Take this kind's next turn before sleeping, so requests of one kind stay
                # spaced however many of them wait at once.
                turn = max(loop.time(), self._next_request.get(kind, 0))
                self._next_request[kind] = turn + self._intervals[kind]
                if turn > loop.time():
                    await asyncio.sleep(turn - loop.time())
                # Another in-flight request may have started a cooldown meanwhile.
                if self._flood_until <= loop.time():
                    return
        finally:
            if token in self.retries.waiting:
                self.retries.clear_wait(token)

    async def _ensure_connected(self):
        if self._client.is_connected() and not self._needs_reconnect:
            return
        async with self._connect_lock:
            if self._client.is_connected() and not self._needs_reconnect:
                return
            # A single recovery serves all concurrent readers. Errors from the old
            # connection must not tear down the newly restored connection again.
            self._connection_generation += 1
            self._needs_reconnect = True
            try:
                if self._client.is_connected():
                    await asyncio.wait_for(self._client.disconnect(), 5)
                await asyncio.wait_for(self._client.connect(), 30)
            except BaseException:
                # A failed handshake can leave the transport marked connected.
                try:
                    await asyncio.wait_for(self._client.disconnect(), 5)
                except Exception:
                    pass
                raise
            self._needs_reconnect = False

    async def _read(self, operation, timeout=30, kind="rpc"):
        self._runtime()
        failures = 0
        while True:
            await self._before_read(kind)
            generation = self._connection_generation
            try:
                await self._ensure_connected()
                generation = self._connection_generation
                token = _active_gateway.set(self)
                try:
                    async with request_timeout(timeout):
                        result = await operation()
                finally:
                    _active_gateway.reset(token)
                self._succeeded(kind)
                return result
            except errors.TakeoutInitDelayError:
                raise
            except errors.FloodError as exc:
                if not hasattr(exc, "seconds"):
                    raise
                # Share the server's cooldown across every export request.
                self._flood(exc, kind)
            except (OSError, asyncio.TimeoutError, errors.ServerError, errors.TimedOutError) as exc:
                if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EACCES, errno.EIO, errno.EROFS}:
                    raise  # Retry the export stage from its checkpoint, not the connection.
                failures += 1
                if isinstance(exc, OSError) and kind != "download" and generation == self._connection_generation:
                    self._needs_reconnect = True
                phase = "server_wait" if isinstance(exc, errors.RPCError) else "network_wait"
                await self.retries.pause(exc, failures, phase=phase, operation=kind)
            except asyncio.CancelledError:
                if not request_dropped():
                    raise
                failures += 1
                await self.retries.pause(RequestDropped(), failures, phase="network_wait", operation=kind)

    async def connect(self):
        try:
            return await self._read(self._client.is_user_authorized)
        except APP_REFUSALS:
            raise AppRejected() from None

    async def identity(self):
        return await self._read(self._client.get_me)

    async def login_qr(self, show_qr, ask_password):
        try:
            return await self._login_qr(show_qr, ask_password)
        except APP_REFUSALS:
            raise AppRejected() from None

    async def _login_qr(self, show_qr, ask_password):
        while True:
            try:
                qr = await asyncio.wait_for(self._client.qr_login(), 30)
                # The phone took the last code as it expired, while this one was being made: Telegram
                # answers with the login itself, which has no code to show.
                accepted = getattr(qr, "_resp", None)
                if isinstance(accepted, (types.auth.LoginTokenSuccess, types.auth.LoginTokenMigrateTo)):
                    return await self._qr_accepted(accepted)
                show_qr(qr.url)
                try:
                    return await qr.wait(timeout=max(1, (qr.expires.replace(tzinfo=timezone.utc)
                                                          - datetime.now(timezone.utc)).total_seconds()))
                except asyncio.TimeoutError:
                    continue
            except errors.SessionPasswordNeededError:
                show_qr(None)
                while True:
                    password = await ask_password()
                    try:
                        return await asyncio.wait_for(self._client.sign_in(password=password), 30)
                    except errors.PasswordHashInvalidError:
                        continue
                    finally:
                        password = None

    async def _qr_accepted(self, answer):
        """Finish a QR login Telegram has already accepted, as Telethon's QRLogin.wait() does."""
        if isinstance(answer, types.auth.LoginTokenMigrateTo):
            await self._client._switch_dc(answer.dc_id)
            answer = await self._client(functions.auth.ImportLoginTokenRequest(answer.token))
        if not isinstance(answer, types.auth.LoginTokenSuccess):
            raise TypeError("Unexpected login token response")
        user = answer.authorization.user
        await self._client._on_login(user)
        return user

    def session_string(self):
        return self._client.session.save()

    async def disconnect(self):
        await asyncio.wait_for(self._client.disconnect(), 15)

    async def logout(self):
        return bool(await asyncio.wait_for(self._client.log_out(), 20))

    async def dialogs(self):
        """Every chat as (name, peer, details): its kind, username and ID, for a label a person can read;
        the key chat_photo() knows it by, whether it has a photo, and whether it forbids copying.

        A deleted account has no name left, and Saved Messages bears the account's own. Saved Messages
        is the account as a user: Telethon's InputPeerSelf has no ID, and a checkpoint is named by it.
        """
        result, self._chat_entities = [], {}
        iterator = self._client.iter_dialogs()
        while True:
            try:
                item = await asyncio.wait_for(anext(iterator), 30)
            except StopAsyncIteration:
                return result
            entity = item.entity
            kind = ("self" if getattr(entity, "is_self", False) else "deleted" if getattr(entity, "deleted", False)
                    else "user" if isinstance(entity, types.User)
                    else "channel" if getattr(entity, "broadcast", False) else "group")
            peer = utils.get_input_peer(entity, allow_self=False) if kind == "self" else item.input_entity
            key = peer_id(peer)
            # Saved Messages is drawn as Telegram's bookmark, never as the account's own photo.
            photo = kind != "self" and isinstance(getattr(entity, "photo", None),
                                                  (types.UserProfilePhoto, types.ChatPhoto))
            if photo:
                self._chat_entities[key] = entity
            result.append((item.name, peer, dict(kind=kind, username=getattr(entity, "username", None) or "",
                                                 id=entity.id, key=key, photo=photo,
                                                 protected=bool(getattr(entity, "noforwards", False)))))

    async def chat_photo(self, key):
        """A chat's small photo for the chat list, as bytes, or None: for the window only, never retried.

        A FloodWait is raised, for the list to wait; any other failure leaves the chat its initials.
        """
        entity = getattr(self, "_chat_entities", {}).get(key)
        if entity is None:
            return None
        try:
            return await asyncio.wait_for(
                self._client.download_profile_photo(entity, file=bytes, download_big=False), CHAT_PHOTO_SECONDS)
        except errors.FloodError:
            raise
        except (errors.RPCError, OSError, asyncio.TimeoutError, TypeError, ValueError):
            return None

    async def profile_photo(self, entity):
        """Someone's small profile photo as bytes, or None: for the window only, asked once, never retried."""
        try:
            return await asyncio.wait_for(self._client.download_profile_photo(entity, file=bytes, download_big=False), 30)
        except (errors.RPCError, OSError, asyncio.TimeoutError, TypeError, ValueError):
            return None

    async def entity(self, peer):
        if isinstance(peer, types.PeerUser) and peer.user_id in getattr(self, "_known_users", {}):
            return self._known_users[peer.user_id]
        return await self._read(lambda: self._client.get_entity(peer))

    async def users_by_id(self, user_ids):
        """Resolve profiles known only by ID, in batches; IDs Telegram will not resolve are absent.

        This is the same users.getUsers lookup the SDK performs for one unknown peer at a time.
        """
        known = getattr(self, "_known_users", {})
        result = {user_id: known[user_id] for user_id in user_ids if user_id in known}
        missing = [types.InputUser(user_id, 0) for user_id in user_ids if user_id not in result]
        for start in range(0, len(missing), 200):
            await self._collect_users(missing[start:start + 200], result)
        return result

    async def users_from_messages(self, peer, pairs):
        """Whole profiles of people Telegram sent partial ("min"), asked for through a message
        they are in: [(user_id, message_id)]. A partial profile leaves the username out."""
        result = {}
        inputs = [types.InputUserFromMessage(peer, message_id, user_id) for user_id, message_id in pairs]
        for start in range(0, len(inputs), 200):
            await self._collect_users(inputs[start:start + 200], result)
        return result

    async def _collect_users(self, chunk, result):
        request = functions.users.GetUsersRequest(chunk)
        try:
            users = await self._read(lambda: self._client(request), kind="profiles")
        except errors.BadRequestError:
            # One unusable ID must not discard a whole batch of valid ones.
            if len(chunk) == 1:
                return
            middle = len(chunk) // 2
            await self._collect_users(chunk[:middle], result)
            await self._collect_users(chunk[middle:], result)
            return
        for user in users:
            if isinstance(user, types.User):  # UserEmpty means Telegram has no profile for us.
                result[user.id] = user

    async def full_info(self, entity):
        if isinstance(entity, types.Channel):
            request = functions.channels.GetFullChannelRequest(entity)
        elif isinstance(entity, types.Chat):
            request = functions.messages.GetFullChatRequest(entity.id)
        else:
            request = functions.users.GetFullUserRequest(entity)
        return await self._read(lambda: self._client(request))

    async def message_bounds(self, peer, start, end):
        # Telegram dates have second resolution; both user boundaries are inclusive.
        before_start = start.replace(microsecond=0) - timedelta(seconds=int(start.microsecond == 0))
        after_end = end.replace(microsecond=0) + timedelta(seconds=1)
        first = await self._read(lambda: self._client.get_messages(
            peer, limit=BOUND_MESSAGES, offset_date=before_start, reverse=True), kind="history")
        last = await self._read(lambda: self._client.get_messages(
            peer, limit=BOUND_MESSAGES, offset_date=after_end), kind="history")
        if not first or not last or not (start <= first[0].date <= end and start <= last[0].date <= end):
            return None
        return first[0].id, last[0].id

    def allow_takeout(self):
        """Let this export read history pages through a takeout session: TAKEOUT_HISTORY_INTERVAL.

        Telegram may first ask the person to allow one, in a message to their Telegram: the export
        reads at the usual pace meanwhile and asks again every TAKEOUT_ASK_SECONDS. Whether the session
        gives this export's chat whole is not known yet: _try_takeout.
        """
        self._runtime()
        self._takeout_wanted = True
        self._takeout_proven, self._takeout_unfit = False, None
        if not self.takeout_delayed:
            self._takeout_next = -math.inf  # At this export's first page, unless Telegram is waiting.

    @property
    def takeout_delayed(self):
        """Telegram asked this login to wait before a takeout session, and the wait is not over."""
        self._runtime()
        return self._takeout_after > asyncio.get_running_loop().time()

    @property
    def takeout_allowed(self):
        """History is read through a takeout session Telegram first waited for the person to allow."""
        self._runtime()
        return self._takeout_allowed and self._takeout_id is not None

    @property
    def takeout_active(self):
        """History pages go through a takeout session now: the fast mode, at TAKEOUT_HISTORY_INTERVAL."""
        self._runtime()
        return self._takeout_id is not None and self._takeout_proven and not self._takeout_unfit

    @property
    def takeout_unfit(self):
        """Why this export's chat is read at the usual speed whatever Telegram allows: "protected" (the
        chat forbids copying), "filtered" (the session gave only the account's own messages), or None."""
        self._runtime()
        return self._takeout_unfit

    def decline_takeout(self, reason):
        """Read this export's chat without a takeout session to its end, and ask Telegram for none."""
        self._runtime()
        self._takeout_unfit, self._takeout_wanted = reason, False

    async def ask_takeout(self):
        """Ask for a takeout session before an export, while the window says what Telegram's "Data export
        request" is: "open" (the fast mode is on), "delayed" (Telegram waits for "Allow" in that message)
        or "unavailable". Asked again by calling again, every TAKEOUT_WAIT_ASK_SECONDS; a FloodWait on the
        ask is waited out first. The export that follows uses the session it opened."""
        self._runtime()
        self._takeout_wanted = True
        if self._takeout_id is None and asyncio.get_running_loop().time() >= self._takeout_hold:
            await self._open_takeout()
        if self._takeout_id is not None:
            return "open"
        return "delayed" if self.takeout_delayed else "unavailable"

    def takeout_for(self, request):
        """The takeout session a request goes through, or None. History pages only, and only of a chat
        the session is known to give whole."""
        if not isinstance(request, functions.messages.GetHistoryRequest) or self._takeout_unfit:
            return None
        return self._takeout_id if self._takeout_proven or _takeout_trial.get() else None

    def takeout_refused(self, error):
        """A request was refused inside the takeout session and answered outside it: this export
        goes on without one. Not opened again, which would end another export's session."""
        self._takeout_wanted, self._takeout_id = False, None
        self.retries.failed(error, "takeout")

    async def _open_takeout(self):
        """Ask for a takeout session once; while Telegram waits for the person, again later.

        Not through _read: a FloodWait on this ask must not hold every other request, nor a
        failed ask a history page for longer than TAKEOUT_ASK_TIMEOUT.
        """
        loop = asyncio.get_running_loop()
        self._takeout_next = loop.time() + TAKEOUT_ASK_SECONDS
        request = functions.account.InitTakeoutSessionRequest(**TAKEOUT_SCOPE)
        try:
            takeout = await asyncio.wait_for(self._client(request), TAKEOUT_ASK_TIMEOUT)
        except errors.TakeoutInitDelayError as exc:
            # Telegram has sent the person a "Data export request" to allow the export meanwhile.
            if not self.takeout_delayed:
                self.retries.failed(exc, "takeout")  # Once, not at every ask.
            self._takeout_after = loop.time() + exc.seconds
        except errors.FloodError as exc:
            self._takeout_next = loop.time() + max(TAKEOUT_ASK_SECONDS, getattr(exc, "seconds", None) or 0)
            self._takeout_hold = loop.time() + (getattr(exc, "seconds", None) or 0)
            self.retries.failed(exc, "takeout")
        except errors.RPCError as exc:
            self._takeout_wanted = False
            self.retries.failed(exc, "takeout")
        except (OSError, asyncio.TimeoutError) as exc:
            self.retries.failed(exc, "takeout")  # The connection's: asked again at the next turn.
        except asyncio.CancelledError:
            if not request_dropped():
                raise
        else:
            self._takeout_allowed = self._takeout_allowed or self.takeout_delayed
            self._takeout_id, self._takeout_after = takeout.id, -math.inf

    async def end_takeout(self, success):
        """Finish this export's takeout session, if it has one. Never fails or holds up the export."""
        takeout, self._takeout_id, self._takeout_wanted = getattr(self, "_takeout_id", None), None, False
        if takeout is None:
            return
        try:
            await asyncio.wait_for(self._client(functions.InvokeWithTakeoutRequest(
                takeout, functions.account.FinishTakeoutSessionRequest(success=success))), 5)
        except (errors.RPCError, OSError, asyncio.TimeoutError):
            pass  # The next takeout session of this login ends this one anyway.

    async def messages(self, peer, offset_id=0, min_id=0):
        self._runtime()
        if (self._takeout_wanted and self._takeout_id is None
                and asyncio.get_running_loop().time() >= self._takeout_next):
            await self._open_takeout()
        users = {}
        token = _page_users.set(users)
        try:
            page = await self._read(lambda: self._client.get_messages(
                peer, limit=100, offset_id=offset_id, min_id=min_id),
                kind="takeout" if self.takeout_active else "history")
        finally:
            _page_users.reset(token)
        if self._takeout_id is not None and not self._takeout_proven and not self._takeout_unfit:
            await self._try_takeout(peer, page)
        if not hasattr(self, "_known_users"):
            self._known_users, self._partial_users = {}, {}
        # The page also carries the people of its messages' recent reactions and polls' latest voters.
        # Complete profiles stand in for a reaction list; a partial ("min") one lacks the username a
        # list gives, and is kept apart: for a latest voter it is all Telegram sends.
        for user in users.values():
            if not user.min:
                self._known_users[user.id] = user
            else:
                self._partial_users[user.id] = user
        for message in page:
            sender = getattr(message, "sender", None)
            if isinstance(sender, types.User):
                self._known_users[sender.id] = sender
        return page

    async def _try_takeout(self, peer, page):
        """Learn whether the takeout session gives this chat whole, from a page read without it.

        Of a chat that forbids copying Telegram gives through the session only the account's own
        messages, as if there were no others, and no error says so. The first message someone else
        wrote is therefore asked for through the session too: if it comes, the pages that follow go
        through it; if not, the whole chat is read as before. A page of the account's own messages
        tells nothing, and the next one is read without the session as well.
        """
        other = next((message for message in page if not message.out), None)
        if other is None:
            return
        token = _takeout_trial.set(True)
        try:
            same = await self._read(lambda: self._client.get_messages(peer, limit=1, offset_id=other.id + 1),
                                    kind="takeout")
        finally:
            _takeout_trial.reset(token)
        if self._takeout_id is None:
            return  # The session ended meanwhile and the message came outside it: takeout_refused.
        if same and same[0].id == other.id:
            self._takeout_proven = True
        else:
            self.decline_takeout("filtered")

    def known_user(self, user_id):
        """A complete profile a history page already brought, or None."""
        user = getattr(self, "_known_users", {}).get(user_id)
        return user if isinstance(user, types.User) and not user.min else None

    def partial_user(self, user_id):
        """A partial ("min") profile a history page brought, or None: a name without a username.

        A poll's latest voters may come only so: neither a bare ID nor a lookup through the
        poll's message (MSG_ID_INVALID) finds them whole."""
        user = getattr(self, "_partial_users", {}).get(user_id)
        return user if isinstance(user, types.User) else None

    async def topics(self, peer):
        topics, offset_date, offset_id, offset_topic = [], None, 0, 0
        while True:
            page = await self._read(lambda: self._client(functions.messages.GetForumTopicsRequest(
                peer, offset_date, offset_id, offset_topic, 100)))
            items = [topic for topic in page.topics if isinstance(topic, types.ForumTopic)]
            if not items:
                return topics
            topics.extend(items)
            if len(topics) >= page.count:
                return topics
            last = items[-1]
            if last.id == offset_topic:
                raise ValueError("Non-advancing topic page")
            # Telegram sorts by creation time only when explicitly flagged.
            # Otherwise the cursor uses the last message date, not topic.date.
            offset_date = last.date if page.order_by_create_date else next(
                (message.date for message in page.messages if message.id == last.top_message), None)
            if offset_date is None:
                raise ValueError("Missing topic pagination date")
            offset_id, offset_topic = last.top_message, last.id

    async def reactions(self, peer, message_id):
        offset = None
        while True:
            page = await self._read(lambda: self._client(functions.messages.GetMessageReactionsListRequest(
                peer, message_id, 100, offset=offset)), kind="reactions")
            yield page
            if not page.next_offset:
                return
            if offset == page.next_offset:
                raise ValueError("Non-advancing reaction page")
            offset = page.next_offset

    async def poll_votes(self, peer, message_id, option):
        offset = None
        while True:
            page = await self._read(lambda: self._client(functions.messages.GetPollVotesRequest(
                peer, message_id, 100, option=option, offset=offset)), kind="details")
            yield page
            if not page.next_offset:
                return
            if offset == page.next_offset:
                raise ValueError("Non-advancing poll page")
            offset = page.next_offset

    async def emoji_document(self, document_id):
        return (await self.emoji_documents([document_id])).get(document_id)

    async def emoji_documents(self, document_ids):
        result = {}
        ids = sorted(set(document_ids))
        for offset in range(0, len(ids), 100):
            documents = await self._read(lambda: self._client(
                functions.messages.GetCustomEmojiDocumentsRequest(ids[offset:offset + 100])))
            result.update((doc.id, doc) for doc in documents)
        return result

    async def dice_stickers(self, emoticon):
        """The stickers Telegram keeps for a dice emoji, in the order of their set: what its apps draw
        a rolled die, a dart, a ball or a slot machine with. The same for every account."""
        found = await self._read(lambda: self._client(functions.messages.GetStickerSetRequest(
            types.InputStickerSetDice(emoticon), hash=0)))
        return list(getattr(found, "documents", None) or [])

    async def download(self, media, handle, message=None):
        # A caller-opened handle prevents Telegram filenames controlling paths.
        if isinstance(media, types.Document) and (media.size or 0) > PART_SIZE_KB * 1024:
            return await self._file(lambda: self._parts_at_once(media, handle, message), timeout=None)
        async def attempt():
            handle.seek(0)
            handle.truncate()
            # Large files may legitimately take more than three minutes.
            # Time out only when no bytes arrive for that long.
            async with request_timeout(DOWNLOAD_IDLE_SECONDS) as watchdog:
                def progressed(current, total):
                    watchdog.reschedule(asyncio.get_running_loop().time() + DOWNLOAD_IDLE_SECONDS)
                # Keeping message context lets Telethon refresh expired document references.
                return await self._client.download_media(message if message is not None else media,
                                                        file=handle, progress_callback=progressed)
        return await self._file(attempt, timeout=None)

    async def _parts_at_once(self, document, handle, message):
        """A file of several parts, PARALLEL_PARTS of them in flight, each written where it belongs.

        Each strand asks for every PARALLEL_PARTS-th part and counts Telegram's refusals of
        its own part, so a part that never comes still ends the download as unavailable.
        No strand outlives another's failure.
        """
        handle.seek(0)
        handle.truncate()
        part = PART_SIZE_KB * 1024
        count = (document.size + part - 1) // part
        strands = min(PARALLEL_PARTS, count)
        # Keeping message context lets Telethon refresh expired document references.
        chat = getattr(message, "input_chat", None)
        msg_data = (chat, message.id) if chat is not None else None

        async def strand(first):
            _file_download.set(dict(owner=asyncio.current_task(), offset=None, rejections=0, dc_id=None))
            position = first * part
            async for chunk in self._client._iter_download(
                    document, offset=position, stride=strands * part, limit=(count - first + strands - 1) // strands,
                    request_size=part, chunk_size=part, msg_data=msg_data):
                handle.seek(position)
                handle.write(chunk)
                position += strands * part

        tasks = [asyncio.create_task(strand(first)) for first in range(strands)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return handle

    async def _file(self, attempt, timeout):
        self._runtime()
        state = dict(owner=asyncio.current_task(), offset=None, rejections=0, dc_id=None)
        token = _file_download.set(state)
        self.files_in_flight += 1
        try:
            return await self._read(attempt, timeout=timeout, kind="download")
        finally:
            self.files_in_flight -= 1
            _file_download.reset(token)

    async def avatar(self, entity, handle):
        # Only the big still profile image on the photo's own DC. Embedded
        # stripped/cached previews are never a substitute for the avatar.
        current = entity
        for refreshed in (False, True):
            async def attempt():
                handle.seek(0)
                handle.truncate()
                return await self._client.download_profile_photo(current, file=handle)
            try:
                result = await self._file(attempt, timeout=180)
                if result is not None:
                    return result
                # Telethon (1.42.0 to 1.45.0) returns None for a user's LOCATION_INVALID.
                stale = FileUnavailable("LocationInvalidError", rpc_code=400,
                                        location_type="InputPeerPhotoFileLocation")
            except AVATAR_REFRESH_ERRORS as exc:
                stale = FileUnavailable.from_error(exc, location_type="InputPeerPhotoFileLocation")
            if refreshed:
                raise stale
            # Only a stale photo ID or access hash is repaired by refreshing the
            # entity. Server TIMEOUT/FLOOD_WAIT never reach this point.
            try:
                current = await self._read(lambda: self._client.get_entity(current))
            except (ValueError, TypeError, errors.BadRequestError, errors.ForbiddenError) as exc:
                raise FileUnavailable.from_error(exc, stage="refresh") from None
            if not isinstance(getattr(current, "photo", None), (types.UserProfilePhoto, types.ChatPhoto)):
                return None  # The avatar was removed.

    async def messages_by_id(self, peer, message_ids):
        """Re-read messages by ID. The response carries their authors, unlike a bare user ID.

        Service messages count too: a join names its people, a new group photo is its file.
        """
        ids, result = list(message_ids), []
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            page = await self._read(lambda: self._client.get_messages(peer, ids=chunk), kind="history")
            result.extend(item for item in page if isinstance(item, (types.Message, types.MessageService)))
        return result

    async def message(self, peer, message_id):
        """Re-read one message, e.g. to retry its attachment with a fresh file reference."""
        page = await self.messages_by_id(peer, [message_id])
        return page[0] if page else None


def peer_id(peer):
    return utils.get_peer_id(peer)


def extension(media):
    suffix = utils.get_extension(media)
    return suffix if suffix and len(suffix) < 12 and suffix[1:].isalnum() else ".bin"
