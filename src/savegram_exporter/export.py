"""One export job, with explicit read operations and a file manifest."""
import asyncio
from collections import Counter, deque
from contextlib import nullcontext, suppress
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import shutil
from threading import Event
import time
from types import SimpleNamespace
import uuid

from telethon import errors, types

from .archive import ArchiveRejected, json_bytes, json_nodes, write_archive
from .checkpoint import Checkpoint, CheckpointInUse, checkpoint_path
from .models import (SITE_ARCHIVE_BYTES, SITE_FILE_LIMIT, SITE_MESSAGE_LIMIT, SITE_NODE_LIMIT, Cancelled,
                     ExportOptions, ExportProgress, ExportResult, SignedOut, check_cancel)
from .privacy import MessageCheck, Privacy, VERSION, file_paths, ordered, user_name, validate_export
from .serialize import (attach_emoji_paths, custom_emoji_ids, entity_data, json_value,
                        referenced_chats, referenced_users, topic_for)
from .storage import no_links
from .retry import RetryState
from .telegram import FileUnavailable, RequestDropped, extension, peer_id, request_dropped

# A file Telegram does not serve never holds up history. Storage outages for a
# single file can last hours: retry between history pages at most this often,
# then a few rounds before publishing. What is still missing is recorded.
UNAVAILABLE_RETRY_SECONDS = 30 * 60
FINAL_RETRY_ROUNDS = 2
FINAL_RETRY_PAUSE_SECONDS = 60
# Telegram answering "no" to the same file again and again is an answer. Asking
# past this only lengthens the shared cooldown for every other request.
UNAVAILABLE_MAX_ATTEMPTS = 5
# What is needed to find the file again later; the error part is replaced per attempt.
SOURCE_KEYS = ("kind", "message_id", "document_id", "photo_id", "user_id", "chat_id", "via")
# Files download side by side, whatever message they belong to: most are avatars and
# stickers, where one paced round trip, not the bytes, is the cost.
DOWNLOAD_WORKERS = 8
# Files of one DC take at most this many of them: a DC that answers slowly must not hold every
# slot while the others answer at once.
DC_WORKERS = 6
# History reads this many messages past the oldest one still unfinished, so one long
# file does not hold up everything after it. The resume cursor never passes that one.
# A finished message is saved and let go, so the window costs only what is in flight.
MESSAGE_WINDOW = 5000
SAVE_SECONDS = 1.0
# What reading one message costs against downloading files, in bytes, for the percentage: a page
# of 100 takes about as long as a few megabytes of files.
MESSAGE_WORK = 64 * 1024
# The export window's download speed, said this often while files come, also when no message is
# done meanwhile: the average of the last seconds, once it has a second of them.
PROGRESS_TICK_SECONDS = 1
SPEED_WINDOW_SECONDS = 10
SPEED_MIN_SPAN = 1
# Where a profile seen there keeps its name without a photo: each photo is a request of its
# own, and these people show only by name. The photo follows once they write or vote.
NAME_ONLY = ("reactions", "action", "forward")
# Where a person is part of the message itself, which can then name them in a lookup: its
# author, a forward's origin, whom a service message names.
IN_MESSAGE = ("sender", "forward", "action")
# Service messages that name people besides their author, who come with the same page.
PEOPLE_ACTIONS = (types.MessageActionChatAddUser, types.MessageActionChatDeleteUser)
# Channels and groups: one that writes, is forwarded from or reacts is kept as a profile too.
CHATS = (types.Channel, types.ChannelForbidden, types.Chat, types.ChatForbidden)
# The profile tables a payload carries, and how a message names an entry of each.
PROFILES = (("users", referenced_users), ("chats", referenced_chats))
# Chats whose message IDs are each member's own sequence, not the chat's: a basic group and a
# private chat. The same message has another ID for another member.
OWN_NUMBERING = ("Chat", "ChatForbidden", "User")
MESSAGE_FILES = "media/messages/message_"


def profile_refs(entities, message):
    """The (table, key) of every saved profile a message names."""
    return {(table, str(key)) for table, refer in PROFILES for key in refer(message)
            if str(key) in entities.get(table, {})}


def forward_origin(message):
    """Where a forwarded message comes from, as its history page brought it."""
    forward = getattr(message, "forward", None)
    return getattr(forward, "sender", None) or getattr(forward, "chat", None)


def action_people(message):
    """The people a join, an addition or a removal names, as its history page brought them."""
    if not isinstance(getattr(message, "action", None), PEOPLE_ACTIONS):
        return []
    return [person for person in getattr(message, "action_entities", None) or () if isinstance(person, types.User)]


def mentioned_people(message):
    """IDs of the people a message's text links to by name; its history page brings their profiles."""
    return [entity.user_id for entity in getattr(message, "entities", None) or ()
            if isinstance(entity, types.MessageEntityMentionName)]


def rich_people(data):
    """IDs of the people a saved rich message names: by a link on a name or by a profile button."""
    return sorted(referenced_users(data.get("rich_message") or {}))


def media_people(data):
    """IDs of the people a saved message's media names besides its voters: who added a poll's answer, who
    ticked a checklist's item, whose contact it is."""
    media = data.get("media") or {}
    peers = [answer.get("added_by") for answer in (media.get("poll") or {}).get("answers") or ()]
    peers += [done.get("completed_by") for done in media.get("completions") or ()]
    found = [(peer or {}).get("user_id") for peer in peers] + [media.get("user_id")]
    return sorted({user_id for user_id in found if isinstance(user_id, int) and user_id > 0})


def media_files(media):
    """The photo or file a piece of media holds, as [(kind, object)]: a poll's picture, an answer's."""
    photo, document = getattr(media, "photo", None), getattr(media, "document", None)
    if isinstance(photo, types.Photo):
        return [("photo", photo)]
    return [("document", document)] if isinstance(document, types.Document) else []


def message_parts(message):
    """A message's files besides its own: a rich message's photos and files, a poll's picture and those of
    its answers and its explanation, the video of a live photo. As (kind, object), each once, in a fixed
    order: the n-th is saved as message_<id>-<n>."""
    parts = []
    rich = getattr(message, "rich_message", None)
    if rich is not None:
        parts += [("photo", photo) for photo in rich.photos or () if isinstance(photo, types.Photo)]
        parts += [("document", document) for document in rich.documents or () if isinstance(document, types.Document)]
    media = getattr(message, "media", None)
    if isinstance(media, types.MessageMediaPoll):
        parts += media_files(media.attached_media)
        for answer in media.poll.answers or ():
            parts += media_files(getattr(answer, "media", None))
        parts += media_files(getattr(media.results, "solution_media", None))
    elif isinstance(media, types.MessageMediaPhoto) and isinstance(media.video, types.Document):
        parts.append(("document", media.video))
    return list({(kind, item.id): (kind, item) for kind, item in parts}.values())


def part_records(data, kind=None, media_id=None):
    """The saved records of a photo or a file of a message, wherever the message names it (an answer may
    show the picture the poll does): those of `kind` and `media_id`, or all of them."""
    wanted = {"photo": {"Photo"}, "document": {"Document"}}.get(kind, {"Photo", "Document"})
    found, pending = [], [data]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if value.get("_") in wanted and (media_id is None or value.get("id") == media_id):
                found.append(value)
            pending.extend(item for item in value.values() if isinstance(item, (dict, list)))
        elif isinstance(value, list):
            pending.extend(value)
    return found


def part_source(record):
    """Which of a message's files an unavailable-file record is: (kind, ID)."""
    return ("photo", record["photo_id"]) if record.get("photo_id") is not None else ("document", record.get("document_id"))


def media_bytes(media):
    """What a document, or a photo's largest size, takes to download."""
    if isinstance(media, types.Document):
        return media.size or 0
    return max((max(size.sizes, default=0) if isinstance(size, types.PhotoSizeProgressive)
                else getattr(size, "size", 0) or 0 for size in getattr(media, "sizes", None) or ()), default=0)


def named_people(data):
    """IDs of the people a saved message would bring again when read anew: its author, whom
    its join or removal names, and whom it forwards."""
    action = data.get("action") or {}
    people = [(data.get("from_id") or {}).get("user_id"),
              ((data.get("fwd_from") or {}).get("from_id") or {}).get("user_id")]
    if action.get("_") in ("MessageActionChatAddUser", "MessageActionChatDeleteUser"):
        people.extend(action.get("users") or [action.get("user_id")])
    return [person for person in people if isinstance(person, int)]


def linked_people(data):
    """IDs of the people a saved message links to by name, in its text, its rich text or its media."""
    return [entity["user_id"] for entity in data.get("entities") or ()
            if isinstance(entity, dict) and entity.get("_") == "MessageEntityMentionName"
            and isinstance(entity.get("user_id"), int)] + rich_people(data) + media_people(data)


def recent_voters(message):
    """IDs of a poll's latest voters, which its results name by peer."""
    results = getattr(getattr(message, "media", None), "results", None)
    return [peer.user_id for peer in getattr(results, "recent_voters", None) or ()
            if isinstance(peer, types.PeerUser)]


def archive_files(meta):
    """Every file a payload's metadata refers to, including profile photos; messages add theirs."""
    names = file_paths(meta)
    names.update(profile["avatar_path"] for rows in meta["related_entities"].values()
                 for profile in rows.values() if profile and profile.get("avatar_path"))
    return names


