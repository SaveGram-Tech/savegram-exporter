"""Streaming writer for current TGEX v1. No network or account dependencies.

Wire layout: TGEX/version/info/key/multipart/sizes/JSON location/SHA256/payload.
The 16-character key and repeating XOR are part of the format the site reads, not
confidentiality or authentication: the key is in the header.
"""
import hashlib
import json
from pathlib import Path
import secrets
import string
import struct
from threading import Event
import zipfile

from .models import SITE_FILE_LIMIT, SITE_JSON_LIMIT, check_cancel
from .storage import no_links

BLOCK = 64 * 1024
# The XOR pass runs a fixed Python cost per block, so larger blocks are faster. Output bytes
# are the same whatever the block.
XOR_BLOCK = 1024 * 1024
# Files that are compressed already: deflating them again gains next to nothing and takes most
# of the packing time. They are stored; anything else is deflated.
STORED = frozenset(".jpg .jpeg .png .gif .webp .heic .avif .mp4 .m4v .mov .mkv .webm .mp3 .m4a .ogg .oga "
                   ".opus .flac .tgs .zip .rar .7z .gz .bz2 .xz".split())
class ArchiveRejected(ValueError):
    """This collection cannot be packed within the website's limits; repeating it cannot help."""


def xor(data: bytes, key: bytes, offset=0) -> bytes:
    # Translate each key-position slice in C instead of a per-byte Python loop.
    result = bytearray(data)
    for index in range(len(key)):
        value = key[(offset + index) % len(key)]
        result[index::len(key)] = data[index::len(key)].translate(bytes(i ^ value for i in range(256)))
    return bytes(result)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _header(info, key, sizes, part, json_size):
    multi = len(sizes) > 1
    prefix = b"TGEX\x01" + struct.pack("<I", len(info)) + info + struct.pack("<H", len(key)) + key
    prefix += bytes([multi])
    if multi:
        prefix += struct.pack("<HH", len(sizes), part)
    prefix += struct.pack("<Q", sum(sizes))
    if multi:
        prefix += struct.pack("<Q", sizes[part - 1])
        if part == 1:
            prefix += struct.pack(f"<{len(sizes)}Q", *sizes)
    if part == 1:
        prefix += struct.pack("<QQ", len(prefix) + 16 + 64, json_size)
    return prefix


def _part_sizes(info, key, json_size, zip_size, limit):
    size = len(_header(info, key, [0], 1, json_size)) + 64 + json_size + zip_size
    if limit is None or size <= limit:
        return [size]
    count = 2
    while count <= 1024:
        first = len(_header(info, key, [0] * count, 1, json_size)) + 64
        rest = len(_header(info, key, [0] * count, 2, json_size)) + 64
        if first + json_size + 4 > limit or rest >= limit:
            raise ArchiveRejected("Part size must fit export JSON and its header")
        capacity = limit - rest
        remaining = zip_size - (limit - first - json_size)
        needed = 1 + (remaining + capacity - 1) // capacity
        if needed == count:
            return [limit] * (count - 1) + [rest + remaining - capacity * (count - 2)]
        count = max(count + 1, needed)
    raise ArchiveRejected("Too many archive parts")


def json_nodes(value) -> int:
    """Count JSON nodes the way the website's processor counts them."""
    pending, total = [value], 0
    while pending:
        item = pending.pop()
        total += 1
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return total


def write_archive(source: Path, manifest: list[str], info: dict, staging: Path, work: Path, cancel: Event,
                  part_size: int | None = None, json_limit: int | None = SITE_JSON_LIMIT,
                  sources: dict | None = None) -> tuple[Path, ...]:
    """Write to a private staging directory; the caller publishes the whole set.

    Only explicitly registered files are accepted. The working ZIP is outside
    source and staging. Memory for payload processing is bounded by XOR_BLOCK.
    `sources` names the file under `source` for an archive name that differs from it.
    """
    source, staging, work = (no_links(p) for p in (source, staging, work))
    if work == source or work.is_relative_to(source) or staging == source or staging.is_relative_to(source):
        raise ValueError("Archive scratch/output must be outside the input tree")
    sources = sources or {}
    members = []
    seen = set()
    for name in ["export.json", *manifest]:
        stored = sources.get(name, name) if name != "export.json" else name
        path = no_links(source / stored)
        if (not path.is_relative_to(source) or not path.is_file() or ".." in Path(name).parts
                or ".." in Path(stored).parts or Path(name).is_absolute()):
            raise ValueError("Invalid export member")
        relative = name if name in sources else path.relative_to(source).as_posix()
        if relative.casefold() in seen:
            raise ValueError("Duplicate export member")
        seen.add(relative.casefold())
        members.append((relative, path))
    if len(members) > SITE_FILE_LIMIT:
        raise ArchiveRejected("Too many export files")
    # Report a JSON the destination will not accept before anything is published.
    json_path = source / "export.json"
    json_size = json_path.stat().st_size
    if json_limit is not None and json_size > json_limit:
        raise ArchiveRejected("Export JSON exceeds the archive size limit; choose a shorter date range")
    zip_path = work / "payload.zip"
    # ZIP64 only changes how the directory is written, and the destination reads it;
    # a chat with more than 65535 media files has no other way to travel whole.
    with zipfile.ZipFile(zip_path, "x", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name, path in members[1:]:
            check_cancel(cancel)
            no_links(path)
            # Explicit streaming permits cancellation during a single large file.
            entry = zipfile.ZipInfo(name)
            entry.compress_type = zipfile.ZIP_STORED if Path(name).suffix.lower() in STORED else zipfile.ZIP_DEFLATED
            with path.open("rb") as inp, archive.open(entry, "w") as out:
                while data := inp.read(BLOCK):
                    check_cancel(cancel)
                    out.write(data)
    key = ''.join(secrets.choice(string.ascii_letters + string.digits + "!@#$%^&*") for _ in range(16)).encode()
    encoded_info = json_bytes(info)
    if len(encoded_info) > 1024**2:
        raise ArchiveRejected("Chat header too large")
    sizes = _part_sizes(encoded_info, key, json_size, zip_path.stat().st_size, part_size)
    paths = []
    zip_position = 0
    with zip_path.open("rb") as zip_input:
        for part, size in enumerate(sizes, 1):
            check_cancel(cancel)
            name = "export.tgex" + (f".{part:03d}" if len(sizes) > 1 else "")
            path = staging / name
            prefix = _header(encoded_info, key, sizes, part, json_size)
            digest = hashlib.sha256(prefix)
            with path.open("xb") as out:
                out.write(prefix)
                out.write(b"0" * 64)
                if part == 1:
                    position = 0
                    with json_path.open("rb") as inp:
                        while data := inp.read(XOR_BLOCK):
                            check_cancel(cancel)
                            encoded = xor(data, key, position)
                            position += len(data)
                            out.write(encoded)
                            digest.update(encoded)
                remaining = size - out.tell()
                while remaining:
                    check_cancel(cancel)
                    data = zip_input.read(min(XOR_BLOCK, remaining))
                    if not data:
                        raise ValueError("Truncated working ZIP")
                    if zip_position == 0:
                        encoded = data[:4] + xor(data[4:], key)
                    else:
                        encoded = xor(data, key, zip_position - 4)
                    zip_position += len(data)
                    remaining -= len(data)
                    out.write(encoded)
                    digest.update(encoded)
                out.seek(len(prefix))
                out.write(digest.hexdigest().encode())
            paths.append(path)
    check_cancel(cancel)
    return tuple(paths)
