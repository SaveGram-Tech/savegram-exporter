"""Versioned, fail-closed projection of Telegram data for anonymous exports.

This removes account perspective, not identifying content in messages or files.
Never derive these allowlists from Telethon's schema: newly added fields must be
reviewed before they can enter an anonymous archive.
"""
import json
from pathlib import PurePosixPath


VERSION = 5

# Fields describe public content, not the requesting account's state.
FIELDS = {
    # Every participant sees an account as deleted alike.
    "User": "id username deleted", "UserEmpty": "id",
    "Chat": "id title photo participants_count date deactivated",
    # A channel's date is when the exporting account joined it (its creation only for a
    # non-member): the exporter's own history, left out.
    "Channel": "id title photo username broadcast megagroup gigagroup forum",
    "ChatForbidden": "id title", "ChannelForbidden": "id title broadcast megagroup",
    "PeerUser": "user_id", "PeerChat": "chat_id", "PeerChannel": "channel_id",
    "ChatPhoto": "photo_id dc_id has_video", "ChatPhotoEmpty": "",
    # A forum's admins pin a topic for every member.
    "ForumTopic": "id date title icon_color icon_emoji_id top_message from_id closed hidden pinned downloaded_path",
    "ForumTopicDeleted": "id",
    "Message": "id peer_id date message from_id fwd_from via_bot_id reply_to media reply_markup entities "
               "views forwards replies edit_date post_author grouped_id pinned post action "
               "downloaded_media_path file_info topic_info user_reactions reactions rich_message from_rank",
    "MessageEmpty": "id peer_id",
    "MessageFwdHeader": "date from_id from_name channel_post post_author psa_type",
    "MessageReplyHeader": "reply_to_msg_id reply_to_peer_id reply_to_top_id forum_topic "
                          "quote_text quote_entities quote_offset quote",
    "MessageReplyStoryHeader": "peer story_id",
    "MessageReplies": "replies comments channel_id max_id",
    "MessageReactions": "results",
    "ReactionCount": "reaction count",
    "ReactionEmoji": "emoticon", "ReactionCustomEmoji": "document_id downloaded_path",
    "ReactionPaid": "", "ReactionEmpty": "",
    "MessagePeerReaction": "peer_id date reaction user_id emoji",
    "MessagePeerVote": "peer peer_id option date", "MessagePeerVoteMultiple": "peer peer_id options date",
    "MessagePeerVoteInputOption": "peer peer_id date",
    # Layer 224: a poll's picture, its answers' and its explanation's, answers members add and the
    # poll's settings. Not whether the exporter made it (creator) nor what it has not read yet.
    "MessageMediaPoll": "poll results attached_media",
    "Poll": "id question answers closed public_voters multiple_choice quiz close_period close_date "
            "open_answers shuffle_answers revoting_disabled hide_results_until_close",
    "PollAnswer": "text option media added_by date",
    "PollResults": "results total_voters solution solution_entities votes_by_option solution_media",
    "PollAnswerVoters": "option voters correct",
    "TextWithEntities": "text entities",
    # A live photo is a photo and its short video.
    "MessageMediaPhoto": "photo spoiler ttl_seconds live_photo video",
    "MessageMediaDocument": "document spoiler ttl_seconds video round voice",
    "MessageMediaEmpty": "", "MessageMediaUnsupported": "",
    # A rich message's photos and files carry the path of their own download.
    "Photo": "id date sizes video_sizes has_stickers dc_id downloaded_path",
    "PhotoEmpty": "id", "DocumentEmpty": "id",
    "Document": "id date mime_type size dc_id attributes thumbs video_thumbs downloaded_path",
    "PhotoSize": "type w h size", "PhotoSizeEmpty": "type",
    "PhotoSizeProgressive": "type w h sizes", "PhotoCachedSize": "type w h bytes",
    "PhotoStrippedSize": "type bytes", "PhotoPathSize": "type bytes",
    "VideoSize": "type w h size video_start_ts",
    "DocumentAttributeFilename": "file_name", "DocumentAttributeImageSize": "w h",
    "DocumentAttributeVideo": "duration w h round_message supports_streaming nosound",
    "DocumentAttributeAudio": "duration voice title performer waveform",
    "DocumentAttributeAnimated": "", "DocumentAttributeHasStickers": "",
    "DocumentAttributeSticker": "alt stickerset mask mask_coords",
    "DocumentAttributeCustomEmoji": "alt stickerset free text_color",
    "InputStickerSetID": "id", "InputStickerSetShortName": "short_name", "InputStickerSetEmpty": "",
    "MaskCoords": "n x y zoom",
    "MessageMediaGeo": "geo", "MessageMediaGeoLive": "geo period heading proximity_notification_radius",
    "GeoPoint": "long lat accuracy_radius", "GeoPointEmpty": "",
    "MessageMediaVenue": "geo title address provider venue_id venue_type",
    # A contact sent as a message is message content, not the exporter's contacts.
    "MessageMediaContact": "phone_number first_name last_name vcard user_id",
    "MessageMediaDice": "value emoticon",
    # A game, an invoice and a story as every member sees them: not whether the exporter paid
    # (receipt_msg_id, bought extended_media) nor the story as it looks to them.
    "MessageMediaGame": "game", "Game": "id short_name title description",
    "MessageMediaInvoice": "title description currency total_amount",
    "MessageMediaStory": "peer id",
    "MessageMediaWebPage": "webpage",
    "WebPage": "id url display_url type site_name title description photo embed_url embed_type "
               "embed_width embed_height duration author document",
    "WebPageEmpty": "id url", "WebPagePending": "id date url",
    "ReplyInlineMarkup": "rows", "ReplyKeyboardMarkup": "rows resize single_use placeholder",
    "ReplyKeyboardHide": "", "ReplyKeyboardForceReply": "single_use placeholder",
    "KeyboardButtonRow": "buttons", "KeyboardButton": "text type style", "KeyboardButtonUrl": "text url",
    # A bot button keeps the label every member sees. Callback/query tokens and authorization
    # buttons are deliberately omitted.
    "KeyboardButtonCallback": "text", "KeyboardButtonSwitchInline": "text", "KeyboardButtonGame": "text",
    # In Telegram's layer 229 a button is its label, what it does and how it looks.
    # A button whose action is not listed here is left out whole: ACTION_BUTTONS.
    "KeyboardInlineButtonRow": "buttons", "KeyboardInlineButton": "text type style",
    "KeyboardButtonStyle": "bg_primary bg_danger bg_success icon downloaded_path",
    "InlineButtonTypeUrl": "url", "InlineButtonTypeCallback": "", "InlineButtonTypeSwitchInline": "",
    "InlineButtonTypeGame": "", "InlineButtonTypeBuy": "", "InlineButtonTypeDisabled": "",
    "InlineButtonTypeCopy": "copy_text", "InlineButtonTypeUserProfile": "user_id", "InlineButtonTypeWebView": "url",
    "ButtonTypeDefault": "", "ButtonTypeRequestPhone": "", "ButtonTypeRequestGeoLocation": "",
    "ButtonTypeRequestPoll": "quiz", "ButtonTypeSimpleWebView": "url",
    # A rich message, written in Telegram's editor (layer 227): blocks of formatted text, tables,
    # lists and the photos and files it shows, the same for every member. An embed's HTML is left
    # out: the viewer never shows one.
    "RichMessage": "blocks photos documents rtl",
    "PageCaption": "text credit",
    "PageBlockUnsupported": "", "PageBlockDivider": "", "PageBlockAnchor": "name",
    "PageBlockAuthorDate": "author published_date",
    "PageBlockPreformatted": "text language",
    "PageBlockList": "items", "PageBlockOrderedList": "items reversed start type",
    "PageListItemText": "text checkbox checked", "PageListItemBlocks": "blocks checkbox checked",
    "PageListOrderedItemText": "text checkbox checked num value type",
    "PageListOrderedItemBlocks": "blocks checkbox checked num value type",
    "PageBlockBlockquote": "text caption collapsed", "PageBlockBlockquoteBlocks": "blocks caption",
    "PageBlockPullquote": "text caption",
    "PageBlockPhoto": "photo_id caption spoiler url", "PageBlockVideo": "video_id caption autoplay loop spoiler",
    "PageBlockAudio": "audio_id caption", "PageBlockDocument": "document_id caption",
    "PageBlockCover": "cover", "PageBlockCollage": "items caption", "PageBlockSlideshow": "items caption",
    "PageBlockEmbed": "url poster_photo_id w h full_width allow_scrolling caption",
    "PageBlockEmbedPost": "url author author_photo_id date blocks caption",
    "PageBlockChannel": "channel",
    "PageBlockTable": "title rows bordered striped compact", "PageTableRow": "cells",
    "PageTableCell": "text header align_center align_right valign_middle valign_bottom colspan rowspan",
    "PageBlockDetails": "blocks title open",
    "PageBlockRelatedArticles": "title articles",
    "PageRelatedArticle": "url title description photo_id author published_date",
    "PageBlockMap": "geo zoom w h caption", "PageBlockMath": "source", "PageBlockThinking": "text",
    "PageBlockButtonRow": "buttons align_left align_center align_right", "PageButton": "text type style",
    "TextButton": "text type style", "RichButtonStyle": "bg_primary bg_danger bg_success link",
    "TextEmpty": "", "TextConcat": "texts", "TextUrl": "text url", "TextEmail": "text email",
    "TextPhone": "text phone", "TextAnchor": "text name", "TextMentionName": "text user_id",
    "TextImage": "document_id w h", "TextCustomEmoji": "document_id alt downloaded_path", "TextMath": "source",
    "TextDate": "text date relative short_time long_time short_date long_date day_of_week",
    "TextDiff": "text old_text",
    "MessageActionEmpty": "", "MessageActionChatCreate": "title users",
    "MessageActionChannelCreate": "title", "MessageActionChatEditTitle": "title",
    "MessageActionChatEditPhoto": "photo", "MessageActionChatDeletePhoto": "",
    "MessageActionChatAddUser": "users", "MessageActionChatDeleteUser": "user_id",
    "MessageActionChatJoinedByLink": "inviter_id", "MessageActionChatJoinedByRequest": "",
    "MessageActionChatMigrateTo": "channel_id", "MessageActionChannelMigrateFrom": "title chat_id",
    "MessageActionPinMessage": "", "MessageActionCustomAction": "message",
    "MessageActionTopicCreate": "title icon_color icon_emoji_id downloaded_path",
    "MessageActionTopicEdit": "title icon_emoji_id closed hidden downloaded_path",
    "MessageActionSetMessagesTTL": "period auto_setting_from",
    "MessageActionGroupCallScheduled": "schedule_date", "MessageActionGroupCall": "duration",
    "MessageActionInviteToGroupCall": "users", "MessageActionBoostApply": "boosts",
    # Actions both sides of a chat see the same. Call and wallpaper IDs, gift themes, payments
    # and bot exchanges stay out; a call's direction lives in `out`.
    "MessageActionGameScore": "game_id score", "MessageActionScreenshotTaken": "",
    "MessageActionGiveawayLaunch": "stars", "MessageActionGiveawayResults": "winners_count unclaimed_count stars",
    "MessageActionPaidMessagesPrice": "stars broadcast_messages_allowed",
    "MessageActionTodoAppendTasks": "list", "TodoItem": "id title",
    # A checklist and who ticked what, as every member sees it; answers added to a poll.
    "MessageMediaToDo": "todo completions", "TodoList": "title list others_can_append others_can_complete",
    "TodoCompletion": "id completed_by date",
    "MessageActionPollAppendAnswer": "answer", "MessageActionPollDeleteAnswer": "answer",
    "MessageActionTodoCompletions": "completed incompleted",
    "MessageActionGeoProximityReached": "from_id to_id distance",
    "MessageActionSetChatTheme": "theme", "ChatTheme": "emoticon",
    "MessageActionSetChatWallPaper": "same for_both",
    "MessageActionPhoneCall": "video reason duration",
    "PhoneCallDiscardReasonMissed": "", "PhoneCallDiscardReasonBusy": "",
    "PhoneCallDiscardReasonHangup": "", "PhoneCallDiscardReasonDisconnect": "",
    "PhoneCallDiscardReasonMigrateConferenceCall": "",
}
FIELDS["MessageService"] = FIELDS["Message"]
for _kind in (
    "Unknown Mention Hashtag BotCommand Url Email Bold Italic Code Phone Cashtag Underline Strike "
    "BankCard Spoiler"
).split():
    FIELDS[f"MessageEntity{_kind}"] = "offset length"