def slice_meta(meta, ids, refs, first):
    """One archive's payload without its messages: the profiles and records those need."""
    records = [record for record in meta["unavailable_files"]
               if record.get("message_id") in ids or (first and "message_id" not in record)]
    return dict(meta, unavailable_files=records,
                related_entities={table: {key: rows[key] for key in sorted(k for t, k in refs if t == table)}
                                  for table, rows in meta["related_entities"].items()})


def json_head(meta):
    """What an export.json holds besides its messages, written before them."""
    return dict(meta, messages_last=True, messages=[])


def write_export_json(path, meta, messages, check=None):
    """Write export.json one message at a time, the messages last; return (messages, nodes).

    A collection is never held whole: a million messages would take gigabytes. "messages_last"
    tells a reader nothing follows the array, so reading the rest never walks through it.
    """
    head = json_head(meta)
    count, nodes = 0, json_nodes(head)
    with path.open("wb") as out:
        out.write(json_bytes(head)[:-2])  # Up to the array's opening bracket.
        for message in messages:
            if check:
                check(message)
            out.write(b"," if count else b"")
            out.write(json_bytes(message))
            count, nodes = count + 1, nodes + json_nodes(message)
        out.write(b"]}")
    return count, nodes


# What one file adds to an archive besides its bytes: its local and central ZIP
# records and name, with room for ZIP64 fields. Deliberately generous.
ENTRY_OVERHEAD = 256


def stored_bytes(names, sizes):
    """How much these files add to an archive, by the sizes they were saved with."""
    return sum(sizes.get(name, 0) + ENTRY_OVERHEAD for name in names)


