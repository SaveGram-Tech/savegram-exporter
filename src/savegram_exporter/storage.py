"""The only persistent account stores: current-user Windows DPAPI, never SQLite."""
import json
import os
from pathlib import Path
import stat
import tempfile


def no_links(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Links and Windows reparse points are not allowed")
    return path


def protect(data: bytes) -> bytes:
    import win32crypt
    # CRYPTPROTECT_UI_FORBIDDEN, deliberately NOT CRYPTPROTECT_LOCAL_MACHINE.
    return win32crypt.CryptProtectData(data, "SaveGram Exporter", None, None, None, 1)


def unprotect(data: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptUnprotectData(data, None, None, None, 1)[1]


def validate_api_id(api_id):
    if not isinstance(api_id, int) or isinstance(api_id, bool) or not 0 < api_id < 2**31:
        raise ValueError("Invalid API ID")


def validate_credentials(api_id, api_hash):
    validate_api_id(api_id)
    if not isinstance(api_hash, str) or len(api_hash) != 32 or any(c not in "0123456789abcdef" for c in api_hash):
        raise ValueError("Invalid API Hash")


class Protected:
    """One value in a file of its own, encrypted for the current Windows user and replaced whole."""
    name = ""

    def __init__(self, directory: Path | None = None):
        self.directory = no_links(directory or Path(os.environ["LOCALAPPDATA"]) / "SaveGramExporter")
        self.path = self.directory / f"{self.name}.dpapi"

    def write(self, value: dict):
        encrypted = protect(json.dumps(value).encode())
        no_links(self.directory).mkdir(parents=True, exist_ok=True)
        no_links(self.path)
        fd, name = tempfile.mkstemp(dir=self.directory, prefix=f"{self.name}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(encrypted)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    def read(self, keys: set):
        no_links(self.path)
        if not self.path.exists():
            return None
        if self.path.stat().st_size > 16384:
            raise ValueError("Invalid container")
        value = json.loads(unprotect(self.path.read_bytes()))
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("Invalid container")
        return value

    def delete(self):
        no_links(self.path).unlink(missing_ok=True)


class Vault(Protected):
    name = "account"

    def save(self, api_id: int, session: str):
        validate_api_id(api_id)
        if not session:
            raise ValueError("Empty session")
        self.write(dict(api_id=api_id, session=session))

    def load(self):
        value = self.read({"api_id", "session"})
        if value is None:
            return None
        validate_api_id(value["api_id"])
        if not isinstance(value["session"], str) or not value["session"]:
            raise ValueError("Invalid session")
        return value


class AppKeys(Protected):
    """The API ID and API Hash a person entered for a Telegram app of their own, when this copy has none or
    Telegram stopped accepting its own. Kept apart from the account: signing out does not take them away."""
    name = "app"

    def save(self, api_id: int, api_hash: str):
        validate_credentials(api_id, api_hash)
        self.write(dict(api_id=api_id, api_hash=api_hash))

    def load(self):
        value = self.read({"api_id", "api_hash"})
        if value is None:
            return None
        validate_credentials(value["api_id"], value["api_hash"])
        return value["api_id"], value["api_hash"]