FIELDS.update({
    "MessageEntityPre": "offset length language", "MessageEntityTextUrl": "offset length url",
    "MessageEntityMentionName": "offset length user_id",
    "MessageEntityCustomEmoji": "offset length document_id downloaded_path",
    "MessageEntityBlockquote": "offset length collapsed",
    # Layers 223-224: a date shown in the reader's own format, and a text's corrections.
    "MessageEntityFormattedDate": "offset length date relative short_time long_time short_date long_date day_of_week",
    "MessageEntityDiffInsert": "offset length", "MessageEntityDiffDelete": "offset length",
    "MessageEntityDiffReplace": "offset length old_text",
})
# A rich message's text styles and its blocks of one text.
for _kind in (
    "Plain Bold Italic Underline Strike Fixed Subscript Superscript Marked Spoiler Mention Hashtag Cashtag "
    "BotCommand BankCard AutoUrl AutoEmail AutoPhone"
).split():
    FIELDS[f"Text{_kind}"] = "text"
for _kind in "Title Subtitle Header Subheader Kicker Paragraph Footer Heading1 Heading2 Heading3 Heading4 Heading5 " \
             "Heading6".split():
    FIELDS[f"PageBlock{_kind}"] = "text"
FIELDS = {kind: frozenset(fields.split()) for kind, fields in FIELDS.items()}
# People's names and photos, when the exporter chooses to keep them. They stay out by default:
# Telegram gives each account the name and photo it saved for a contact, and some photos only
# to contacts, so they can show whose export it is.
KEPT_NAMES = ("first_name", "last_name")
SETTINGS = frozenset("start_date end_date media_download_mode media_download_enabled media_size_limit_mb "
                     "anonymize_exporter anonymization_version keep_names keep_avatars".split())