def plan_parts(meta, source, header, limit, nodes_limit=SITE_NODE_LIMIT, messages_limit=SITE_MESSAGE_LIMIT,
               sizes=None, bytes_limit=SITE_ARCHIVE_BYTES, files_limit=SITE_FILE_LIMIT):
    """Split one collection into independent archives that fit the destination.

    Five budgets bind an archive: the bytes of its JSON, the number of JSON nodes the
    processor walks, how many messages it keeps a record for while building, the
    bytes of the whole archive -- JSON and files together, `sizes` giving each file's
    -- and its number of files. History order is preserved and each archive carries
    only the profiles and files its own messages refer to. Sizes are estimated, then
    measured: a group over any budget is divided again, because an estimate that runs
    over would produce an archive its destination refuses.

    `source(ids)` streams the messages in history order: all of them for None, else those
    IDs. Only sums are kept per archive, never the messages. Returns the metadata, header,
    files and message IDs of each archive.
    """
    sizes = sizes or {}
    entities = meta["related_entities"]
    empty = json_head(dict(meta, related_entities={table: {} for table in entities}, unavailable_files=[]))
    base = (len(json_bytes(empty)), json_nodes(empty))
    base_files = frozenset(archive_files(empty))
    profiles = {(table, key): value for table, rows in entities.items() for key, value in rows.items()}
    profile_cost = {ref: (len(json_bytes(value)) + len(ref[1]) + 4, json_nodes(value) + 1)
                    for ref, value in profiles.items()}
    # As archive_files counts a profile's files: whatever it carries, and its avatar.
    profile_files = {ref: file_paths(value) | ({value["avatar_path"]} if value.get("avatar_path") else set())
                     for ref, value in profiles.items() if value}

    def measured(messages):
        """Each message with what it weighs: JSON bytes, nodes, files and profiles."""
        for message in messages:
            yield (message["id"], len(json_bytes(message)), json_nodes(message), file_paths(message),
                   profile_refs(entities, message))

    def grouped(weighed):
        """Consecutive groups, each within every budget by estimate, with its own sums."""
        groups, ids, keys, files, own_files = [], [], set(), set(base_files), set()
        size, nodes, own_bytes, own_nodes = *base, 0, 0
        stored = stored_bytes(base_files, sizes)
        for ident, length, count, paths, needed in weighed:
            for attempt in (0, 1):
                fresh = needed - keys
                more_files = paths.union(*(profile_files.get(ref, ()) for ref in fresh)) - files
                more_size = length + 1 + sum(profile_cost[ref][0] for ref in fresh)
                more_nodes = count + sum(profile_cost[ref][1] for ref in fresh)
                more_stored = stored_bytes(more_files, sizes)
                if attempt or not ids or not (
                        size + more_size > limit or nodes + more_nodes > nodes_limit or len(ids) >= messages_limit
                        or size + more_size + stored + more_stored > bytes_limit
                        or len(files) + len(more_files) >= files_limit):
                    break
                groups.append((ids, keys, own_bytes, own_nodes, own_files))
                ids, keys, files, own_files = [], set(), set(base_files), set()
                size, nodes, own_bytes, own_nodes = *base, 0, 0
                stored = stored_bytes(base_files, sizes)
            ids.append(ident)
            keys |= needed
            files |= more_files
            own_files |= paths
            size, nodes, stored = size + more_size, nodes + more_nodes, stored + more_stored
            own_bytes, own_nodes = own_bytes + length, own_nodes + count
        groups.append((ids, keys, own_bytes, own_nodes, own_files))
        return groups

    parts, pending = [], grouped(measured(source(None)))
    while pending:
        ids, refs, own_bytes, own_nodes, own_files = pending.pop(0)
        part = slice_meta(meta, set(ids), refs, not parts)
        head = json_head(part)
        part_files = sorted(archive_files(part) | own_files)
        json_size = len(json_bytes(head)) + own_bytes + max(0, len(ids) - 1)
        over = (json_size > limit or json_nodes(head) + own_nodes > nodes_limit or len(ids) > messages_limit
                or json_size + stored_bytes(part_files, sizes) > bytes_limit
                or len(part_files) >= files_limit)
        if over and len(ids) > 1:
            # Weighed again half by half: rare, and it reads only this group's messages.
            weighed = list(measured(source(ids)))
            halves = [weighed[:len(weighed) // 2], weighed[len(weighed) // 2:]]
            pending[:0] = [(tuple(w[0] for w in half), set().union(*(w[4] for w in half)),
                            sum(w[1] for w in half), sum(w[2] for w in half), set().union(*(w[3] for w in half)))
                           for half in halves]
            continue
        parts.append((part, dict(header, messages_count=len(ids)), part_files, list(ids)))
    return parts


def payload_slices(payload, header, limit, **budgets):
    """plan_parts over a payload that holds its messages, each archive with its messages."""
    meta = {key: value for key, value in payload.items() if key != "messages"}
    by_id = {message["id"]: message for message in payload["messages"]}
    def source(ids):
        return iter(payload["messages"]) if ids is None else (by_id[ident] for ident in ids)
    return [(dict(part, messages=[by_id[ident] for ident in ids]), part_header, files)
            for part, part_header, files, ids in plan_parts(meta, source, header, limit, **budgets)]


def optional_error(exc):
    # Transport/access failures go to the retry supervisor, never into omissions.
    if isinstance(exc, (errors.UnauthorizedError, errors.FloodError, errors.ServerError, errors.TimedOutError, TimeoutError)):
        raise exc


class DownloadSpeed:
    """Bytes a second the files came at over the last SPEED_WINDOW_SECONDS, from Telegram's running
    total; None while no file is on its way, or too soon to say."""
    def __init__(self, now, total):
        self.samples = deque([(now, total)])

    def __call__(self, now, total, in_flight):
        self.samples.append((now, total))
        while len(self.samples) > 2 and now - self.samples[1][0] >= SPEED_WINDOW_SECONDS:
            self.samples.popleft()
        then, before = self.samples[0]
        if not in_flight or now - then < SPEED_MIN_SPAN:
            return None
        return max(0.0, (total - before) / (now - then))


class Collector:
    def __init__(self, telegram, options, source, progress, cancel, checkpoint):
        self.telegram, self.options, self.source = telegram, options, source
        self.progress, self.cancel = progress, cancel
        self.checkpoint = checkpoint
        self.state = checkpoint.rows("state").get("job", {})
        self.omissions = Counter(self.state.get("omissions", {}))
        self.manifest = list(checkpoint.files)
        self.emojis = {int(key): value for key, value in checkpoint.rows("emoji").items()}
        self.users = checkpoint.rows("user")
        # Channels and groups that write, are forwarded from or react; the chat itself is dialog_info.
        self.chats = checkpoint.rows("chat")
        # Only the IDs of saved messages stay in memory; the messages are read back from the
        # checkpoint when needed. Held whole, a large chat would take gigabytes.
        self.message_ids = {int(key) for key in checkpoint.ids("message")}
        self.pending_messages = []
        self.dirty_users, self.dirty_emojis, self.dirty_chats = set(), set(), set()
        self.unavailable = checkpoint.rows("unavailable")
        self.dirty_unavailable, self.resolved_unavailable = set(), set()
        # Profiles Telegram would not resolve, with the lookups already spent on them,
        # so a resume neither repeats a lookup nor skips one it never tried.
        self.unresolved = checkpoint.rows("unresolved_user")
        for record in self.unresolved.values():
            record.setdefault("tried_by_id", True)  # Written when that was the only lookup.
            record.setdefault("tried_by_message", False)
        self.dirty_unresolved, self.cleared_unresolved = set(), set()
        # People seen only in reactions keep their name without a photo; the photo follows
        # once they write or vote. Kept in the checkpoint so a resume can still add it.
        self.photo_skipped = set(checkpoint.rows("photo_skipped"))
        self.dirty_photo_skipped, self.cleared_photo_skipped = set(), set()
        # People Telegram sent only partially ("min", without the username), with a message they
        # are in: their whole profile is asked for through it once the history is read.
        self.partial = checkpoint.rows("partial_profile")
        self.dirty_partial, self.cleared_partial = set(), set()
        # Set by renumber(): message IDs and message files as the archive names them.
        self.numbers, self.olds, self.renamed = {}, {}, {}
        # Fresh download operations from this run; after a restart they are recovered from records.
        self.retry_operations = {}
        self.last_retry = time.monotonic()
        self.last_save = time.monotonic()
        self.cursor = {}  # How far history is complete, not yet saved.
        self.read_id = None  # The oldest message read so far, its files perhaps still downloading.
        # How much of the export is done, for the percentage: each message read costs MESSAGE_WORK and
        # the bytes of its file, and counts as done once saved with that file. Messages saved by an
        # earlier attempt count as read and done.
        self.work_read = self.work_done = len(self.message_ids) * MESSAGE_WORK
        self.work_pending = {}
        self.share_shown = 0.0
        self.history_read = False  # Every history page is in; only files may still be coming.
        self.shown = 0  # Messages the last progress report counted.
        self.final_omissions = {}
        self.warnings = ()
        self.emoji_documents = {}
        self.user_locks = {}
        self.chat_locks = {}
        self.own_id = None
        self.emoji_lock = asyncio.Lock()
        self.emoji_locks = {}
        self.download_slots = asyncio.Semaphore(DOWNLOAD_WORKERS)
        self.dc_slots = {}
        self.phase = "bounds"
        self.privacy = Privacy(options.keep_names, options.keep_avatars) if options.anonymize_exporter else None
        if self.privacy:
            self.privacy.unsupported = self.state.get("privacy_unsupported", 0)
        self.private_peer = None
        self.identity = None

    def report(self, phase=None, wait_seconds=0):
        if phase and not wait_seconds:
            self.phase = phase
        skipped = sum(self.omissions.values()) + len(self.unavailable)
        self.shown = len(self.message_ids)
        current = self.cursor.get("current_id", self.state.get("current_id"))
        self.progress(ExportProgress(phase or self.phase, len(self.message_ids), skipped,
            self.state.get("first_id"), self.state.get("last_id"), current,
            self.state.get("history_done", False), wait_seconds, self.checkpoint.root,
            share=self.share(), history_read=self.history_read))

    def share(self):
        """How much of the export is done, 0 to 0.99, or None before the first page.

        Not the resume cursor, which waits for the slowest file. Nor the history read alone, which a
        chat of large files reads in a minute and then downloads for half an hour. The work read so
        far, over the share of the history it covers, estimates the whole; saved messages with their
        files are what is done.
        """
        first, last = self.state.get("first_id"), self.state.get("last_id")
        if self.history_read or first is None or last is None or last <= first:
            covered = 1.0
        else:
            position = self.read_id if self.read_id is not None else self.state.get("offset", last + 1)
            covered = min(1.0, max(0.0, (last - position) / (last - first)))
        if self.work_read <= 0 or covered <= 0:
            return None
        self.share_shown = max(self.share_shown, min(0.99, self.work_done * covered / self.work_read))
        return self.share_shown

    def save(self, **state):
        self.state.update(state, omissions=dict(self.omissions))
        if self.privacy:
            self.state["privacy_unsupported"] = self.privacy.unsupported
        with self.checkpoint.db:
            for data in self.pending_messages:
                self.checkpoint.put("message", data["id"], data)
            for key in self.dirty_users:
                self.checkpoint.put("user", key, self.users[key])
            for key in self.dirty_chats:
                self.checkpoint.put("chat", key, self.chats[key])
            for key in self.dirty_emojis:
                self.checkpoint.put("emoji", key, self.emojis[key])
            for key in self.dirty_unavailable:
                self.checkpoint.put("unavailable", key, self.unavailable[key])
            for key in self.resolved_unavailable:
                self.checkpoint.remove("unavailable", key)
            for key in self.dirty_unresolved:
                self.checkpoint.put("unresolved_user", key, self.unresolved[key])
            for key in self.cleared_unresolved:
                self.checkpoint.remove("unresolved_user", key)
            for key in self.dirty_photo_skipped:
                self.checkpoint.put("photo_skipped", key, True)
            for key in self.cleared_photo_skipped:
                self.checkpoint.remove("photo_skipped", key)
            for key in self.dirty_partial:
                self.checkpoint.put("partial_profile", key, self.partial[key])
            for key in self.cleared_partial:
                self.checkpoint.remove("partial_profile", key)
            self.checkpoint.put("state", "job", self.state)
        self.pending_messages.clear()
        self.dirty_users.clear()
        self.dirty_chats.clear()
        self.dirty_emojis.clear()
        self.dirty_unavailable.clear()
        self.resolved_unavailable.clear()
        self.dirty_unresolved.clear()
        self.cleared_unresolved.clear()
        self.dirty_photo_skipped.clear()
        self.cleared_photo_skipped.clear()
        self.dirty_partial.clear()
        self.cleared_partial.clear()
        self.last_save = time.monotonic()

    def message_record(self, message_id):
        """A saved message, with any change this run has not saved yet."""
        return (next((data for data in reversed(self.pending_messages) if data["id"] == message_id), None)
                or self.checkpoint.get("message", message_id))

    def stored_messages(self, ids=None):
        """Saved messages in history order, read one batch at a time: all, or those IDs."""
        if self.pending_messages:
            self.save()
        return self.checkpoint.values("message", sorted(self.message_ids) if ids is None else ids)

    def export_messages(self, ids=None):
        """Messages as the archive carries them, streamed like stored_messages; `ids` are the
        archive's own, which renumber() may have changed."""
        recovered = self.state.get("emoji_recovered")
        stored = ids if ids is None or not self.numbers else [self.olds[ident] for ident in ids]
        for data in self.stored_messages(stored):
            if recovered:
                attach_emoji_paths(data, self.emojis)
            if self.privacy:
                # Kept as sanitized when collected; the current rules apply again at packing.
                data = self.privacy.node(data)
                if data is None:
                    raise ValueError("Unsupported anonymous message type")
            if self.numbers:
                data = self.renumbered(data)
            yield data

    def renumber(self, meta):
        """Number the messages 1, 2, 3... in history order, and their files alike; returns `meta`
        with its file records numbered so too.

        In a basic group or a private chat every account numbers messages in a sequence of its
        own, so a copy of the chat compared with the archive would say whose it was. A reply to a
        message the archive does not hold points to -1: the viewer then says the original is not in
        the archive, and no number of it is left.
        """
        self.numbers = {old: new for new, old in enumerate(sorted(self.message_ids), 1)}
        self.olds = {new: old for old, new in self.numbers.items()}
        names = set(self.manifest) | {record["path"] for record in meta["unavailable_files"] if record.get("path")}
        for name in names:
            # message_<id>.<ext>, or message_<id>-<n>.<ext> for the files of a rich message.
            stem, dot, rest = name.removeprefix(MESSAGE_FILES).partition(".")
            number, dash, part = stem.partition("-")
            if (name.startswith(MESSAGE_FILES) and number.isdigit() and (not dash or part.isdigit())
                    and int(number) in self.numbers):
                self.renamed[name] = f"{MESSAGE_FILES}{self.numbers[int(number)]}{dash}{part}{dot}{rest}"
        records = []
        for record in meta["unavailable_files"]:
            record = dict(record)
            if "message_id" in record:
                record["message_id"] = self.numbers.get(record["message_id"], -1)
            if record.get("path"):
                record["path"] = self.renamed.get(record["path"], record["path"])
            records.append(record)
        return dict(meta, unavailable_files=records)

    def renumbered(self, data):
        data["id"] = self.numbers[data["id"]]
        reply = data.get("reply_to")
        if isinstance(reply, dict):
            for key in ("reply_to_msg_id", "reply_to_top_id"):
                if isinstance(reply.get(key), int):
                    reply[key] = self.numbers.get(reply[key], -1)
        if data.get("downloaded_media_path"):
            data["downloaded_media_path"] = self.renamed.get(data["downloaded_media_path"],
                                                             data["downloaded_media_path"])
        for record in part_records(data):
            if record.get("downloaded_path"):
                record["downloaded_path"] = self.renamed.get(record["downloaded_path"], record["downloaded_path"])
        return data

    async def optional(self, category, operation):
        check_cancel(self.cancel)
        try:
            return await operation()
        except (errors.RPCError, TimeoutError) as exc:
            optional_error(exc)
            self.omissions[category] += 1
            return None

    async def file(self, name, operation, source, dc=None):
        """Download now; a file Telegram does not provide is recorded and the caller continues."""
        if name in self.checkpoint.files:
            return name
        self.retry_operations[name] = operation
        return await self._fetch(name, operation, source, dc)

    def dc_slot(self, dc):
        """One of the DC_WORKERS places a file on `dc` may take; none for an unknown DC."""
        return self.dc_slots.setdefault(dc, asyncio.Semaphore(DC_WORKERS)) if isinstance(dc, int) else nullcontext()

    async def _fetch(self, name, operation, source, dc=None):
        check_cancel(self.cancel)
        path = self.checkpoint.file_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # The DC's own place first: waiting for it never holds a slot another DC could use.
            async with self.dc_slot(dc), self.download_slots:
                # Retry downloads start at zero; never append a second attempt.
                with path.open("wb") as handle:
                    result = await operation(handle)
        except FileUnavailable as exc:
            path.unlink(missing_ok=True)
            self.defer(name, source, exc.details)
            return None
        except (errors.RPCError, TimeoutError) as exc:
            path.unlink(missing_ok=True)
            optional_error(exc)
            self.defer(name, source, FileUnavailable.from_error(exc).details)
            return None
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self.retry_operations.pop(name, None)
        self.forget_unavailable(name)
        if not result or not path.stat().st_size:
            # Telegram reports nothing to download any more, e.g. a removed avatar.
            path.unlink(missing_ok=True)
            self.omissions["files_unavailable"] += 1
            return None
        self.manifest.append(name)
        self.checkpoint.file_done(name, path.stat().st_size)
        return name

    def defer(self, name, source, error):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        previous = self.unavailable.get(name, {})
        self.unavailable[name] = dict(
            {key: source[key] for key in SOURCE_KEYS if source.get(key) is not None}, path=name,
            attempts=previous.get("attempts", 0) + 1, first_failed_at=previous.get("first_failed_at", now),
            last_failed_at=now, error=error)
        self.dirty_unavailable.add(name)
        self.resolved_unavailable.discard(name)

    def forget_unavailable(self, name):
        if self.unavailable.pop(name, None) is not None:
            self.dirty_unavailable.discard(name)
            self.resolved_unavailable.add(name)

    def retriable(self, record):
        return record["attempts"] < UNAVAILABLE_MAX_ATTEMPTS

    async def retry_unavailable(self):
        self.last_retry = time.monotonic()
        for name, record in sorted(self.unavailable.items()):
            check_cancel(self.cancel)
            if not self.retriable(record):
                continue
            if self.telegram.rate_limited():
                # Retrying a refused file during a cooldown delays the whole export.
                break
            operation = self.retry_operations.get(name)
            if operation is None:
                try:
                    operation = await self.recover(record)
                except FileUnavailable as exc:
                    self.defer(name, record, exc.details)
                    continue
                except (errors.RPCError, TimeoutError, ValueError, TypeError) as exc:
                    if not isinstance(exc, (ValueError, TypeError)):
                        optional_error(exc)
                    self.defer(name, record, FileUnavailable.from_error(exc, stage="recover").details)
                    continue
                if operation is None:
                    # The message or profile no longer has this file.
                    self.forget_unavailable(name)
                    self.omissions["files_unavailable"] += 1
                    continue
            if await self._fetch(name, operation, record):
                self.attach(record, name)
        self.save()

    def attach(self, record, name):
        kind = record["kind"]
        if kind == "message_media":
            data = self.message_record(record["message_id"])
            if data is not None:
                data["downloaded_media_path"] = name
                self.pending_messages.append(data)
        elif kind in ("message_part", "rich_media"):
            data = self.message_record(record["message_id"])
            found = part_records(data, *part_source(record)) if data is not None else []
            for item in found:
                item["downloaded_path"] = name
            if found:
                self.pending_messages.append(data)
        elif kind == "avatar":
            key = str(record["user_id"])
            if self.users.get(key) is not None:
                self.users[key]["avatar_path"] = name
                self.dirty_users.add(key)
        elif kind == "chat_avatar":
            self.state["info"]["chat_photos"] = [{"downloaded_path": name}]
        elif kind == "chat_photo":
            key = str(record["chat_id"])
            if self.chats.get(key) is not None:
                self.chats[key]["avatar_path"] = name
                self.dirty_chats.add(key)
        elif kind == "emoji":
            self.emojis[record["document_id"]] = name
            self.dirty_emojis.add(record["document_id"])
            # Earlier references were stored without the path; attach before publishing.
            self.state["emoji_recovered"] = True

    async def recover(self, record):
        """Find the Telegram object for a file recorded by an earlier run."""
        telegram, peer, kind = self.telegram, self.options.peer, record["kind"]
        if kind == "message_media":
            message = await telegram.message(peer, record["message_id"])
            media = message and (getattr(message, "document", None) or getattr(message, "photo", None))
            if not media:
                return None
            return lambda handle: telegram.download(media, handle, message=message)
        if kind in ("message_part", "rich_media"):
            # Read anew, the message brings the file with a fresh reference.
            message = await telegram.message(peer, record["message_id"])
            wanted = part_source(record)
            media = next((item for found, item in message_parts(message) if (found, item.id) == wanted), None)
            if media is None:
                return None
            return lambda handle: telegram.download(media, handle)
        if kind == "emoji":
            document = (await telegram.emoji_documents([record["document_id"]]) or {}).get(record["document_id"])
            if document is None:
                return None
            return lambda handle: telegram.download(document, handle)
        if kind == "chat_avatar":
            entity = await telegram.entity(peer)
        elif kind == "chat_photo":
            entity = await self.find_chat(record)
        else:
            entity = await self.find_user(record)
        if entity is None:
            raise FileUnavailable("ProfileNotFound", stage="recover")
        photo = getattr(entity, "photo", None)
        if not photo or isinstance(photo, (types.ChatPhotoEmpty, types.UserProfilePhotoEmpty)):
            return None
        return lambda handle: telegram.avatar(entity, handle)

    async def find_chat(self, record):
        # A channel's photo is found again through the message it wrote.
        found = await self.telegram.message(self.options.peer, record["message_id"])
        author = getattr(found, "sender", None)
        return author if isinstance(author, CHATS) and author.id == record["chat_id"] else None

    async def find_user(self, record):
        # A profile seen in a reaction or vote list is addressable again through that list.
        user_id, via, peer = record["user_id"], record.get("via"), self.options.peer
        if via == "reactions":
            async for page in self.telegram.reactions(peer, record["message_id"]):
                if found := next((user for user in page.users if user.id == user_id), None):
                    return found
            return None
        if via == "sender":
            found = await self.telegram.message(peer, record["message_id"])
            author = getattr(found, "sender", None)
            return author if isinstance(author, types.User) and author.id == user_id else None
        if via == "poll_votes":
            message = await self.telegram.message(peer, record["message_id"])
            if message is None or not isinstance(message.media, types.MessageMediaPoll):
                return None
            for answer in message.media.poll.answers:
                async for page in self.telegram.poll_votes(peer, message.id, answer.option):
                    if found := next((user for user in page.users if user.id == user_id), None):
                        return found
            return None
        return await self.telegram.entity(types.PeerUser(user_id))

    def unresolve(self, user_id, **tried):
        key = str(user_id)
        record = self.unresolved.get(key)
        if record is None:
            record = dict(user_id=user_id, tried_by_id=False, tried_by_message=False)
            self.omissions["users_unavailable"] += 1
        record.update(tried, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.unresolved[key] = record
        self.dirty_unresolved.add(key)
        self.cleared_unresolved.discard(key)

    def profile_found(self, user_id):
        key = str(user_id)
        if self.unresolved.pop(key, None) is not None:
            self.omissions["users_unavailable"] -= 1
            if self.omissions["users_unavailable"] <= 0:
                del self.omissions["users_unavailable"]  # An omission of zero is not an omission.
            self.dirty_unresolved.discard(key)
            self.cleared_unresolved.add(key)

    async def authors_from_messages(self, user_ids):
        """Recover people saved by an earlier run, which kept their profiles only in memory.

        A message read anew brings its author again, the people its join or removal names,
        and whom it forwards. Someone its text only links to by name is asked for through it.
        """
        wanted, by_person, by_link = set(user_ids), {}, {}
        for data in self.stored_messages():
            for person in named_people(data):
                if person in wanted and person not in by_person:
                    by_person[person] = data["id"]
            for person in linked_people(data):
                if person in wanted and person not in by_link:
                    by_link[person] = data["id"]
        message_ids = sorted(set(by_person.values()))
        for start in range(0, len(message_ids), 100):
            check_cancel(self.cancel)
            batch = message_ids[start:start + 100]
            try:
                page = await self.telegram.messages_by_id(self.options.peer, batch)
            except (errors.RPCError, TimeoutError) as exc:
                optional_error(exc)
                continue
            except (ValueError, TypeError):
                continue
            for found in page:
                author, origin = getattr(found, "sender", None), forward_origin(found)
                seen = [(author, "sender")] if isinstance(author, types.User) else []
                seen += [(origin, "forward")] if isinstance(origin, types.User) else []
                for person, via in seen + [(person, "action") for person in action_people(found)]:
                    if person.id in wanted:
                        if str(person.id) not in self.users:
                            await self.user(person, via, found.id)
                        self.profile_found(person.id)
            for person, message_id in by_person.items():
                if message_id in set(batch) and str(person) not in self.users and person not in by_link:
                    self.unresolve(person, tried_by_message=True)
            self.save()
            self.report()
        # A link by name does not bring its person back with the message read anew, and a bare ID may
        # not find them either. Asked for through the message, the profile comes whole.
        pairs = sorted((person, message_id) for person, message_id in by_link.items() if str(person) not in self.users)
        for start in range(0, len(pairs), 200):
            check_cancel(self.cancel)
            chunk = pairs[start:start + 200]
            try:
                found = await self.telegram.users_from_messages(self.options.peer, chunk)
            except (errors.RPCError, TimeoutError) as exc:
                optional_error(exc)
                found = {}
            except (ValueError, TypeError):
                found = {}
            for person, message_id in chunk:
                if (user := found.get(person)) is not None:
                    await self.user(user, "mention", message_id)
                    self.profile_found(person)
                else:
                    self.unresolve(person, tried_by_message=True)
            self.save()
            self.report()

    async def final_retries(self):
        for round_number in range(FINAL_RETRY_ROUNDS):
            if not any(self.retriable(record) for record in self.unavailable.values()):
                return
            self.report("files_retry")
            if self.telegram.rate_limited():
                return  # Publishing now beats waiting out a cooldown for a refused file.
            if round_number:
                await asyncio.sleep(FINAL_RETRY_PAUSE_SECONDS)
            await self.retry_unavailable()

    async def enrich_emoji(self, data):
        if self.options.media_mode == "none":
            return
        ids = custom_emoji_ids(data)
        if ids - self.emojis.keys() - self.emoji_documents.keys():
            # One documents request at a time, for the IDs no earlier request covered.
            async with self.emoji_lock:
                missing = sorted(ids - self.emojis.keys() - self.emoji_documents.keys())
                if missing:
                    documents = await self.optional("emoji_unavailable",
                                                    lambda: self.telegram.emoji_documents(missing))
                    self.emoji_documents.update({key: (documents or {}).get(key) for key in missing})
        for doc_id in sorted(ids):
            # Different emoji download side by side; the same one only once.
            async with self.emoji_locks.setdefault(doc_id, asyncio.Lock()):
                if doc_id in self.emojis:
                    continue
                document, path = self.emoji_documents.get(doc_id), None
                if document:
                    path = await self.file(f"media/emojis/emoji_{doc_id}{extension(document)}",
                                           lambda handle: self.telegram.download(document, handle),
                                           dict(kind="emoji", document_id=doc_id), getattr(document, "dc_id", None))
                self.emojis[doc_id] = path
                self.dirty_emojis.add(doc_id)
        attach_emoji_paths(data, self.emojis)

    async def user(self, entity, via="entity", message_id=None):
        # `via` records where this profile was seen, so its avatar can be found again later.
        async with self.user_locks.setdefault(str(entity.id), asyncio.Lock()):
            await self._user(entity, dict(kind="avatar", user_id=entity.id, via=via, message_id=message_id))

    async def _user(self, entity, source):
        key = str(entity.id)
        partial = bool(getattr(entity, "min", False))
        if key in self.users:
            if not partial and (key in self.partial or self.users[key].get("min")):
                await self.whole_profile(entity)  # First seen partial, now whole.
            if key in self.photo_skipped and source["via"] not in NAME_ONLY:
                # First seen reacting or joining, now writing or voting: the photo belongs in the archive.
                await self.user_photo(entity, self.users[key], source)
                self.photo_skipped.discard(key)
                self.dirty_photo_skipped.discard(key)
                self.cleared_photo_skipped.add(key)
                self.dirty_users.add(key)
            return
        data = entity_data(entity)
        if self.privacy:
            data = self.privacy.person(data)
            if data is None:
                return
        self.users[key] = data
        if partial and source["via"] in IN_MESSAGE and source.get("message_id") is not None:
            self.partial[key] = source["message_id"]
            self.cleared_partial.discard(key)
            self.dirty_partial.add(key)
        if self.privacy and not self.privacy.keep_avatars:
            self.dirty_users.add(key)
            return
        if source["via"] in NAME_ONLY:
            # Only reacting, or named by a service message: a photo would cost a request each.
            if self.has_photo(entity):
                self.photo_skipped.add(key)
                self.cleared_photo_skipped.discard(key)
                self.dirty_photo_skipped.add(key)
        else:
            await self.user_photo(entity, data, source)
        if not self.privacy:  # An emoji status is not among what an anonymous archive keeps.
            await self.enrich_emoji(data)
        # Saved only complete: a profile checkpointed before its photo would never get it.
        self.dirty_users.add(key)

    async def whole_profile(self, entity):
        """A person's whole profile in place of the partial one saved first; a saved photo stays."""
        key = str(entity.id)
        data = entity_data(entity)
        if self.privacy:
            data = self.privacy.person(data)
            if data is None:
                return
        if self.users[key].get("avatar_path") and (not self.privacy or self.privacy.keep_avatars):
            data["avatar_path"] = self.users[key]["avatar_path"]
        if not self.privacy:
            await self.enrich_emoji(data)
        self.users[key] = data
        self.dirty_users.add(key)
        self.done_partial(key)

    def done_partial(self, key):
        if self.partial.pop(key, None) is not None:
            self.dirty_partial.discard(key)
            self.cleared_partial.add(key)

    async def whole_profiles(self):
        """Ask for the whole profiles of people Telegram sent only partially, through a message
        they are in; each lookup is spent once, whatever it brings."""
        pending = sorted(self.partial.items())
        for start in range(0, len(pending), 200):
            chunk = pending[start:start + 200]
            check_cancel(self.cancel)
            try:
                found = await self.telegram.users_from_messages(self.options.peer,
                                                                [(int(key), message_id) for key, message_id in chunk])
            except (errors.RPCError, TimeoutError) as exc:
                optional_error(exc)
                found = {}
            except (ValueError, TypeError):
                found = {}
            for key, _ in chunk:
                user = found.get(int(key))
                async with self.user_locks.setdefault(key, asyncio.Lock()):
                    if user is not None and not getattr(user, "min", False) and key in self.users:
                        await self.whole_profile(user)
                    self.done_partial(key)
            self.save()
            self.report()

    def has_photo(self, entity):
        photo = getattr(entity, "photo", None)
        return (self.options.media_mode != "none" and photo
                and not isinstance(photo, (types.UserProfilePhotoEmpty, types.ChatPhotoEmpty)))

    async def chat(self, entity, via="sender", message_id=None):
        """A channel or group that writes, is forwarded from or reacts, as its page brought it.

        Only one that writes shows a photo, as its avatar; the chat itself is dialog_info's.
        """
        if entity.id == self.own_id:
            return
        key = str(entity.id)
        async with self.chat_locks.setdefault(key, asyncio.Lock()):
            data = self.chats.get(key)
            if data is None:
                data = entity_data(entity)
                if self.privacy:
                    data = self.privacy.node(data)
                    if data is None:
                        return
                self.chats[key] = data
                self.dirty_chats.add(key)
            # First seen forwarded or reacting, now writing: the photo belongs in the archive. A
            # channel's photo is the same for every member, so an anonymous archive keeps it too.
            if via != "sender" or "avatar_path" in data or not self.has_photo(entity):
                return
            data["avatar_path"] = await self.file(
                f"media/avatars/chat_{entity.id}.jpg", lambda handle: self.telegram.avatar(entity, handle),
                dict(kind="chat_photo", chat_id=entity.id, via=via, message_id=message_id),
                getattr(entity.photo, "dc_id", None))
            self.dirty_chats.add(key)

    async def user_photo(self, entity, data, source):
        if self.has_photo(entity):
            data["avatar_path"] = await self.file(
                f"media/avatars/user_{entity.id}.jpg", lambda handle: self.telegram.avatar(entity, handle), source,
                getattr(entity.photo, "dc_id", None))

    def reactions_at_hand(self, reactions):
        """The reaction list itself when the message already carries all of it, else None.

        Telegram sends a message's latest three reactions with it, the very records a list
        request returns, in the same order. They suffice when they cover every reaction and
        everyone in them already has a complete profile: the archive is then the list's.
        """
        recent = reactions.recent_reactions or []
        if not recent or sum(r.count for r in reactions.results) > len(recent):
            return None
        people = []
        for reaction in recent:
            user_id = getattr(reaction.peer_id, "user_id", None)
            if user_id is None:
                return None  # A chat reacting as itself: its profile comes only with the list.
            if str(user_id) not in self.users:
                user = self.telegram.known_user(user_id)
                if user is None:
                    return None
                people.append(user)

        async def page():
            yield SimpleNamespace(users=people, reactions=recent, count=len(recent))
        return page()

    async def message_reactions(self, message, data):
        reactions = getattr(message, "reactions", None)
        if not reactions or not reactions.can_see_list:
            return
        collected = []
        pending_users, pending_chats = [], []
        expected = sum(r.count for r in reactions.results) if self.privacy else 0
        complete = True
        pages = self.reactions_at_hand(reactions) or self.telegram.reactions(self.options.peer, message.id)
        try:
            async for page in pages:
                check_cancel(self.cancel)
                # A channel reacting as itself comes with the list's chats.
                chats = [chat for chat in getattr(page, "chats", None) or () if isinstance(chat, CHATS)]
                if self.privacy:
                    pending_users.extend(page.users)
                    pending_chats.extend(chats)
                    expected = max(expected, getattr(page, "count", 0))
                else:
                    for user in page.users:
                        await self.user(user, "reactions", message.id)
                    for chat in chats:
                        await self.chat(chat, "reactions", message.id)
                for reaction in page.reactions:
                    row = json_value(reaction.to_dict())
                    row["user_id"] = getattr(reaction.peer_id, "user_id", None)
                    row["emoji"] = getattr(reaction.reaction, "emoticon", None)
                    collected.append(row)
        except (errors.RPCError, TimeoutError) as exc:
            optional_error(exc)
            self.omissions["reactions_unavailable"] += 1
            complete = False
        if self.privacy:
            collected = ordered(self.privacy.node(collected))
            if complete and len(collected) < expected:
                self.omissions["reactions_unavailable"] += 1
                complete = False
            if not complete:
                collected = []
            else:
                for user in pending_users:
                    await self.user(user, "reactions", message.id)
                for chat in pending_chats:
                    await self.chat(chat, "reactions", message.id)
        data["user_reactions"] = collected

    async def poll(self, message, data):
        if not isinstance(message.media, types.MessageMediaPoll) or not message.media.poll.public_voters:
            return
        if not message.media.poll.closed and (self.privacy or not (message.media.results.results or ())):
            # Telegram shows an open poll's counts and voters only to an account it has let see them
            # and answers the rest POLL_VOTE_REQUIRED. Whether the counts came, not whether the account
            # voted, tells which: without them the voters are not asked for, and the viewer says the
            # counts are missing. An anonymous archive keeps none: privacy.OPEN_POLL_RESULTS.
            return
        by_option = {}
        pending_users = []
        complete = True
        for answer in message.media.poll.answers:
            key = json_value(answer.option)
            by_option[key] = []
            option_complete = True
            expected = max((r.voters for r in (message.media.results.results or [])
                            if r.option == answer.option), default=0) if self.privacy else 0
            try:
                async for page in self.telegram.poll_votes(self.options.peer, message.id, answer.option):
                    check_cancel(self.cancel)
                    if self.privacy:
                        pending_users.extend(page.users)
                        expected = max(expected, getattr(page, "count", 0))
                    else:
                        for user in page.users:
                            await self.user(user, "poll_votes", message.id)
                    by_option[key].extend(json_value(v.to_dict()) for v in page.votes)
            except (errors.RPCError, TimeoutError) as exc:
                optional_error(exc)
                self.omissions["poll_voters_unavailable"] += 1
                option_complete = False
                complete = False
            if self.privacy:
                by_option[key] = ordered(self.privacy.node(by_option[key]))
                if option_complete and len(by_option[key]) < expected:
                    self.omissions["poll_voters_unavailable"] += 1
                    complete = False
        if self.privacy:
            if not complete:
                by_option = {}
            else:
                for user in pending_users:
                    await self.user(user, "poll_votes", message.id)
        data["media"].setdefault("results", {})["votes_by_option"] = by_option

    async def advance(self, window, block=False):
        """Move the saved cursor past the finished start of `window`; with `block`, first wait
        for any message there to finish.

        The cursor passes a message only when it and everything before it are complete, so a
        resume never skips unfinished work. A failed message stops the export.
        """
        running = [task for _, task in window if task is not None and not task.done()]
        if block and running:
            await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(0)  # Let a message just read start its first request.
        failed = next((task for _, task in window if task is not None and task.done()
                       and (task.cancelled() or task.exception() is not None)), None)
        self.settle(window, force=failed is not None)
        if failed is not None:
            failed.result()

    def settle(self, window, force=False, report=True):
        """Take the finished start of `window` into the cursor; save it at most once a second."""
        moved = False
        while window:
            ident, task = window[0]
            if task is None:
                self.cursor["offset"] = ident
            elif task.done() and not task.cancelled() and task.exception() is None:
                self.cursor.update(offset=ident, current_id=ident)
            else:
                break
            window.popleft()
            moved = True
        if self.cursor and (force or time.monotonic() - self.last_save >= SAVE_SECONDS):
            self.save(**self.cursor)
            self.cursor = {}
        # Messages finished past a long download count at once; the percentage waits for the cursor.
        if report and (moved or len(self.message_ids) != self.shown):
            self.report("messages")

    async def drain(self, window):
        while any(task is not None for _, task in window):
            await self.advance(window, block=True)
        self.settle(window, force=True)

    @staticmethod
    async def stop(window):
        tasks = [task for _, task in window if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def message_part_files(self, message, data):
        """A message's files besides its own (message_parts), each saved as one of its own files:
        message_<id>-<n>. Their records name where. A file whose record the anonymous rules dropped
        (an open poll's explanation) is not downloaded."""
        for index, (kind, media) in enumerate(message_parts(message), 1):
            records = part_records(data, kind, media.id)
            if not records:
                continue
            if media_bytes(media) > self.options.media_limit:
                self.omissions["size_limit"] += 1
                continue
            # Downloaded by itself: Telethon refreshes a reference only for the message's own file.
            path = await self.file(f"{MESSAGE_FILES}{message.id}-{index}{extension(media)}",
                                   lambda handle, media=media: self.telegram.download(media, handle),
                                   dict(kind="message_part", message_id=message.id, **{f"{kind}_id": media.id}),
                                   getattr(media, "dc_id", None))
            for record in records if path else ():
                record["downloaded_path"] = path

    async def message(self, message, is_forum):
        data = json_value(message.to_dict())
        if self.privacy:
            # Telegram can omit from_id in private dialogs. Resolve it before
            # discarding out, without persisting which participant is get_me().
            if self.private_peer and not data.get("from_id"):
                sender = self.private_peer
                if getattr(message, "out", False):
                    if self.identity is None:
                        self.identity = await self.telegram.identity()
                        if self.identity is None:
                            raise SignedOut()
                        await self.user(self.identity)
                    sender = self.identity
                data["from_id"] = {"_": "PeerUser", "user_id": sender.id}
            data = self.privacy.node(data)
            if data is None:
                raise ValueError("Unsupported anonymous message type")
        data["topic_info"] = {"topic_id": topic_for(data, is_forum)}
        # The history page carries the author's profile; keeping it costs no request
        # and is the only chance to store it, because the page is not kept.
        author = getattr(message, "sender", None)
        if isinstance(author, types.User):
            await self.user(author, "sender", message.id)
        elif isinstance(author, CHATS):
            await self.chat(author, "sender", message.id)
        # So are the people a service message adds or removes, and where a forward comes
        # from; later their ID is all there is.
        for person in action_people(message):
            await self.user(person, "action", message.id)
        origin = forward_origin(message)
        if isinstance(origin, types.User):
            await self.user(origin, "forward", message.id)
        elif isinstance(origin, CHATS):
            await self.chat(origin, "forward", message.id)
        # So are the people its text links to by name. Looked up only at the end, they would be lost to
        # an export resumed on a new login: only a lookup by ID would be left, which finds only people
        # the login has already seen.
        for user_id in mentioned_people(message) + rich_people(data) + media_people(data):
            if str(user_id) not in self.users and (person := self.telegram.known_user(user_id)) is not None:
                await self.user(person, "mention", message.id)
        # So are a poll's latest voters, whom its results name by ID; the page often brings them only
        # partially, with a name. An anonymous archive keeps no latest voters.
        if not self.privacy:
            for user_id in recent_voters(message):
                if str(user_id) not in self.users and (person := self.telegram.known_user(user_id)
                                                      or self.telegram.partial_user(user_id)) is not None:
                    await self.user(person, "poll_votes", message.id)
        document, photo = getattr(message, "document", None), getattr(message, "photo", None)
        media = document or photo
        if media and self.options.media_mode == "all":
            size = message.file.size or 0
            if size > self.options.media_limit:
                self.omissions["size_limit"] += 1
            else:
                path = f"media/messages/message_{message.id}{extension(media)}"
                data["downloaded_media_path"] = await self.file(
                    path, lambda handle: self.telegram.download(media, handle, message=message),
                    dict(kind="message_media", message_id=message.id), getattr(media, "dc_id", None))
            if document:
                data["file_info"] = {
                    "mime_type": document.mime_type, "size": document.size,
                    "filename": next((a.file_name for a in document.attributes
                                      if isinstance(a, types.DocumentAttributeFilename)), None),
                }
        if self.options.media_mode == "all":
            await self.message_part_files(message, data)
        await self.message_reactions(message, data)
        await self.poll(message, data)
        await self.enrich_emoji(data)
        self.message_ids.add(data["id"])
        self.work_done += self.work_pending.pop(message.id, MESSAGE_WORK)
        self.pending_messages.append(data)

    def work(self, message):
        """What reading and saving `message` costs, for the percentage: MESSAGE_WORK and the bytes of the
        file it downloads."""
        media = getattr(message, "document", None) or getattr(message, "photo", None)
        size = getattr(getattr(message, "file", None), "size", 0) or 0
        if not media or self.options.media_mode != "all" or size > self.options.media_limit:
            size = 0
        return MESSAGE_WORK + size

    async def collect(self):
        if self.privacy:
            self.privacy.unsupported = self.state.get("privacy_unsupported", 0)
        self.report("resuming" if self.message_ids else "bounds")
        if "first_id" not in self.state:
            bounds = await self.telegram.message_bounds(self.options.peer, self.options.start, self.options.end)
            first, last = bounds or (None, None)
            self.save(first_id=first, last_id=last, offset=(last + 1 if last is not None else 0),
                      history_done=bounds is None)
        entity = await self.telegram.entity(self.options.peer)
        self.private_peer = entity if isinstance(entity, types.User) else None
        self.own_id = None if self.private_peer else entity.id
        basic = entity_data(entity)
        if self.privacy:
            basic = self.privacy.person(basic)
            if basic is None:
                raise ValueError("Unsupported anonymous chat type")
        title = basic.get("title") or " ".join(filter(None, (basic.get("first_name"), basic.get("last_name"))))
        if self.privacy and isinstance(entity, types.User):
            title = user_name(basic)
        info = self.state.get("info")
        if info is None:
            # An anonymous export reads a group's details only to learn whether it hides earlier
            # history from new members, and keeps none of them.
            full = await self.optional("chat_info_unavailable", lambda: self.telegram.full_info(entity)) if (
                not self.privacy or getattr(entity, "megagroup", False)) else None
            if self.privacy and getattr(getattr(full, "full_chat", None), "hidden_prehistory", False):
                # The history then begins where the exporter joined, and so does the archive.
                self.state["hidden_history"] = True
            info = dict(basic_info=basic, full_info=json_value(full.to_dict()) if full and not self.privacy else None,
                        title=title, is_forum=bool(getattr(entity, "forum", False)), topics=[], chat_photos=[])
            photo = getattr(entity, "photo", None)
            # A private chat's photo is the other person's: an anonymous export keeps it only with
            # people's photos.
            if (self.options.media_mode != "none" and photo
                    and not (self.privacy and not self.privacy.keep_avatars and isinstance(entity, types.User))
                    and not isinstance(photo, (types.ChatPhotoEmpty, types.UserProfilePhotoEmpty))):
                avatar = await self.file("media/avatars/chat.jpg", lambda handle: self.telegram.avatar(entity, handle),
                                         dict(kind="chat_avatar"), getattr(photo, "dc_id", None))
                info["chat_photos"] = [{"downloaded_path": avatar}]
            if info["is_forum"]:
                topics = await self.optional("topics_unavailable", lambda: self.telegram.topics(self.options.peer))
                info["topics"] = [json_value(t.to_dict()) for t in topics or []]
                if self.privacy:
                    info["topics"] = self.privacy.node(info["topics"])
            await self.enrich_emoji(info)
            self.save(info=info)
        offset = self.state["offset"]
        seen = set(self.message_ids)
        # Messages in history order: (id, task) while being read, (offset, None) where a page ends.
        window = deque()
        self.report("messages")
        try:
            while not self.state["history_done"]:
                check_cancel(self.cancel)
                page = await self.telegram.messages(self.options.peer, offset, min_id=self.state["first_id"] - 1)
                if not page:
                    break
                next_offset = page[-1].id
                if next_offset <= 0 or offset and next_offset >= offset:
                    raise ValueError("Non-advancing message page")
                selected = [message for message in page if message.date
                            and self.options.start <= message.date <= self.options.end
                            and self.state["first_id"] <= message.id <= self.state["last_id"]
                            and message.id not in seen]
                # One metadata request per page of distinct emoji IDs instead of one per emoji.
                if self.options.media_mode != "none":
                    raw = [json_value(message.to_dict()) for message in selected]
                    if self.privacy:
                        raw = Privacy().node(raw)
                    ids = custom_emoji_ids(raw) - self.emojis.keys() - self.emoji_documents.keys()
                    if ids:
                        documents = await self.optional("emoji_unavailable",
                                                        lambda: self.telegram.emoji_documents(ids))
                        self.emoji_documents.update({key: (documents or {}).get(key) for key in ids})
                for message in selected:
                    check_cancel(self.cancel)
                    while sum(task is not None for _, task in window) >= MESSAGE_WINDOW:
                        await self.advance(window, block=True)
                    seen.add(message.id)
                    window.append((message.id, asyncio.create_task(self.message(message, info["is_forum"]))))
                    self.read_id = message.id
                    self.work_pending[message.id] = cost = self.work(message)
                    self.work_read += cost
                    await self.advance(window)
                offset = next_offset
                window.append((offset, None))
                await self.advance(window)
                if (any(self.retriable(record) for record in self.unavailable.values())
                        and time.monotonic() - self.last_retry >= UNAVAILABLE_RETRY_SECONDS):
                    # Retried only while nothing else downloads; history resumes right after.
                    await self.drain(window)
                    await self.retry_unavailable()
                    self.report("messages")
                if offset <= self.state["first_id"] or any(m.date and m.date < self.options.start for m in page):
                    break
            # Every page is in: what is left are files, and the faster reading no longer matters.
            self.history_read = True
            self.report()
            await self.drain(window)
        except BaseException:
            # Whatever finished stays finished for the next attempt; its own error comes first.
            with suppress(Exception):
                self.settle(window, force=True, report=False)
            raise
        finally:
            await self.stop(window)
        self.save(history_done=True)
        self.report("entities")
        # No member list is asked for: the viewer shows only people the messages name, and what the
        # list holds depends on who asks.
        if isinstance(entity, types.User) and not self.state.get("participants_done"):
            await self.user(entity)
        self.save(participants_done=True)
        # Messages mention profiles that may no longer be reachable: never fatal, and
        # resolved in batches so tens of thousands of IDs do not become one request each.
        named, span = set(), []
        for data in self.stored_messages():
            named |= referenced_users(data)
            if data.get("date"):
                moment = datetime.fromisoformat(data["date"])
                span = [min(span[0], moment), max(span[1], moment)] if span else [moment, moment]
        missing = [user_id for user_id in sorted(named) if str(user_id) not in self.users]
        # An author, or someone a join or removal names, is found through that message; a bare
        # ID only works for profiles this account may already address. Each lookup is spent once.
        authors = [user_id for user_id in missing
                   if not self.unresolved.get(str(user_id), {}).get("tried_by_message")]
        if authors:
            await self.authors_from_messages(authors)
        pending = [user_id for user_id in missing if str(user_id) not in self.users
                   and not self.unresolved.get(str(user_id), {}).get("tried_by_id")]
        for start in range(0, len(pending), 200):
            chunk = pending[start:start + 200]
            check_cancel(self.cancel)
            try:
                found = await self.telegram.users_by_id(chunk)
            except (errors.RPCError, TimeoutError) as exc:
                optional_error(exc)
                found = {}
            except (ValueError, TypeError):
                found = {}  # Telegram has no profile this account may address for these IDs.
            for user_id in chunk:
                if user := found.get(user_id):
                    await self.user(user)
                    self.profile_found(user_id)
                else:
                    self.unresolve(user_id, tried_by_id=True)
            self.save()
            self.report()
        await self.whole_profiles()
        await self.final_retries()
        if self.state.get("emoji_recovered"):
            # Messages get theirs as they are written: export_messages.
            for value in (info, self.users):
                attach_emoji_paths(value, self.emojis)
        # Still missing: an omission for the reader, and a record for trying again later.
        unavailable = [self.unavailable[name] for name in sorted(self.unavailable)]
        omissions = Counter({key: count for key, count in self.omissions.items() if count > 0})
        if unavailable:
            omissions["files_unavailable"] += len(unavailable)
        settings = dict(start_date=self.options.start.isoformat(), end_date=self.options.end.isoformat(),
                        media_download_mode=self.options.media_mode,
                        media_download_enabled=self.options.media_mode == "all",
                        media_size_limit_mb=self.options.media_limit / 1024**2)
        if self.privacy:
            settings.update(anonymize_exporter=True, anonymization_version=VERSION,
                            keep_names=self.privacy.keep_names, keep_avatars=self.privacy.keep_avatars)
            # The chosen bounds are the exporter's own: the app sets the end to the moment its window
            # opened, to the millisecond, which dates the export as export_date would. An anonymous
            # archive states the period its messages span instead; the site reads neither.
            del settings["start_date"], settings["end_date"]
            if span:
                settings.update(start_date=span[0].astimezone(timezone.utc).isoformat(),
                                end_date=span[1].astimezone(timezone.utc).isoformat())
        exported_at = datetime.now(timezone.utc).isoformat()
        # Everything but the messages, which are written after it from the checkpoint.
        payload = dict(dialog_info=info, related_entities={"users": self.users, "chats": self.chats},
                       export_date=exported_at, export_settings=settings, omissions=dict(omissions),
                       unavailable_files=unavailable)
        header = dict(id=peer_id(entity), title=title, type=type(entity).__name__, export_date=exported_at,
                      export_settings=settings, messages_count=len(self.message_ids))
        if self.privacy:
            # Omissions stay in the app (ExportResult): which lists and profiles this account could
            # not get depends on the account.
            payload = self.privacy.payload(payload)
            if self.privacy.unsupported:
                omissions["privacy_unsupported"] = self.omissions["privacy_unsupported"] = self.privacy.unsupported
            header.pop("export_date")
            header["title"] = payload["dialog_info"]["title"]
            self.manifest.sort()
        self.final_omissions = dict(omissions)
        self.warnings = ("hidden_history",) if self.state.get("hidden_history") else ()
        return header, payload


async def _job(telegram, options, progress, cancel, buffered=None, floods=None):
    options.validate()
    output = no_links(options.output)
    output.mkdir(parents=True, exist_ok=True)
    identity = await telegram.identity()
    if identity is None:
        raise SignedOut()  # Telethon's get_me answers None once Telegram refuses the login.
    root = checkpoint_path(options, identity.id)
    if options.resume_from is not None and no_links(options.resume_from) != root:
        raise ValueError("Checkpoint belongs to another account, chat or export settings")
    checkpoint = Checkpoint(root)
    retries = getattr(telegram, "retries", None)
    old_record = retries.record if retries else None
    # Which kind of request Telegram slows down, and how much. What earlier runs of this checkpoint
    # counted is taken once per export; this run's own count survives a job attempt whose empty
    # checkpoint was thrown away.
    floods = {} if floods is None else floods
    floods.setdefault("before", checkpoint.get("diagnostic", "flood_waits") or {})
    def flood_waits():
        total = {kind: dict(row) for kind, row in floods["before"].items()}
        for kind, row in (retries.floods.items() if retries else ()):
            into = total.setdefault(kind, {"count": 0, "seconds": 0})
            into["count"] += row["count"]
            into["seconds"] += row["seconds"]
        return total
    def record(diagnostic):
        with checkpoint.db:
            checkpoint.put("diagnostic", "last_error", diagnostic)
            if diagnostic.get("category", "").startswith("Flood"):
                checkpoint.put("diagnostic", "flood_waits", flood_waits())
    if retries:
        retries.record = record
    if buffered:
        # Errors between attempts had no open checkpoint to be recorded in.
        for diagnostic in buffered:
            record(diagnostic)
        buffered.clear()
    published = False
    try:
        collector = Collector(telegram, options, checkpoint.source, progress, cancel, checkpoint)
        collector.identity = identity
        # The archive limit is remembered for a resume but is deliberately absent from the
        # checkpoint fingerprint: it changes packing, never what was collected.
        collector.save(options=dict(peer_id=peer_id(options.peer), start=options.start.isoformat(),
            end=options.end.isoformat(), media_mode=options.media_mode, media_limit=options.media_limit,
            part_size=options.part_size, anonymize_exporter=options.anonymize_exporter,
            archive_limit=options.archive_limit, keep_names=options.keep_names, keep_avatars=options.keep_avatars))
        header, meta = await collector.collect()
        check_cancel(cancel)
        collector.report("archive")
        if collector.privacy and header["type"] in OWN_NUMBERING:
            meta = collector.renumber(meta)
        # The archive's names for the checkpoint's files, and back.
        manifest_all = sorted(collector.renamed.get(name, name) for name in collector.manifest) \
            if collector.renamed else collector.manifest
        sources = {new: old for old, new in collector.renamed.items()}
        limit = options.archive_limit
        sizes = {collector.renamed.get(name, name): size for name, size in checkpoint.files.items()}
        json_path = checkpoint.source / "export.json"

        def write(part, part_header, manifest, ids=None):
            check = MessageCheck() if collector.privacy else None
            written = write_export_json(json_path, part, collector.export_messages(ids), check)
            if check:
                validate_export(part, part_header, manifest, check)
            return written

        # Most collections fit one archive: written whole as it goes, then measured.
        count, nodes = write(meta, header, manifest_all)
        json_size = json_path.stat().st_size
        if limit is not None and (json_size > limit or nodes > SITE_NODE_LIMIT or count > SITE_MESSAGE_LIMIT
                                  or json_size + stored_bytes(manifest_all, sizes) > SITE_ARCHIVE_BYTES
                                  or len(manifest_all) >= SITE_FILE_LIMIT):
            parts = plan_parts(meta, collector.export_messages, header, limit, sizes=sizes,
                               bytes_limit=SITE_ARCHIVE_BYTES, files_limit=SITE_FILE_LIMIT)
        else:
            parts = [(meta, header, manifest_all, None)]
        base = (f"SaveGram_anonymous_{uuid.uuid4().hex[:8]}" if options.anonymize_exporter
                else f"SaveGram_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}")
        produced = []
        for index, (part, part_header, manifest, ids) in enumerate(parts, 1):
            if ids is not None:
                write(part, part_header, manifest, ids)
            # Packing can restart from completed source files without re-reading Telegram.
            with tempfile.TemporaryDirectory(prefix="packing-", dir=root) as temporary:
                work = Path(temporary)
                staging = work / "ready"
                staging.mkdir()
                writer = asyncio.create_task(asyncio.to_thread(write_archive, checkpoint.source, manifest,
                                              part_header, staging, work, cancel, options.part_size, limit,
                                              sources))
                try:
                    files = await asyncio.shield(writer)
                except asyncio.CancelledError:
                    cancel.set()
                    while not writer.done():
                        try:
                            await asyncio.shield(writer)
                        except asyncio.CancelledError:
                            continue
                        except Cancelled:
                            break
                    try:
                        writer.result()
                    except Cancelled:
                        pass
                    raise
                check_cancel(cancel)
                name = base + (f"_{index:02d}" if len(parts) > 1 else "")
                destination = no_links(output / name)
                staging.rename(destination)
                produced.append((destination, files))
                collector.report("archive")
        published = True
        directory = produced[0][0] if len(produced) == 1 else output
        return ExportResult(directory, tuple(target / file.name for target, files in produced for file in files),
                            len(collector.message_ids), collector.final_omissions, flood_waits(),
                            collector.warnings + (("takeout_later",) if getattr(telegram, "takeout_delayed", False)
                                                  else ()))
    finally:
        if retries:
            retries.record = old_record
        try:
            useful = bool(checkpoint.files or checkpoint.db.execute(
                "SELECT 1 FROM records WHERE kind='message' LIMIT 1").fetchone())
        finally:
            checkpoint.close()
        if published or not useful:
            # Verify the exact tree before recursive deletion on Windows.
            checked = no_links(root).resolve()
            if checked.parent != output.resolve() or not checked.name.startswith(".savegram-resume-"):
                raise ValueError("Invalid checkpoint cleanup path")
            try:
                shutil.rmtree(checked)
            except OSError:
                if not published:
                    raise


async def export(telegram, options: ExportOptions, progress, cancel: Event) -> ExportResult:
    """Retry until success or explicit cancellation, retaining durable checkpoints."""
    options.validate()
    latest = ExportProgress("bounds", checkpoint=options.resume_from)
    waiting = None
    loop = asyncio.get_running_loop()
    # Whether this client counts the bytes of file parts and the files on their way.
    counted = hasattr(telegram, "downloaded_bytes")
    speed = DownloadSpeed(loop.time(), getattr(telegram, "downloaded_bytes", 0))
    def report(value):
        nonlocal latest
        latest = value
        if waiting:
            phase, seconds, attempt, category = waiting
            value = replace(value, phase=phase, wait_seconds=seconds,
                            retry_attempt=attempt, error_category=category)
        # Which speed history is read at, said while pages are read. Once they are all in, the fast mode
        # has nothing left to speed up, and nothing asks Telegram for it again.
        if not (value.history_read or value.history_done):
            if getattr(telegram, "takeout_active", False):
                value = replace(value, notes=("takeout_on",))
            elif getattr(telegram, "takeout_delayed", False):
                value = replace(value, notes=("takeout_delayed",))
        if counted:
            value = replace(value, download_speed=speed(loop.time(), telegram.downloaded_bytes,
                                                        telegram.files_in_flight))
        progress(value)
    def on_wait(phase, seconds, attempt, category):
        nonlocal waiting
        waiting = (phase, seconds, attempt, category) if phase else None
        report(latest)
    previous_retries = getattr(telegram, "retries", None)
    retries = telegram.retries = RetryState(on_wait)
    # Between attempts there is no checkpoint open; the next one records what happened.
    buffered, floods = [], {}
    retries.record = buffered.append
    # History pages may go through a takeout session, opened at the first one; every attempt
    # of this export shares it, and it is finished when the export ends, however it ends.
    takeout = getattr(telegram, "end_takeout", None)
    if takeout:
        telegram.allow_takeout()
    finished = False
    async def run_until_done():
        failures = 0
        while True:
            check_cancel(cancel)
            try:
                return await _job(telegram, options, report, cancel, buffered, floods)
            except (Cancelled, CheckpointInUse, ArchiveRejected, SignedOut):
                # Closing the other window, changing the settings or signing in helps; retrying never does.
                raise
            except asyncio.CancelledError:
                if cancel.is_set() or not request_dropped():
                    raise
                # Not this export's cancellation: a request dropped past its own retry.
                failures += 1
                await retries.pause(RequestDropped(), failures, phase="retry_wait")
            except Exception as exc:
                failures += 1
                await retries.pause(exc, failures, phase="retry_wait")
    async def watch_cancel():
        while not cancel.is_set():
            await asyncio.sleep(0.1)
    worker = asyncio.create_task(run_until_done())
    watcher = asyncio.create_task(watch_cancel())
    # The speed is said every moment, also while one long file comes and no message is done.
    # Scheduled on the loop's clock, not by sleeping: a sleep would be one more task to wait for.
    ticker = None
    def tick():
        nonlocal ticker
        ticker = loop.call_later(PROGRESS_TICK_SECONDS, tick)
        report(latest)
    if counted:
        ticker = loop.call_later(PROGRESS_TICK_SECONDS, tick)
    try:
        done, _ = await asyncio.wait((worker, watcher), return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            result = await worker
            finished = True
            return result
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        raise Cancelled()
    except asyncio.CancelledError:
        cancel.set()
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Cancelled):
            pass
        raise
    finally:
        if ticker is not None:
            ticker.cancel()
        if previous_retries is None:
            del telegram.retries
        else:
            telegram.retries = previous_retries
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if takeout:
            await takeout(finished)
