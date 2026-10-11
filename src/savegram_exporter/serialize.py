"""Pure Telegram-value to JSON transformations. No client, filesystem or network."""
import base64
from datetime import datetime


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported export value: {type(value).__name__}")


def entity_data(entity):
    data = json_value(entity.to_dict())
    # These are not needed to render the selected chat.
    data.pop("phone", None)
    data.pop("access_hash", None)
    return data


def referenced_users(value):
    # 0 is no one: a contact of someone without Telegram has user_id 0.
    result = set()
    if isinstance(value, dict):
        # An action names the people it adds or invites by bare ID, not as user_id.
        if str(value.get("_", "")).startswith("MessageAction") and isinstance(value.get("users"), list):
            result.update(item for item in value["users"] if isinstance(item, int) and item > 0)
        for key, item in value.items():
            if key == "user_id" and isinstance(item, int) and item > 0:
                result.add(item)
            elif isinstance(item, (list, dict)):
                result.update(referenced_users(item))
    elif isinstance(value, list):
        for item in value:
            result.update(referenced_users(item))
    return result


def referenced_chats(value):
    """IDs of the channels and groups a message names: an author, a forward's origin, a reactor."""
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("channel_id", "chat_id") and isinstance(item, int) and item > 0:
                result.add(item)
            elif isinstance(item, (list, dict)):
                result.update(referenced_chats(item))
    elif isinstance(value, list):
        for item in value:
            result.update(referenced_chats(item))
    return result


def custom_emoji_ids(value):
    result = set()
    if isinstance(value, dict):
        kind = value.get("_", "")
        if "CustomEmoji" in kind or "EmojiStatus" in kind:
            if isinstance(value.get("document_id"), int):
                result.add(value["document_id"])
        if value.get("icon_emoji_id"):
            result.add(value["icon_emoji_id"])
        # A bot button's own emoji in front of its label (layer 229).
        if kind == "KeyboardButtonStyle" and isinstance(value.get("icon"), int) and value["icon"]:
            result.add(value["icon"])
        for item in value.values():
            if isinstance(item, (list, dict)):
                result.update(custom_emoji_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(custom_emoji_ids(item))
    return result


def attach_emoji_paths(value, paths):
    """Keep Telegram UTF-16 offsets unchanged, including surrogate pairs."""
    if isinstance(value, dict):
        doc_id = (value.get("document_id") or value.get("icon_emoji_id")
                  or (value.get("icon") if value.get("_") == "KeyboardButtonStyle" else None))
        if doc_id in paths and paths[doc_id]:
            value["downloaded_path"] = paths[doc_id]
        for item in list(value.values()):
            if isinstance(item, (dict, list)):
                attach_emoji_paths(item, paths)
    elif isinstance(value, list):
        for item in value:
            attach_emoji_paths(item, paths)


def topic_for(message, is_forum):
    if not is_forum:
        return None
    reply = message.get("reply_to") or {}
    if reply.get("forum_topic"):
        return reply.get("reply_to_top_id") or reply.get("reply_to_msg_id") or 1
    if (message.get("action") or {}).get("_") == "MessageActionTopicCreate":
        return message["id"]
    return 1