# Until a poll closes, Telegram shows its per-answer counts, a quiz's answer and explanation
# and its voters only to members who voted; the rest get POLL_VOTE_REQUIRED for the voters.
# An open poll keeps what every member sees, so the archive does not tell who voted where.
OPEN_POLL_RESULTS = frozenset({"_", "total_voters"})
# A bot button whose action is not reviewed is left out label and all, like a login button;
# a button in a rich message's text keeps its words.
ACTION_BUTTONS = frozenset({"KeyboardInlineButton", "KeyboardButton"})
UNAVAILABLE_FIELDS = ("path", "kind", "message_id", "document_id", "photo_id", "attempts")


def ordered(rows):
    """Canonicalize public sets; never retain Telegram's account-specific order."""
    unique = {json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in rows}
    return [unique[key] for key in sorted(unique)]


def user_label(user):
    return f"@{user['username']}" if user.get("username") else str(user["id"])


def user_name(user):
    """The name a profile carries, if it was kept; else @username or ID."""
    return " ".join(filter(None, (user.get(key) for key in KEPT_NAMES))) or user_label(user)


def scalars(value, fields):
    """Exporter-owned plain records cannot smuggle nested Telegram structures."""
    result = {key: value[key] for key in sorted(fields) if key in value}
    if any(item is not None and not isinstance(item, (str, int, float, bool)) for item in result.values()):
        raise ValueError("Unexpected nested anonymous metadata")
    return result


