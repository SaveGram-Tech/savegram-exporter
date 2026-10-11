"""Single GUI entry point. No raw exception text or Telegram data in diagnostics."""
import asyncio
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import sys
from threading import Event

from PyQt5.QtCore import QDateTime, QEvent, QLocale, QSettings, Qt, QUrl
from PyQt5.QtGui import QColor, QDesktopServices, QImage, QPainter, QPixmap, QStandardItem
from PyQt5.QtWidgets import (QAbstractSpinBox, QActionGroup, QApplication, QComboBox, QCompleter, QDateTimeEdit,
                             QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QMenu,
                             QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                             QSizePolicy, QSpinBox, QVBoxLayout, QWidget)
import qasync
import qrcode
from telethon import types

from . import __version__, theme
from .account import Account
from .chat_photos import ChatPhotos
from .credentials import ConfigurationError, load_credentials
from .checkpoint import read_options
from .export import export
from .faq import CONTROL_TOPICS, FaqWindow
from .i18n import TEXT, tr
from .archive import ArchiveRejected
from .checkpoint import CheckpointInUse
from .models import SITE_JSON_LIMIT, AppRejected, Cancelled, ExportOptions, SignedOut
from .pictures import TAKEOUT_REQUEST
from .storage import AppKeys, Vault, validate_credentials
from .telegram import TAKEOUT_WAIT_ASK_SECONDS, peer_id


LANGUAGES = (("English", "en"), ("Русский", "ru"), ("Español", "es"))
# How each button looks (theme.STYLE): the action a screen is for is "primary", leaving is "danger".
BUTTON_KINDS = {"login": "primary", "submit": "primary", "export": "primary", "forget_local": "danger",
                "logout": "danger", "cancel": "danger", "faq": "link", "site": "link", "source": "link",
                "resume": "link"}
# Where a row of the chat list keeps its chat's key, which its photo is kept by.
CHAT_KEY = Qt.UserRole + 1
# The window opens this big, in Qt's pixels: wide enough for the settings in two columns, and short enough
# for a 1080p screen at Windows' 125 %, under its taskbar and title bar, with nothing to scroll.
WINDOW_SIZE = (960, 780)
# How wide Telegram's "Data export request" shows before an export, in Qt's pixels.
TAKEOUT_PICTURE_WIDTH = 250
# How long the fast mode's "allowed" stays on screen before the export starts.
TAKEOUT_READY_SECONDS = 1.2


def settings_store():
    """The window's own choices (the theme), in %APPDATA%: never the account, which only the Vault keeps."""
    return QSettings(QSettings.IniFormat, QSettings.UserScope, "SaveGram", "SaveGram Exporter")


def system_language():
    """The first of the system's display languages the app speaks, else English."""
    for name in QLocale.system().uiLanguages():
        code = name.replace("_", "-").split("-")[0].lower()
        if code in {code for _, code in LANGUAGES}:
            return code
    return "en"


def label(text="", role=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    widget.setWordWrap(True)
    if role:
        widget.setProperty("role", role)
    return widget


class KeysProblem(Exception):
    """What keeps the API ID and API Hash typed in from signing in, said by the note above their fields:
    the note's text key."""


class Status(QLabel):
    """The line saying how things stand; it tells its card when it has something to say."""

    def __init__(self, changed):
        super().__init__()
        self.changed = changed
        self.setObjectName("status")
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)

    def setText(self, text):
        super().setText(text)
        self.setVisible(bool(text))  # Empty, it took a line of the status card's height.
        self.changed()

    def clear(self):
        super().clear()
        self.hide()
        self.changed()


def qr_image(url):
    qr = qrcode.QRCode(border=4)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    scale = 4
    image = QImage(len(matrix) * scale, len(matrix) * scale, QImage.Format_RGB32)
    image.fill(Qt.white)
    painter = QPainter(image)
    for y, row in enumerate(matrix):
        for x, cell in enumerate(row):
            if cell:
                painter.fillRect(x * scale, y * scale, scale, scale, QColor(Qt.black))
    painter.end()
    return image


