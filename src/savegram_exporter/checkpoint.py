"""Transactional export checkpoints. No Telegram session or account credentials.

The directory fingerprint binds the work to an account, peer and exact options.
Only projected export records and completed files are persisted, never TL objects.
Files Telegram did not provide are kept as `unavailable` records until a later attempt succeeds.
"""
import hashlib
import json
import msvcrt
from pathlib import Path
import sqlite3

from .archive import json_bytes
from .storage import no_links


def checkpoint_path(options, account_id):
    from .telegram import peer_id
    from datetime import timezone
    key = [1, account_id, peer_id(options.peer),
           options.start.astimezone(timezone.utc).isoformat(),
           options.end.astimezone(timezone.utc).isoformat(), options.media_mode,
           options.media_limit, options.part_size, options.anonymize_exporter]
    if options.keep_names or options.keep_avatars:
        # Only when chosen, so a checkpoint from before these choices still matches its export.
        key.append([options.keep_names, options.keep_avatars])
    return no_links(options.output / (".savegram-resume-" + hashlib.sha256(json_bytes(key)).hexdigest()[:32]))


class CheckpointInUse(ValueError):
    """Another exporter window is working on this checkpoint; retrying cannot help."""


class Checkpoint:
    def __init__(self, root: Path):
        self.root = no_links(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = no_links(root / "source")
        self.source.mkdir(exist_ok=True)
        self.lock = no_links(root / "checkpoint.lock").open("a+b")
        try:
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.lock.close()
            raise CheckpointInUse("This checkpoint is already in use") from None
        self.db = None
        try:
            path = no_links(root / "checkpoint.sqlite")
            for suffix in ("-journal", "-wal", "-shm"):
                no_links(Path(str(path) + suffix))
            self.db = sqlite3.connect(path)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))")
            self.db.commit()
            self.files = self.rows("file")
            for name, size in self.files.items():
                file = self.file_path(name)
                if not file.is_file() or file.stat().st_size != size:
                    raise ValueError("Checkpoint file is missing or changed")
        except BaseException:
            self.close()
            raise

    def file_path(self, name):
        path = no_links(self.source / name)
        if not path.is_relative_to(self.source) or not name.startswith("media/") or ".." in Path(name).parts:
            raise ValueError("Invalid checkpoint media path")
        return path

    def rows(self, kind):
        return {key: json.loads(value) for key, value in self.db.execute(
            "SELECT id,value FROM records WHERE kind=?", (kind,))}

    def ids(self, kind):
        return [key for (key,) in self.db.execute("SELECT id FROM records WHERE kind=?", (kind,))]

    def get(self, kind, key):
        row = self.db.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, str(key))).fetchone()
        return json.loads(row[0]) if row else None

    def values(self, kind, keys, batch=500):
        """The records of these keys in the order given, read a batch at a time."""
        keys = [str(key) for key in keys]
        for start in range(0, len(keys), batch):
            chunk = keys[start:start + batch]
            found = dict(self.db.execute(f"SELECT id,value FROM records WHERE kind=? AND id IN "
                                         f"({','.join('?' * len(chunk))})", (kind, *chunk)))
            for key in chunk:
                if key in found:
                    yield json.loads(found[key])

    def put(self, kind, key, value):
        self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)",
                        (kind, str(key), json_bytes(value).decode("utf-8")))

    def remove(self, kind, key):
        self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, str(key)))

    def file_done(self, name, size):
        with self.db:
            self.put("file", name, size)
        self.files[name] = size

    def close(self):
        if self.db is not None:
            self.db.close()
        if not self.lock.closed:
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock.close()


def read_options(path):
    path = no_links(path)
    if path.name != "checkpoint.sqlite" or not path.parent.name.startswith(".savegram-resume-"):
        raise ValueError("Choose an export checkpoint")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        row = db.execute("SELECT value FROM records WHERE kind='state' AND id='job'").fetchone()
        return json.loads(row[0])["options"]
    finally:
        db.close()