class Privacy:
    def __init__(self, keep_names=False, keep_avatars=False):
        self.unsupported = 0
        self.keep_names, self.keep_avatars = keep_names is True, keep_avatars is True

    def node(self, value):
        if isinstance(value, list):
            return [clean for item in value if (clean := self.node(item)) is not None]
        if not isinstance(value, dict):
            return value
        kind = value.get("_")
        if kind not in FIELDS:
            self.unsupported += 1
            return None
        result = {"_": kind}
        for key in sorted(FIELDS[kind]):
            if value.get(key) is None:
                continue
            if key == "topic_info":
                clean = scalars(value[key], ("topic_id",))
            elif key == "file_info":
                clean = scalars(value[key], ("mime_type", "size", "filename"))
            elif key == "votes_by_option":
                clean = {k: ordered(self.node(v)) for k, v in sorted(value[key].items())}
            else:
                clean = self.node(value[key])
            if clean is not None:
                is_set = key == "user_reactions" or key == "results" and kind in {"MessageReactions", "PollResults"}
                result[key] = ordered(clean) if is_set else clean
        if kind == "MessageMediaPoll" and isinstance(result.get("results"), dict) \
                and not (result.get("poll") or {}).get("closed"):
            result["results"] = {key: item for key, item in result["results"].items() if key in OPEN_POLL_RESULTS}
        if kind in ACTION_BUTTONS and isinstance(value.get("type"), dict) and "type" not in result:
            return None
        return result

    def person(self, value):
        """Someone's profile: ID, @username and whether the account is deleted; their name and
        their photo, downloaded by the exporter, only if it chose to keep them."""
        clean = self.node(value)
        if not isinstance(clean, dict) or clean.get("_") != "User":
            return clean
        if self.keep_names:
            clean.update({key: name for key, name in scalars(value, KEPT_NAMES).items() if name is not None})
        path = value.get("avatar_path")
        if self.keep_avatars and path == f"media/avatars/user_{clean.get('id')}.jpg":
            clean["avatar_path"] = path
        return clean

    def chat(self, value):
        """A channel or group's public identity, with its photo when it writes: a channel's photo
        is the same for every member, unlike a person's."""
        clean = self.node(value)
        path = value.get("avatar_path") if isinstance(value, dict) else None
        if isinstance(clean, dict) and path == f"media/avatars/chat_{clean.get('id')}.jpg":
            clean["avatar_path"] = path
        return clean

    def payload(self, value):
        info = value["dialog_info"]
        basic = self.person(info["basic_info"])
        if basic is None:
            raise ValueError("Unsupported anonymous chat type")
        is_user = basic["_"] in {"User", "UserEmpty"}
        title = user_name(basic) if is_user else basic.get("title", "Telegram archive")
        return {
            "dialog_info": {
                "basic_info": basic, "full_info": None, "title": title,
                "is_forum": bool(info["is_forum"]),
                # Telegram's own order, the same for every member: General, the pinned topics in the
                # admins' order, then by the latest message.
                "topics": self.node(info["topics"]),
                # A private chat's photo is the other person's: kept only with people's photos.
                "chat_photos": [] if is_user and not self.keep_avatars else [
                    scalars(p, ("downloaded_path",)) for p in info["chat_photos"]
                    if p.get("downloaded_path")],
            },
            "related_entities": {
                "users": {k: self.person(v) for k, v in sorted(value["related_entities"]["users"].items())},
                # Channels and groups that write, are forwarded from or react: public identity only.
                "chats": {k: self.chat(v) for k, v in sorted(value["related_entities"].get("chats", {}).items())}},
            "export_settings": scalars(value["export_settings"], SETTINGS),
            # No omission counts: which lists or profiles an account could not get is its own
            # history. The app shows them; the archive records only the files still missing.
            # Enough to try these files again later; no profile IDs, DCs or times of the export.
            "unavailable_files": [
                dict(scalars(record, UNAVAILABLE_FIELDS), error=scalars(record.get("error") or {}, ("category", "rpc_code")))
                for record in value.get("unavailable_files", [])],
            # Written last, after everything they refer to; a payload may come without them.
            **({"messages": self.node(value["messages"])} if "messages" in value else {}),
        }