class Window(QMainWindow):
    def __init__(self, vault=None, credentials_loader=load_credentials, keys=None):
        super().__init__()
        self.setWindowTitle(f"SaveGram Exporter {__version__}")
        self.resize(*WINDOW_SIZE)
        self.setMinimumWidth(640)
        self.account = Account(vault or Vault())
        # A person's own API ID and API Hash: kept beside the remembered account, never as part of it.
        self.keys = keys or AppKeys(self.account.vault.directory)
        self.task = None
        self.cancel_event = Event()
        self.password_future = None
        self.ready = False
        self.allow_close = False
        self.closing = False
        self.needs_session_check = False
        self.saved = None
        self.result = None
        self.last_progress = None
        self.resume_from = None
        self.translated = []
        self.help_rows = {}
        self.help_buttons = {}
        self.busy_controls = []
        self.faq_window = FaqWindow(self)
        self.vault_failed = False
        # The program's own API ID and API Hash (none in a copy built without them), a person's own if they
        # entered some, and whether Telegram refused the program's own since the window opened.
        self.builtin = self.own = None
        self.refused = False
        try:
            self.builtin = credentials_loader()
        except ConfigurationError:
            pass
        try:
            self.own = self.keys.load()
        except Exception:
            pass  # Unreadable here, another Windows user's copy say: asked for again.
        self.keys_state = "keys_missing"
        central = QWidget()
        central.setObjectName("page")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 12, 20, 10)
        layout.setSpacing(10)
        # The header: SaveGram's mark and name; the language and the help on the right.
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addWidget(theme.Mark(44))
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel("SaveGram Exporter")
        title.setObjectName("appTitle")
        names.addWidget(title)
        self.subtitle = label()
        self.subtitle.setObjectName("appSubtitle")
        self.translated.append((lambda text: self.subtitle.setText(f"{text} · {__version__}"), "tagline"))
        names.addWidget(self.subtitle)
        header.addLayout(names, 1)
        self.language = QComboBox()
        for name, code in LANGUAGES:
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(system_language())))
        self.translated.append((self.language.setToolTip, "hint_language"))
        header.addWidget(self.language)
        # Light, night, or as Windows is set; remembered for the next start.
        self.settings = settings_store()
        self.theme_button = theme.ThemeButton()
        themes = QMenu(self.theme_button)
        group = QActionGroup(themes)
        self.theme_actions = {}
        for mode in theme.MODES:
            action = themes.addAction("")
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(lambda checked=False, mode=mode: self.choose_theme(mode))
            self.translated.append((action.setText, "theme_" + mode))
            self.theme_actions[mode] = action
        self.theme_button.setMenu(themes)
        saved = self.settings.value("theme", "system")
        saved = saved if saved in theme.MODES else "system"
        if saved != "system":  # main() has already applied Windows' own choice.
            theme.set_mode(QApplication.instance(), saved)
        self.theme_button.show_mode(saved)
        self.theme_actions[saved].setChecked(True)
        self.translated.append((self.theme_button.setToolTip, "theme"))
        self.translated.append((self.theme_button.setAccessibleName, "theme"))
        header.addWidget(self.theme_button)
        self.button(header, "faq", self.open_faq)
        layout.addLayout(header)
        self.add_label(layout, "privacy", role="hint")
        self.auth_box = theme.Card()
        self.translated.append((self.auth_box.setTitle, "auth"))
        # Rows across the whole card: a form layout kept the QR code's row as high as before the code.
        auth = self.auth_box.body
        # A person's own API ID and API Hash, asked for only when this copy has none, Telegram refused its
        # own, or they entered theirs before; the note above says which, or what is wrong with them.
        self.keys_box = QWidget()
        keys = QVBoxLayout(self.keys_box)
        keys.setContentsMargins(0, 0, 0, 0)
        keys.setSpacing(10)
        self.keys_note = label(role="warning")
        keys.addWidget(self.keys_note)
        fields = QFormLayout()
        fields.setHorizontalSpacing(14)
        fields.setVerticalSpacing(10)
        fields.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        keys.addLayout(fields)
        self.app_id_field, self.app_hash_field = QLineEdit(), QLineEdit()
        for key, field, length in (("app_id", self.app_id_field, 12), ("app_hash", self.app_hash_field, 64)):
            field.setMaxLength(length)
            self.translated.append((field.setPlaceholderText, key + "_example"))
            self.row(fields, key, field)
        if self.own:
            self.app_id_field.setText(str(self.own[0]))
            self.app_hash_field.setText(self.own[1])
        auth.addWidget(self.keys_box)
        self.remember = theme.Switch()
        self.translated.append((self.remember.setText, "remember"))
        auth.addWidget(self.with_help(self.remember, "remember"))
        self.busy_controls.append(self.remember)
        self.login_button = self.button(auth, "login",
                                        lambda: self.start_task(self.login(), cancelled="login_cancelled"))
        self.forget_button = self.button(auth, "forget_local", self.forget_local)
        self.show_control(self.forget_button, False)
        # A plain label: one that wraps words squeezes its picture to a line of text.
        self.qr = QLabel()
        self.qr.setTextFormat(Qt.PlainText)
        self.qr.setObjectName("qr")
        self.qr.hide()
        self.qr.setAlignment(Qt.AlignCenter)
        self.qr.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        auth.addWidget(self.qr, 0, Qt.AlignHCenter)
        self.qr_hint = self.add_label(auth, "qr_hint", role="hint")
        self.qr_hint.setAlignment(Qt.AlignCenter)
        self.qr_hint.hide()
        self.twofa_label = self.add_label(auth, "twofa")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        auth.addWidget(self.with_help(self.password, "twofa"))
        self.password_button = self.button(auth, "submit", self.submit_password)
        self.show_password(False)
        layout.addWidget(self.auth_box)
        # Who is signed in, as Telegram shows someone without a photo.
        self.profile = theme.Card()
        who = QHBoxLayout()
        who.setSpacing(12)
        self.avatar = theme.Avatar(44)
        who.addWidget(self.avatar)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.identity = label()
        self.identity.setObjectName("profileName")
        names.addWidget(self.identity)
        self.add_label(names, "signed_in", role="hint")
        who.addLayout(names, 1)
        self.logout_button = self.button(who, "logout", lambda: self.start_task(self.logout()))
        self.show_control(self.logout_button, False)
        self.profile.body.addLayout(who)
        self.profile.hide()
        layout.addWidget(self.profile)
        self.export_box = theme.Card()
        self.translated.append((self.export_box.setTitle, "select"))
        # The settings in two columns, related ones side by side, each explained by its "?": the whole window
        # fits a 1080p screen without a scroll bar. While an export runs they fold into one line that says
        # what is being exported.
        self.settings_form = QWidget()
        grid = QGridLayout(self.settings_form)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.export_box.body.addWidget(self.settings_form)
        self.settings_summary = label(role="hint")
        self.settings_summary.hide()
        self.export_box.body.addWidget(self.settings_summary)
        self.chats = QComboBox()
        # Hundreds of chats: typing any part of a name offers the ones that match, as Telegram's search does.
        self.chats.setEditable(True)
        self.chats.setInsertPolicy(QComboBox.NoInsert)
        self.chats.setMaxVisibleItems(15)
        search = QCompleter(self.chats.model(), self.chats)
        search.setFilterMode(Qt.MatchContains)
        search.setCaseSensitivity(Qt.CaseInsensitive)
        search.setCompletionMode(QCompleter.PopupCompletion)
        search.setMaxVisibleItems(12)
        search.popup().setObjectName("chatSearch")
        # Each chat with its round picture, as Telegram lists them, in the list and in the search alike
        # (the completer's own item painter also left its rows cramped, under a scroll bar). A photo
        # is asked for once its row shows.
        self.chat_photos = ChatPhotos(self.chat_photo_arrived)
        self.chat_faces, self.chat_photo_images, self.chat_pictures = {}, {}, {}
        # Held here: a painter owned only by Qt's own view loses its Python half, and Qt draws plain rows.
        self.chat_rows = [theme.ChatRow(self.chat_picture, self.chats) for _ in range(2)]
        for view, rows in zip((self.chats.view(), search.popup()), self.chat_rows):
            view.setItemDelegate(rows)
            view.setUniformItemSizes(True)
        self.chats.setCompleter(search)
        self.chats.lineEdit().editingFinished.connect(self.settle_chat_text)
        self.translated.append((self.chats.lineEdit().setPlaceholderText, "chat_search"))
        self.chat_items = []
        # The list loads by itself after signing in; its button, beside it, loads it again.
        chat = QHBoxLayout()
        chat.setSpacing(8)
        chat.addWidget(self.with_help(self.chats, "chat"), 1)
        self.button(chat, "chats", lambda: self.start_task(self.load_chats()), explain=False)
        self.cell(grid, 0, 0, "chat", chat, explain=False, span=3)
        self.busy_controls.append(self.chats)
        self.start_date = QDateTimeEdit(QDateTime(datetime(2013, 1, 1)))
        self.end_date = QDateTimeEdit(QDateTime.currentDateTime())
        for column, (key, widget) in enumerate((("start", self.start_date), ("end", self.end_date))):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            self.cell(grid, 1, 2 * column, key, widget)
        self.media = QComboBox()
        for key in ("all", "no_message_files", "none"):
            self.media.addItem(key, key)
        self.cell(grid, 2, 0, "media", self.media)
        self.file_limit = QSpinBox()
        self.file_limit.setRange(1, 4000)
        self.file_limit.setValue(1000)
        self.cell(grid, 2, 2, "limit", self.file_limit)
        self.anonymize_exporter = theme.Switch()
        self.translated.append((self.anonymize_exporter.setText, "anonymize_exporter"))
        grid.addWidget(self.with_help(self.anonymize_exporter, "anonymize_exporter"), 3, 0, 1, 2)
        self.busy_controls.append(self.anonymize_exporter)
        # What an anonymous export may keep of people, offered only once it is chosen. Off by default, and
        # their "?" says why.
        self.keep_box = QWidget()
        keep = QVBoxLayout(self.keep_box)
        keep.setContentsMargins(0, 0, 0, 0)
        keep.setSpacing(4)
        self.keep_names, self.keep_avatars = theme.Switch(), theme.Switch()
        for key, switch in (("keep_names", self.keep_names), ("keep_avatars", self.keep_avatars)):
            self.translated.append((switch.setText, key))
            self.busy_controls.append(switch)
            keep.addWidget(self.with_help(switch, key))
        grid.addWidget(self.keep_box, 3, 2, 2, 2, Qt.AlignTop)
        # A private chat is titled by the other person, Saved Messages by the exporter: the archive
        # shows who made it whatever the option removes. Said where the option is chosen.
        self.anonymize_private = label(role="warning")
        self.translated.append((self.anonymize_private.setText, "anonymize_private"))
        grid.addWidget(self.anonymize_private, 5, 0, 1, 4)
        self.anonymize_exporter.toggled.connect(self.show_anonymity_limits)
        self.chats.currentIndexChanged.connect(self.show_anonymity_limits)
        self.show_anonymity_limits()
        self.split = theme.Switch()
        self.translated.append((self.split.setText, "split"))
        grid.addWidget(self.with_help(self.split, "split"), 6, 0, 1, 2)
        self.busy_controls.append(self.split)
        self.limit_archive = theme.Switch()
        self.limit_archive.setChecked(True)
        self.translated.append((self.limit_archive.setText, "limit_archive"))
        grid.addWidget(self.with_help(self.limit_archive, "limit_archive"), 6, 2, 1, 2)
        self.busy_controls.append(self.limit_archive)
        self.part_size = QSpinBox()
        self.part_size.setRange(1, 4000)
        self.part_size.setValue(1000)
        self.part_size.setEnabled(False)
        self.split.toggled.connect(self.part_size.setEnabled)
        self.cell(grid, 7, 0, "part_size", self.part_size)
        self.archive_limit = QSpinBox()
        self.archive_limit.setRange(1, 4000)
        self.archive_limit.setValue(SITE_JSON_LIMIT // 1024**2)
        self.limit_archive.toggled.connect(self.archive_limit.setEnabled)
        self.cell(grid, 7, 2, "archive_limit", self.archive_limit)
        folder = QHBoxLayout()
        self.output = QLineEdit(str(Path.home() / "Downloads" / "SaveGram"))
        self.output.setCursorPosition(0)  # The start of the path says where it is.
        self.busy_controls.append(self.output)
        folder.addWidget(self.with_help(self.output, "folder"), 1)
        self.button(folder, "browse", self.choose_folder, explain=False)
        self.cell(grid, 8, 0, "folder", folder, explain=False, span=3)
        actions = QHBoxLayout()
        actions.setSpacing(14)
        self.button(actions, "export", lambda: self.start_task(self.create_export()))
        self.button(actions, "resume", lambda: self.start_task(self.resume_export()))
        actions.setStretch(0, 1)
        grid.addLayout(actions, 9, 0, 1, 4)
        layout.addWidget(self.export_box)
        self.export_box.hide()
        # Telegram's "Data export request", shown when it arrives before an export: what it is, that it
        # comes from this program, and what pressing "Allow" does. The export waits for it, or starts.
        self.takeout_box = theme.Card()
        self.translated.append((self.takeout_box.setTitle, "takeout_title"))
        takeout = QHBoxLayout()
        takeout.setSpacing(18)
        self.takeout_picture = QLabel()
        self.takeout_picture.setObjectName("takeoutPicture")
        takeout.addWidget(self.takeout_picture, 0, Qt.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(12)
        self.takeout_text = self.add_label(words, "takeout_body")
        self.takeout_status = label(role="hint")
        words.addWidget(self.takeout_status)
        words.addStretch()
        self.takeout_skip_button = self.button(words, "takeout_skip", self.skip_takeout, explain=False)
        takeout.addLayout(words, 1)
        self.takeout_box.body.addLayout(takeout)
        self.takeout_box.hide()
        self.takeout_choice, self.takeout_state = None, "takeout_waiting"
        self.settings_folded = False
        layout.addWidget(self.takeout_box)
        # How the current task goes: its progress, what is happening, what can be done with it.
        self.status_box = theme.Card()
        progress = QHBoxLayout()
        progress.setSpacing(10)
        self.progress_text = label()
        self.progress_text.setObjectName("progressText")
        self.progress_text.hide()
        progress.addWidget(self.progress_text, 1)
        # Which speed history is read at while it is: the fast mode, or the usual speed while Telegram
        # waits for "Allow". It turns blue the moment Telegram allows it.
        self.mode_badge = QLabel()
        self.mode_badge.setObjectName("modeBadge")
        self.mode_badge.hide()
        progress.addWidget(self.mode_badge, 0, Qt.AlignRight)
        self.status_box.body.addLayout(progress)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.hide()
        self.status_box.body.addWidget(self.bar)
        self.status = Status(self.show_status)
        self.status_box.body.addWidget(self.status)
        actions = QHBoxLayout()
        self.cancel_button = self.button(actions, "cancel", self.cancel, explain=False)
        self.show_control(self.cancel_button, False)
        self.open_button = self.button(actions, "open", self.open_result, explain=False)
        self.show_control(self.open_button, False)
        actions.addStretch()
        self.status_box.body.addLayout(actions)
        layout.addWidget(self.status_box)
        footer = QHBoxLayout()
        self.button(footer, "site", lambda: QDesktopServices.openUrl(QUrl("https://savegram.tech")), explain=False)
        self.button(footer, "source", lambda: self.open_faq("source"), explain=False)
        footer.addStretch()
        layout.addLayout(footer)
        layout.addStretch()
        self.show_status()
        # A turn of the mouse wheel over a field scrolls the page, as over the rest of it: it never
        # changes a chat, a date or a size by accident.
        for field in self.findChildren((QComboBox, QAbstractSpinBox)):
            field.setFocusPolicy(Qt.StrongFocus)
            field.installEventFilter(self)
        self.language.currentIndexChanged.connect(self.translate)
        self.translate()
        try:
            self.saved = self.account.vault.load()
            if self.saved:
                if self.app_credentials and self.saved["api_id"] != self.app_credentials[0]:
                    raise ValueError("Saved account belongs to another application")
                self.remember.setChecked(True)
                self.show_control(self.forget_button, True)
        except Exception:
            self.vault_failed = True
            self.status.setText(self.t("vault_error"))
            self.show_control(self.forget_button, True)
        self.show_keys()

    @property
    def app_credentials(self):
        """What signing in uses: a person's own API ID and API Hash before the program's."""
        return self.own or (None if self.refused else self.builtin)

    @property
    def asks_keys(self):
        return self.builtin is None or self.own is not None or self.refused

    def show_keys(self, note=None):
        """The API ID and API Hash fields while this copy asks for them, the help's answers about them with
        them, and the note above them: why they are asked for, or `note`, what is wrong with them."""
        if note is None:
            note = ("keys_rejected" if self.refused and not self.own else "keys_missing" if not self.own
                    else "keys_own" if self.builtin is None else "keys_own_builtin")
        self.keys_state = note
        self.keys_note.setText(self.t(note))
        role = "hint" if note.startswith("keys_own") else "warning"
        if self.keys_note.property("role") != role:
            self.keys_note.setProperty("role", role)
            self.keys_note.style().unpolish(self.keys_note)
            self.keys_note.style().polish(self.keys_note)
        self.keys_box.setVisible(self.asks_keys)
        self.faq_window.show_setup(self.asks_keys)
        self.allow_login()

    def allow_login(self):
        # Typed-in keys are checked once "Sign in" is clicked; a saved account that cannot be opened waits
        # until it is forgotten.
        self.login_button.setEnabled(not self.vault_failed and (self.asks_keys or bool(self.app_credentials)))

    def chosen_keys(self):
        """The API ID and API Hash to sign in with. Typed in, they are kept for next time; both fields left
        empty go back to the program's own, if Telegram has not refused them."""
        if not self.asks_keys:
            return self.app_credentials
        typed_id = self.app_id_field.text().strip()
        typed_hash = "".join(self.app_hash_field.text().split()).lower()
        if not typed_id and not typed_hash and self.builtin and not self.refused:
            self.keys.delete()
            self.own = None
            self.show_keys()
            return self.builtin
        if not re.fullmatch(r"[0-9]{1,10}", typed_id):
            raise KeysProblem("keys_invalid")
        chosen = int(typed_id), typed_hash
        try:
            validate_credentials(*chosen)
        except ValueError:
            raise KeysProblem("keys_invalid") from None
        if chosen != self.own:
            self.keys.save(*chosen)
            self.own = chosen
        self.show_keys()
        return chosen

    def t(self, key, **values):
        return tr(key, self.language.currentData(), **values)

    def translate(self):
        for setter, key in self.translated:
            setter(self.t(key))
        for index in range(self.media.count()):
            self.media.setItemText(index, self.t(self.media.itemData(index)))
        self.faq_window.set_language(self.language.currentData())
        if self.chat_items:
            self.fill_chats()  # Saved Messages and deleted accounts are named in the window's language.
        if self.task and not self.task.done() and self.last_progress:
            self.update_progress(self.last_progress)
        self.keys_note.setText(self.t(self.keys_state))
        self.takeout_status.setText(self.t(self.takeout_state))
        if self.settings_folded:
            self.settings_summary.setText(self.settings_text())

    def open_faq(self, topic="top"):
        # clicked(bool) must not be interpreted as a topic.
        if topic == "login" and not self.app_credentials:
            topic = "configuration"
        self.faq_window.open_topic(topic if isinstance(topic, str) else "top")

    def with_help(self, control, key):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        if isinstance(control, QHBoxLayout):
            row.addLayout(control, 1)
        else:
            row.addWidget(control, 1)
            self.help_rows[control] = container
        help_button = theme.HelpButton()
        topic = CONTROL_TOPICS[key]
        help_button.setObjectName(f"help_{key}")
        help_button.clicked.connect(lambda checked=False: self.open_faq(topic))
        # Hovering says what this control does; the click opens the full answer in the FAQ.
        self.translated.append((help_button.setToolTip, "hint_" + key))
        self.translated.append((help_button.setAccessibleName, "help"))
        self.translated.append((help_button.setAccessibleDescription, "hint_" + key))
        self.help_buttons[key] = help_button
        row.addWidget(help_button)
        return container

    def show_anonymity_limits(self, *unused):
        private = isinstance(self.chats.currentData(), (types.InputPeerUser, types.InputPeerSelf))
        self.anonymize_private.setVisible(self.anonymize_exporter.isChecked() and private)
        self.keep_box.setVisible(self.anonymize_exporter.isChecked())

    def show_control(self, control, visible):
        self.help_rows.get(control, control).setVisible(visible)
        control.setVisible(visible)
        self.show_status()

    def show_status(self):
        """The status card is shown only while it has something: a message, a task or a result."""
        if not hasattr(self, "status_box"):
            return
        shown = [bool(self.status.text()), not self.bar.isHidden()]
        shown += [not getattr(self, name).isHidden() for name in ("cancel_button", "open_button") if hasattr(self, name)]
        self.status_box.setVisible(any(shown))

    def add_label(self, layout, key, role=None):
        widget = label(role=role)
        self.translated.append((widget.setText, key))
        layout.addRow(widget) if isinstance(layout, QFormLayout) else layout.addWidget(widget)
        return widget

    def row(self, form, key, widget, explain=True):
        name = label()
        self.translated.append((name.setText, key))
        form.addRow(name, self.with_help(widget, key) if explain else widget)
        if isinstance(widget, QWidget):
            self.busy_controls.append(widget)

    def cell(self, grid, row, column, key, control, explain=True, span=1):
        """A setting in the settings' grid: its name on one line, then the control and its "?" over `span`
        columns."""
        name = label()
        name.setWordWrap(False)
        self.translated.append((name.setText, key))
        grid.addWidget(name, row, column)
        if isinstance(control, QWidget):
            self.busy_controls.append(control)
        if explain:
            control = self.with_help(control, key)
        if isinstance(control, QWidget):
            grid.addWidget(control, row, column + 1, 1, span)
        else:
            grid.addLayout(control, row, column + 1, 1, span)

    def button(self, layout, key, callback, explain=True):
        widget = QPushButton()
        widget.setProperty("kind", BUTTON_KINDS.get(key, "secondary"))
        widget.setCursor(Qt.PointingHandCursor)
        # A single "&" would underline the next letter as a shortcut: "Help _FAQ".
        self.translated.append((lambda text: widget.setText(text.replace("&", "&&")), key))
        widget.clicked.connect(callback)
        explained = explain and key in CONTROL_TOPICS
        if not explained and "hint_" + key in TEXT:
            # A link says on hover what it does, without a "?" beside it.
            self.translated.append((widget.setToolTip, "hint_" + key))
        wrapped = self.with_help(widget, key) if explained else widget
        layout.addRow(wrapped) if isinstance(layout, QFormLayout) else layout.addWidget(wrapped)
        if key not in {"faq", "site", "source", "open", "submit", "cancel", "takeout_skip"}:
            self.busy_controls.append(widget)
        return widget

    def show_takeout(self, shown):
        """Telegram's "Data export request", what it is and what "Allow" does, while the window waits for it."""
        if shown:
            # Scaled for the screen's own pixels: the picture's small text stays sharp at 125 % and 150 %.
            ratio = self.devicePixelRatioF()
            image = QImage.fromData(TAKEOUT_REQUEST).scaledToWidth(round(TAKEOUT_PICTURE_WIDTH * ratio),
                                                                   Qt.SmoothTransformation)
            picture = QPixmap.fromImage(image)
            picture.setDevicePixelRatio(ratio)
            self.takeout_picture.setPixmap(picture)
            self.takeout_status.setText(self.t(self.takeout_state))
        self.takeout_box.setVisible(shown)

    def skip_takeout(self):
        """Start the export now, at the usual speed; it still switches if Telegram allows the fast mode later."""
        if self.takeout_choice is not None and not self.takeout_choice.done():
            self.takeout_choice.set_result(None)

    async def fast_mode(self):
        """Ask Telegram for the fast mode before the export. A sign-in Telegram knows gets it at once. After a
        new one Telegram first sends the person a "Data export request": the window shows it as it arrives,
        with what it is, and starts the export in the fast mode the moment the person presses "Allow", or
        at once at the usual speed."""
        ask = getattr(self.account.telegram, "ask_takeout", None)
        if ask is None:
            return
        self.status.setText(self.t("takeout_checking"))
        if await ask() != "delayed":
            return
        self.takeout_choice = asyncio.get_running_loop().create_future()
        self.takeout_state = "takeout_waiting"
        self.show_takeout(True)
        self.status.clear()
        try:
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(self.takeout_choice), TAKEOUT_WAIT_ASK_SECONDS)
                    return  # Started at the usual speed.
                except asyncio.TimeoutError:
                    pass
                if await ask() == "open":
                    self.takeout_state = "takeout_ready"
                    self.takeout_status.setText(self.t(self.takeout_state))
                    await asyncio.sleep(TAKEOUT_READY_SECONDS)
                    return
        finally:
            self.takeout_choice = None
            self.show_takeout(False)

    def fold_settings(self, folded):
        """While an export runs, its settings fold into one line saying what is exported: its progress and
        Telegram's request get the room, and nothing needs scrolling."""
        self.settings_folded = folded
        if folded:
            self.settings_summary.setText(self.settings_text())
        self.settings_form.setVisible(not folded)
        self.settings_summary.setVisible(folded)

    def settings_text(self):
        parts = [self.chats.currentText(), f"{self.start_date.date().toString('dd.MM.yyyy')} — "
                                           f"{self.end_date.date().toString('dd.MM.yyyy')}", self.media.currentText()]
        if self.anonymize_exporter.isChecked():
            parts.append(self.t("anonymize_exporter"))
        return " · ".join(parts)

    def show_mode(self, mode):
        """The speed history is read at: "fast", "usual" (Telegram waits for "Allow") or None, unsaid."""
        if mode:
            self.mode_badge.setText(self.t("mode_" + mode))
            if self.mode_badge.property("mode") != mode:
                self.mode_badge.setProperty("mode", mode)
                self.mode_badge.style().unpolish(self.mode_badge)
                self.mode_badge.style().polish(self.mode_badge)
        self.mode_badge.setVisible(bool(mode))

    def notice(self, key):
        # Non-modal to avoid nested event loops inside qasync tasks.
        box = QMessageBox(QMessageBox.Information, "SaveGram Exporter", self.t(key), parent=self)
        box.setTextFormat(Qt.PlainText)
        box.setAttribute(Qt.WA_DeleteOnClose)
        box.open()
        self.notice_box = box

    def forget_local(self):
        try:
            self.account.vault.delete()
            self.saved = None
            self.vault_failed = False
            self.show_control(self.forget_button, False)
            self.show_keys()  # A note that the account stood in the way goes with it.
            self.status.setText(self.t("logout_warning"))
        except Exception:
            self.status.setText(self.t("vault_error"))

    def show_password(self, visible):
        for widget in (self.twofa_label, self.password, self.password_button):
            self.show_control(widget, visible)

    async def ask_password(self):
        self.show_password(True)
        self.password.setFocus()
        self.password_future = asyncio.get_running_loop().create_future()
        try:
            return await self.password_future
        finally:
            self.password.clear()
            self.show_password(False)
            self.password_future = None

    def submit_password(self):
        if self.password_future and not self.password_future.done() and self.password.text():
            self.password_future.set_result(self.password.text())
            self.password.clear()

    def show_qr(self, url):
        self.qr.clear()
        self.qr.setVisible(bool(url))
        self.qr_hint.setVisible(bool(url))
        if url:
            self.qr.setPixmap(QPixmap.fromImage(qr_image(url)))

    def start_task(self, coroutine, cancelled="cancelled"):
        if self.task and not self.task.done():
            coroutine.close()
            return
        self.cancel_event = Event()
        self.task = asyncio.create_task(self.run_task(coroutine, cancelled))

    async def run_task(self, coroutine, cancelled="cancelled"):
        """`cancelled`: what the status says if the task is cancelled; a sign-in publishes nothing."""
        self.last_progress = None
        for widget in self.busy_controls:
            widget.setEnabled(False)
        self.show_control(self.cancel_button, True)
        self.cancel_button.setEnabled(True)
        self.bar.setRange(0, 0)
        self.bar.show()
        self.progress_text.hide()
        self.status.setText(self.t("working"))
        try:
            await coroutine
        except (Cancelled, asyncio.CancelledError):
            self.status.setText(self.t(cancelled))
            self.show_saved_progress()
        except CheckpointInUse:
            self.status.setText(self.t("checkpoint_in_use"))
        except ArchiveRejected:
            self.status.setText(self.t("archive_rejected"))
            self.show_saved_progress()
        except SignedOut:
            self.status.setText(self.t("signed_out"))
            self.show_saved_progress()
        except KeysProblem as problem:
            self.status.clear()  # The note above the fields says it, where they are.
            self.show_keys(problem.args[0])
        except AppRejected:
            # Telegram refused the API ID and API Hash: the program's own ones ask for a person's, a
            # person's own ones to be copied again.
            self.status.clear()
            if self.own:
                self.show_keys("keys_refused")
            else:
                self.refused = True
                self.show_keys()
        except Exception as exc:
            # A type name is enough for diagnostics; values may contain secrets.
            self.status.setText(self.t("error", category=type(exc).__name__))
            self.show_saved_progress()
        finally:
            self.show_qr(None)
            self.show_password(False)
            self.bar.hide()
            self.progress_text.hide()
            self.show_mode(None)
            self.show_control(self.cancel_button, False)
            for widget in self.busy_controls:
                widget.setEnabled(True)
            self.part_size.setEnabled(self.split.isChecked())
            self.archive_limit.setEnabled(self.limit_archive.isChecked())
            self.allow_login()

    async def login(self):
        api_id, api_hash = self.chosen_keys()
        saved = self.saved
        same = saved and saved["api_id"] == api_id
        if saved and not same:
            # Its sign-in was made with other keys: forgotten first, it is not left behind unnoticed.
            raise KeysProblem("keys_account")
        try:
            authorized = await self.account.open(api_id, api_hash, self.remember.isChecked(),
                                                 saved["session"] if same else "")
            if not authorized:
                self.account.vault.delete()
                await self.account.telegram.login_qr(self.show_qr, self.ask_password)
            me = await self.account.telegram.identity()
            if me is None:
                raise ValueError("Account unavailable")
            self.account.save()
        except BaseException as error:
            if self.account.telegram:
                # Telegram refusing the app itself signed nobody in: there is no session to end.
                refused = isinstance(error, AppRejected)
                if not await self.account.abandon(signed_in=not refused) and not refused:
                    self.needs_session_check = True
                    self.notice("logout_warning")
            self.saved = None
            raise
        self.saved = None
        self.ready = True
        name = " ".join(filter(None, (me.first_name, me.last_name)))
        self.identity.setText(name)
        self.avatar.show_person(name, getattr(me, "id", 0))
        # The account's own photo, as Telegram shows it; the initials stay if there is none.
        self.photo_task = asyncio.create_task(self.show_profile_photo(me))
        self.auth_box.hide()
        self.profile.show()
        self.show_control(self.logout_button, True)
        self.export_box.show()
        await self.load_chats()

    async def show_profile_photo(self, me):
        try:
            data = await self.account.telegram.profile_photo(me)
        except Exception:
            return  # Only the picture is missing: the initials stay.
        image = QImage.fromData(data) if data else QImage()
        if self.ready and not image.isNull():
            self.avatar.set_photo(image)

    async def load_chats(self):
        self.chat_items = [(item[0], item[1], item[2] if len(item) > 2 else {})
                           for item in await self.account.telegram.dialogs()]
        fetch = getattr(self.account.telegram, "chat_photo", None)
        if fetch != self.chat_photos.fetch:  # Loaded again for the same account, the photos stay.
            self.chat_photos.reset(fetch)
        self.chat_faces = {details["key"]: (details.get("kind", "user"), name.strip() or details.get("username", ""),
                                            details.get("id", 0), bool(details.get("photo")))
                           for name, _, details in self.chat_items if details.get("key") is not None}
        self.fill_chats()
        # The rows the list opens with: their photos are there by the time it is.
        for _, _, details in self.chat_items[:self.chats.maxVisibleItems()]:
            if details.get("photo"):
                self.chat_photos.want(details["key"])
        self.status.clear()

    def chat_label(self, name, details):
        kind = details.get("kind")
        if kind == "self":
            return self.t("saved_messages")
        if kind == "deleted":
            return self.t("deleted_account")  # Telegram clears a deleted account's name: it was an empty row.
        if name.strip():
            return name
        return "@" + details["username"] if details.get("username") else f"ID {details.get('id', '?')}"

    def fill_chats(self):
        """The chats under names a person can read, in the window's language; the choice stays."""
        chosen = self.chats.currentData()
        labels = [self.chat_label(name, details) for name, _, details in self.chat_items]
        repeated = {label for label in labels if labels.count(label) > 1}
        self.chats.blockSignals(True)
        self.chats.clear()
        for label, (_, peer, details) in zip(labels, self.chat_items):
            if label in repeated:  # Two chats of one name: the username or the ID tells them apart.
                label += f" · @{details['username']}" if details.get("username") else f" · ID {details.get('id', '?')}"
            row = QStandardItem(label)
            row.setData(peer, Qt.UserRole)
            row.setData(details.get("key"), CHAT_KEY)
            self.chats.model().appendRow(row)
        self.chats.blockSignals(False)
        index = next((i for i, (_, peer, _) in enumerate(self.chat_items) if peer == chosen), 0)
        self.chats.setCurrentIndex(index if self.chats.count() else -1)
        self.show_anonymity_limits()

    def chat_picture(self, index, side):
        """A row's round picture, `side` pixels across; a chat's photo is asked for once its row shows."""
        key = index.data(CHAT_KEY)
        kind, name, color_id, has_photo = self.chat_faces.get(key, ("user", index.data() or "", 0, False))
        photo = self.chat_photo_images.get(key)
        if has_photo and photo is None:
            self.chat_photos.want(key)
        drawn = (key, kind, name, color_id, photo is not None, side)
        if drawn not in self.chat_pictures:
            self.chat_pictures[drawn] = theme.face(side, name, color_id, kind, photo)
        return self.chat_pictures[drawn]

    def chat_photo_arrived(self, key, data):
        image = QImage.fromData(data)
        if image.isNull():
            return
        # Kept small: a few hundred chats' photos as they come would take tens of megabytes.
        self.chat_photo_images[key] = image.scaled(96, 96, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        for view in (self.chats.view(), self.chats.completer().popup()):
            view.viewport().update()

    def settle_chat_text(self):
        """Typed text that names no chat gives way to the chat chosen: an export is always of a real one."""
        index = self.chats.findText(self.chats.lineEdit().text(), Qt.MatchFixedString)
        if index >= 0:
            self.chats.setCurrentIndex(index)
        self.chats.setEditText(self.chats.itemText(self.chats.currentIndex()) if self.chats.count() else "")

    def choose_theme(self, mode):
        theme.set_mode(QApplication.instance(), mode)
        self.theme_button.show_mode(mode)
        self.theme_actions[mode].setChecked(True)
        self.settings.setValue("theme", mode)

    async def logout(self):
        await self.chat_photos.stop()
        self.chat_faces, self.chat_photo_images, self.chat_pictures = {}, {}, {}
        try:
            confirmed = await self.account.close(forget=True)
        except BaseException:
            self.needs_session_check = True
            self.notice("logout_warning")
            raise
        finally:
            self.ready = False
            self.identity.clear()
            self.avatar.show_person("", 0)
            self.chats.clear()
            self.auth_box.show()
            self.profile.hide()
            self.export_box.hide()
            self.show_control(self.logout_button, False)
            self.show_control(self.forget_button, False)
        if not confirmed:
            self.needs_session_check = True
            self.notice("logout_warning")
        self.status.clear()

    def choose_folder(self):
        directory = QFileDialog.getExistingDirectory(self, self.t("folder"), self.output.text())
        if directory:
            self.output.setText(directory)
            self.output.setCursorPosition(0)

    def update_progress(self, progress):
        self.last_progress = progress
        percent = progress.percent
        if percent is None:
            self.bar.setRange(0, 0)
            self.progress_text.hide()
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(percent)
            self.bar.setFormat(self.t("history_progress") + " %p%")
            # The bar is a thin line, as in Telegram: the percentage is written above it.
            self.progress_text.setText(self.bar.text())
            self.progress_text.show()
        # Every page in, the messages wait for their files: said so, not "reading" for half an hour.
        key = "files_phase" if progress.history_read and progress.phase == "messages" else progress.phase
        phase = self.t(key, seconds=progress.wait_seconds,
                       attempt=progress.retry_attempt, category=progress.error_category)
        self.show_mode("fast" if "takeout_on" in progress.notes else
                       "usual" if "takeout_delayed" in progress.notes else None)
        lines = [self.t("progress", phase=phase, messages=progress.messages, skipped=progress.skipped)]
        if progress.download_speed is not None:
            lines.append(self.t("download_speed", speed=self.speed_text(progress.download_speed)))
        self.status.setText("\n".join(lines + [self.t(key) for key in progress.notes]))

    def speed_text(self, speed):
        """MB a second, or KB below a thousand of them, as Windows counts them (1 MB = 1,048,576 bytes):
        never "1024 KB/s" just below a megabyte."""
        if speed < 1000 * 1024:
            return self.t("speed_kb", value=round(speed / 1024))
        value = f"{speed / 1024**2:.1f}"
        return self.t("speed_mb", value=value if self.language.currentData() == "en" else value.replace(".", ","))

    def show_saved_progress(self):
        progress = self.last_progress
        if progress and progress.checkpoint and (progress.checkpoint / "checkpoint.sqlite").is_file():
            self.status.setText(self.status.text() + "\n" + self.t("progress_saved", path=str(progress.checkpoint)))

    async def resume_export(self):
        path, _ = QFileDialog.getOpenFileName(self, self.t("resume"), self.output.text(), "Checkpoint (checkpoint.sqlite)")
        if not path:
            return
        path = Path(path)
        saved = read_options(path)
        index = next((i for i in range(self.chats.count())
                      if peer_id(self.chats.itemData(i)) == saved["peer_id"]), None)
        if index is None:
            raise ValueError("Load the checkpoint's chat using the original account")
        self.chats.setCurrentIndex(index)
        self.start_date.setDateTime(QDateTime(datetime.fromisoformat(saved["start"]).astimezone().replace(tzinfo=None)))
        self.end_date.setDateTime(QDateTime(datetime.fromisoformat(saved["end"]).astimezone().replace(tzinfo=None)))
        self.media.setCurrentIndex(self.media.findData(saved["media_mode"]))
        self.file_limit.setValue(saved["media_limit"] // 1024**2)
        self.split.setChecked(saved["part_size"] is not None)
        if saved["part_size"]:
            self.part_size.setValue(saved["part_size"] // 1024**2)
        self.anonymize_exporter.setChecked(saved["anonymize_exporter"])
        self.keep_names.setChecked(saved.get("keep_names", False))
        self.keep_avatars.setChecked(saved.get("keep_avatars", False))
        limit = saved.get("archive_limit", SITE_JSON_LIMIT)
        self.limit_archive.setChecked(limit is not None)
        if limit:
            self.archive_limit.setValue(max(1, limit // 1024**2))
        self.output.setText(str(path.parent.parent))
        self.resume_from = path.parent
        try:
            await self.create_export()
        finally:
            self.resume_from = None

    async def create_export(self):
        self.settle_chat_text()
        if self.chats.currentData() is None or not self.output.text().strip():
            raise ValueError("Choose a chat and output directory")
        options = ExportOptions(self.chats.currentData(), Path(self.output.text()),
            self.start_date.dateTime().toPyDateTime().astimezone(timezone.utc),
            self.end_date.dateTime().toPyDateTime().astimezone(timezone.utc),
            self.media.currentData(), self.file_limit.value() * 1024**2,
            self.part_size.value() * 1024**2 if self.split.isChecked() else None,
            anonymize_exporter=self.anonymize_exporter.isChecked(), resume_from=self.resume_from,
            archive_limit=self.archive_limit.value() * 1024**2 if self.limit_archive.isChecked() else None,
            keep_names=self.anonymize_exporter.isChecked() and self.keep_names.isChecked(),
            keep_avatars=self.anonymize_exporter.isChecked() and self.keep_avatars.isChecked())
        self.show_control(self.open_button, False)
        self.fold_settings(True)
        try:
            await self.fast_mode()
            # The chat list's photos wait meanwhile: the export downloads through the same account.
            with self.chat_photos.held():
                self.result = await export(self.account.telegram, options, self.update_progress, self.cancel_event)
        finally:
            self.fold_settings(False)
        lines = [self.t("partial" if self.result.partial else "done", count=self.result.messages)]
        lines += [f"{self.t(key)}: {count}" for key, count in self.result.omissions.items()]
        lines += [self.t(key) for key in self.result.warnings]
        lines += [str(self.result.directory)]
        self.status.setText("\n".join(lines))
        self.show_control(self.open_button, True)

    def open_result(self):
        if self.result:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result.directory)))

    def cancel(self):
        self.cancel_event.set()
        if self.task and not self.task.done() and not self.task.cancelling():
            self.task.cancel()
        self.cancel_button.setEnabled(False)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and isinstance(watched, (QComboBox, QAbstractSpinBox)):
            QApplication.sendEvent(self.centralWidget().verticalScrollBar(), event)
            return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        # Windows switched between light and dark: the help's HTML carries colours of its own.
        if event.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange) and hasattr(self, "language"):
            self.faq_window.set_language(self.language.currentData())
        super().changeEvent(event)

    def closeEvent(self, event):
        if self.allow_close:
            event.accept()
            return
        event.ignore()
        if not self.closing:
            self.closing = True
            self.close_task = asyncio.create_task(self.shutdown())

    async def shutdown(self):
        self.cancel()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)
        await self.chat_photos.stop()
        self.centralWidget().setEnabled(False)
        self.status.setText(self.t("closing"))
        warning = self.needs_session_check
        try:
            if self.account.telegram:
                warning = not await self.account.close() or warning
        except Exception:
            warning = True
        self.allow_close = True
        if warning:
            self.notice("logout_warning")
            self.notice_box.finished.connect(self.close)
        else:
            self.close()


def main():
    if sys.platform != "win32":
        raise SystemExit("SaveGram Exporter supports Windows 10/11 x64.")
    # Libraries cannot write sensitive debug messages to inherited log handlers.
    logging.disable(logging.CRITICAL)
    # Sharp at 125-200 % display scaling instead of tiny or blurred.
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    # The title bar turns dark with Windows' dark mode, as the window below it does.
    os.environ.setdefault("QT_QPA_PLATFORM", "windows:darkmode=1")
    app = QApplication(sys.argv)
    app.setApplicationName("SaveGram Exporter")
    theme.apply(app)
    app.theme_timer = theme.follow_system(app)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = Window()
    window.show()
    with loop:
        loop.run_forever()
