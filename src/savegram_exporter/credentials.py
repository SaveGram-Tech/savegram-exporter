"""Application credentials: a private config file in source runs, bundled data in the EXE.

XOR masking only avoids literal credentials in the binary. It is not encryption:
anyone who has the application can recover its application credentials.
"""
import json
import os
from pathlib import Path
import sys

from .storage import validate_credentials


class ConfigurationError(ValueError):
    """Safe to report by category; never includes configuration contents."""


def parse_config(raw):
    try:
        data = json.loads(raw)
        api_id, api_hash = data["api_id"], data["api_hash"]
        validate_credentials(api_id, api_hash)
        return api_id, api_hash
    except (ValueError, TypeError, KeyError):
        raise ConfigurationError("Application configuration is missing or invalid") from None


def load_config(path):
    try:
        return parse_config(Path(path).read_bytes())
    except OSError:
        raise ConfigurationError("Application configuration is missing or invalid") from None


def masked_config(credentials):
    """Used by the build script; both returned byte strings are public in the EXE."""
    api_id, api_hash = credentials
    validate_credentials(api_id, api_hash)
    raw = json.dumps(dict(api_id=api_id, api_hash=api_hash)).encode("ascii")
    mask = os.urandom(len(raw))
    return bytes(a ^ b for a, b in zip(raw, mask, strict=True)), mask


def unmask_config(payload, mask):
    if not isinstance(payload, bytes) or not isinstance(mask, bytes) or len(payload) != len(mask):
        raise ConfigurationError("Invalid bundled configuration")
    return parse_config(bytes(a ^ b for a, b in zip(payload, mask, strict=True)))


def load_credentials():
    if getattr(sys, "frozen", False):
        try:
            from _savegram_build_config import PAYLOAD, MASK
        except ImportError:
            raise ConfigurationError("This build is not configured") from None
        return unmask_config(PAYLOAD, MASK)
    path = os.environ.get("SAVEGRAM_API_CONFIG") or Path.cwd() / ".private" / "telegram-api.json"
    return load_config(path)