class MessageCheck:
    """Validates anonymous messages one at a time as they are written, keeping only their
    number and the files they name."""
    def __init__(self):
        self.privacy, self.count, self.paths = Privacy(), 0, set()

    def __call__(self, message):
        if self.privacy.node(message) != message or self.privacy.unsupported:
            raise ValueError("Anonymous export failed privacy validation")
        self.count += 1
        self.paths |= file_paths(message)


def file_paths(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"downloaded_path", "downloaded_media_path"} and item:
                result.add(item)
            elif isinstance(item, (list, dict)):
                result.update(file_paths(item))
    elif isinstance(value, list):
        for item in value:
            result.update(file_paths(item))
    return result


def validate_export(payload, header, manifest, checked=None):
    """Reject unsanitized output and orphan files before anything is published.

    `checked` is the MessageCheck the archive's messages went through as they were written,
    for a payload that comes without them.
    """
    if checked is None:
        checked = MessageCheck()
        for message in payload["messages"]:
            checked(message)
    if payload.get("messages_last", True) is not True:
        raise ValueError("Unexpected anonymous payload layout")
    # The messages and the note that they come last are the writer's own.
    payload = {key: value for key, value in payload.items() if key not in ("messages", "messages_last")}
    settings = payload["export_settings"]
    if any(settings.get(key) not in (None, True, False) for key in ("keep_names", "keep_avatars")):
        raise ValueError("Unexpected anonymous export options")
    privacy = Privacy(settings.get("keep_names"), settings.get("keep_avatars"))
    if privacy.payload(payload) != payload or privacy.unsupported:
        raise ValueError("Anonymous export failed privacy validation")
    for key, user in payload["related_entities"]["users"].items():
        if user.get("_") not in {"User", "UserEmpty"} or key != str(user["id"]):
            raise ValueError("Unexpected anonymous profile")
    for key, chat in payload["related_entities"].get("chats", {}).items():
        if chat.get("_") not in {"Channel", "ChannelForbidden", "Chat", "ChatForbidden"} or key != str(chat["id"]):
            raise ValueError("Unexpected anonymous chat profile")
    if settings.get("anonymize_exporter") is not True or settings.get("anonymization_version") != VERSION:
        raise ValueError("Missing anonymous export version")
    if set(header) != {"id", "title", "type", "export_settings", "messages_count"}:
        raise ValueError("Unexpected anonymous archive header")
    if (header["export_settings"] != settings or header["title"] != payload["dialog_info"]["title"]
            or header["messages_count"] != checked.count):
        raise ValueError("Inconsistent anonymous archive header")
    basic = payload["dialog_info"]["basic_info"]
    expected_id = basic["id"]
    if basic["_"] in {"Channel", "ChannelForbidden"}:
        expected_id = -(1000000000000 + expected_id)
    elif basic["_"] in {"Chat", "ChatForbidden"}:
        expected_id = -expected_id
    if header["id"] != expected_id or header["type"] != basic["_"]:
        raise ValueError("Inconsistent anonymous chat identity")
    # People's photos come in only as the photos of profiles that kept them (privacy.person).
    people = {user["avatar_path"] for user in payload["related_entities"]["users"].values() if user.get("avatar_path")}
    paths = file_paths(payload) | checked.paths | people | {
        chat["avatar_path"] for chat in payload["related_entities"].get("chats", {}).values() if chat.get("avatar_path")}
    if paths != set(manifest) or len(manifest) != len(paths):
        raise ValueError("Unexpected anonymous archive files")
    for name in paths:
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
                or not name.startswith("media/") or name.startswith("media/avatars/user_") and name not in people):
            raise ValueError("Unsafe anonymous media path")
