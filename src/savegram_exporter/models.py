"""Values exchanged between the UI and the export job."""
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event

# What SaveGram's processing accepts as one archive's message data: its JSON bytes, JSON nodes
# and messages. These sit a margin below the site's own limits, which it derives from the memory
# its worker is given. They are defaults for the exporter's setting, not rules the exporter
# enforces.
SITE_JSON_LIMIT = 1024 * 1024**2
SITE_NODE_LIMIT = 60_000_000
SITE_MESSAGE_LIMIT = 450_000
# The most the site takes as one archive, messages and files together, and the most ZIP entries
# it reads. A chat larger than that becomes several archives rather than one the site would
# refuse after the whole upload; both sit a margin below the site's limits.
SITE_ARCHIVE_BYTES = 15_000_000_000
SITE_FILE_LIMIT = 500_000


class Cancelled(Exception):
    """A job stopped before publishing the result; a checkpoint may remain."""


class SignedOut(Exception):
    """Telegram no longer accepts this login: it was ended in Telegram or in another window.
    Retrying never helps; the checkpoint stays for after signing in again."""


class AppRejected(Exception):
    """Telegram refuses the application itself, whoever signs in: it does not know its API ID and API Hash
    or no longer accepts them. Nothing was signed in, and only other ones help."""


def check_cancel(cancel: Event) -> None:
    if cancel.is_set():
        raise Cancelled()


@dataclass(frozen=True)
class ExportOptions:
    peer: object
    output: Path
    start: datetime
    end: datetime
    media_mode: str = "all"
    media_limit: int = 1000 * 1024**2
    part_size: int | None = None
    anonymize_exporter: bool = False
    resume_from: Path | None = None
    # Largest export JSON one archive may contain; None publishes a single archive
    # whatever its size. This is a property of where the archive is going, not of
    # the collection, so it never takes part in the checkpoint fingerprint.
    archive_limit: int | None = SITE_JSON_LIMIT
    # What an anonymous export keeps of people when asked: their names, their photos.
    keep_names: bool = False
    keep_avatars: bool = False

    def validate(self):
        if not all(isinstance(value, bool) for value in (self.anonymize_exporter, self.keep_names, self.keep_avatars)):
            raise ValueError("Invalid anonymity option")
        if (self.keep_names or self.keep_avatars) and not self.anonymize_exporter:
            raise ValueError("Invalid anonymity option: names and photos are a choice of an anonymous export")
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start > self.end:
            raise ValueError("Invalid date range")
        if self.media_mode not in {"all", "no_message_files", "none"}:
            raise ValueError("Invalid media mode")
        if self.media_limit <= 0 or self.part_size is not None and self.part_size < 1024:
            raise ValueError("Invalid size limit")
        if self.archive_limit is not None and self.archive_limit < 1024:
            raise ValueError("Invalid archive limit")


@dataclass(frozen=True)
class ExportProgress:
    phase: str
    messages: int = 0
    skipped: int = 0
    first_id: int | None = None
    last_id: int | None = None
    current_id: int | None = None
    history_done: bool = False
    wait_seconds: int = 0
    checkpoint: Path | None = None
    retry_attempt: int = 0
    error_category: str = ""
    # What the person should know while the export runs, as i18n keys, e.g. "takeout_delayed".
    notes: tuple[str, ...] = ()
    # Bytes a second the files came at over the last seconds; None while no file is on its way.
    download_speed: float | None = None
    # How much of the export is done, 0 to 0.99, once a page is read: messages saved with their files
    # against the whole history's estimate (Collector.share). Without it, the history's IDs say.
    share: float | None = None
    # Every history page is in, and only files may still be coming.
    history_read: bool = False

    @property
    def percent(self):
        if self.history_done:
            return 100
        if self.share is not None:
            return max(0, min(99, int(100 * self.share)))
        if self.first_id is None or self.last_id is None:
            return None
        if self.current_id is None:
            return 0
        if self.first_id == self.last_id:
            return 100
        return max(0, min(100, int(100 * (self.last_id - self.current_id)
                                  / (self.last_id - self.first_id))))


@dataclass(frozen=True)
class ExportResult:
    directory: Path
    files: tuple[Path, ...]
    messages: int
    omissions: dict[str, int] = field(default_factory=dict)
    # FloodWaits by kind of request: {"request:GetFileRequest": {"count": 3, "seconds": 21}}.
    flood_waits: dict[str, dict[str, int]] = field(default_factory=dict)
    # What the person should know about the archive or the export, as i18n keys, e.g. "hidden_history".
    warnings: tuple[str, ...] = ()

    @property
    def partial(self):
        return any(count for key, count in self.omissions.items() if key != "size_limit")
