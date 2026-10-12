"""The window's look: Telegram's light or night theme, as Windows is set, with SaveGram's mark.

Drawn in code. Qt style sheets take images only from files: the arrows are drawn into a temporary
folder that is removed when the app exits.
"""
import atexit
from pathlib import Path
import shutil
import tempfile

from PyQt5.QtCore import QByteArray, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PyQt5.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (QCheckBox, QFrame, QLabel, QPushButton, QStyle, QStyledItemDelegate,
                             QStyleOptionViewItem, QToolButton, QVBoxLayout, QWidget)

# Telegram Web's light theme. "accent" is for links, titles, focus and switches; "primary" fills
# the one main button of a screen.
LIGHT = dict(
    accent="#3390EC", primary="#3390EC", primary_hover="#2C84DC", primary_pressed="#2676C7",
    primary_off="#A9CDF5", on_primary="#FFFFFF", on_primary_off="#FFFFFF",
    accent_soft="#EAF3FD", accent_soft_hover="#DCEBFB", accent_soft_pressed="#CEE3FA",
    page="#F1F2F4", card="#FFFFFF", field="#FFFFFF", text="#000000", secondary="#707579", faq_text="#222222",
    border="#DADCE0", border_hover="#B8BEC4", border_off="#E6E8EA", disabled="#A8B0B7", disabled_bg="#F4F5F6",
    danger="#E53935", danger_soft="#FDECEC", warning="#8A5300", warning_bg="#FFF4DC",
    switch_off="#C4C9CE", scroll="#C4C9CE", scroll_hover="#A8B0B7", track="#E3E7EB", tooltip="#FFFFFF",
)
# Telegram's night theme: the viewer's dark colours. White text on the main button needs a deeper
# blue than the light accent the links use.
DARK = dict(
    accent="#6AB3F3", primary="#2F6EA5", primary_hover="#3A7CB4", primary_pressed="#285F8E",
    primary_off="#243B52", on_primary="#FFFFFF", on_primary_off="#8FA3B7",
    accent_soft="#1E3246", accent_soft_hover="#253C54", accent_soft_pressed="#2C4662",
    page="#0E1621", card="#17212B", field="#0E1621", text="#F1F3F5", secondary="#93A3B2", faq_text="#E3E7EA",
    border="#2B3B4C", border_hover="#3E5165", border_off="#1F2B37", disabled="#5D6D7C", disabled_bg="#141D26",
    danger="#F2676C", danger_soft="#3A2227", warning="#F0C674", warning_bg="#2F2A1C",
    switch_off="#3D4B59", scroll="#34495C", scroll_hover="#46607A", track="#233242", tooltip="#232E3C",
)
# The theme in use: apply() fills it; the switches and the help read it as they draw.
COLORS = dict(LIGHT)
# Telegram's colours for someone without a photo, by ID: the same as the viewer's.
PEER_COLORS = ("#E17076", "#FAA774", "#A695E7", "#7BC862", "#6EC9CB", "#65AADD", "#EE7AAE")
# Saved Messages is a bookmark on Telegram's blue, a deleted account a ghost on grey.
SAVED_COLOR = PEER_COLORS[5]
DELETED_COLOR = "#9AA6B2"
SHAPES = {
    "self": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" d="M6.5 2.5h11a1.5 1.5 0 '
            '0 1 1.5 1.5v17.2a.6.6 0 0 1-.95.49L12 17.8l-6.05 3.89a.6.6 0 0 1-.95-.49V4a1.5 1.5 0 0 1 1.5-1.5Z"/></svg>',
    # The viewer's ghost (savegram/processing/static/app.js).
    "deleted": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="#fff" fill-rule="evenodd" '
               'd="M5 10a7 7 0 0 1 14 0v9a2.33 2.33 0 0 1-4.67 0 2.33 2.33 0 0 1-4.66 0A2.33 2.33 0 0 1 5 19ZM9.5 9a1.25 '
               '1.25 0 1 0 0 2.5 1.25 1.25 0 0 0 0-2.5Zm5 0a1.25 1.25 0 1 0 0 2.5 1.25 1.25 0 0 0 0-2.5Z"/></svg>',
}
# The chat list's pictures, across.
CHAT_PHOTO = 30
FONT = "Segoe UI"

# savegram.tech's mark (web/public/mark.svg).
MARK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 310 310"><circle cx="155" cy="155" r="153" fill="#10a5ed"/>'
    '<g fill="#fff"><path d="M21.88,165.74 L45.45,164.27 C45.96,168.09 47,171.01 48.57,173.02 C51.12,176.26 54.77,177.88 '
    '59.51,177.88 C63.04,177.88 65.77,177.05 67.68,175.39 C69.6,173.74 70.55,171.81 70.55,169.63 C70.55,167.55 69.64,'
    '165.69 67.82,164.05 C66,162.41 61.77,160.86 55.13,159.4 C44.27,156.96 36.52,153.71 31.89,149.66 C27.22,145.62 '
    '24.89,140.46 24.89,134.19 C24.89,130.07 26.08,126.18 28.47,122.51 C30.86,118.85 34.45,115.97 39.25,113.87 C44.04,'
    '111.77 50.61,110.73 58.96,110.73 C69.21,110.73 77.02,112.63 82.39,116.44 C87.77,120.25 90.97,126.31 91.99,134.63 '
    'L68.64,135.99 C68.02,132.38 66.72,129.76 64.73,128.12 C62.74,126.48 60,125.66 56.5,125.66 C53.62,125.66 51.45,'
    '126.27 49.99,127.49 C48.53,128.71 47.8,130.2 47.8,131.95 C47.8,133.22 48.41,134.37 49.61,135.39 C50.78,136.45 '
    '53.55,137.43 57.92,138.34 C68.75,140.68 76.51,143.04 81.19,145.43 C85.88,147.81 89.29,150.78 91.42,154.31 C93.55,'
    '157.85 94.62,161.8 94.62,166.18 C94.62,171.32 93.2,176.06 90.35,180.4 C87.51,184.74 83.53,188.03 78.43,190.27 '
    'C73.33,192.51 66.89,193.63 59.13,193.63 C45.49,193.63 36.05,191.01 30.8,185.76 C25.55,180.51 22.58,173.84 21.88,'
    '165.74 Z"/><path d="M91 151 157 125Q166 121 163 132L143 191Q140 200 133 194L114 178Q110 173 115 169L143 143Q147 '
    '138 141 141L111 159Q106 163 100 160L92 157Q87 154 91 151Z"/><path d="M159.15,136.11 L180.86,136.11 L191.43,170.62 '
    'L202.39,136.11 L223.45,136.11 L200.3,190.56 L181.8,190.56 Z M287.17,168.52 L245.34,168.52 C245.71,171.87 246.62,'
    '174.36 248.06,176 C250.07,178.36 252.7,179.54 255.95,179.54 C258,179.54 259.95,179.03 261.8,178 C262.92,177.35 '
    '264.14,176.21 265.44,174.57 L285.99,176.46 C282.85,181.93 279.06,185.85 274.61,188.23 C270.17,190.6 263.79,191.79 '
    '255.49,191.79 C248.28,191.79 242.6,190.78 238.47,188.74 C234.33,186.71 230.91,183.48 228.19,179.05 C225.47,174.63 '
    '224.11,169.42 224.11,163.44 C224.11,154.93 226.84,148.04 232.29,142.78 C237.74,137.51 245.27,134.88 254.87,134.88 '
    'C262.67,134.88 268.82,136.06 273.33,138.42 C277.84,140.78 281.28,144.2 283.64,148.67 C285.99,153.15 287.17,158.98 '
    '287.17,166.16 Z M265.95,158.52 C265.54,154.48 264.45,151.6 262.69,149.85 C260.93,148.11 258.62,147.24 255.75,'
    '147.24 C252.43,147.24 249.78,148.55 247.8,151.19 C246.53,152.83 245.73,155.27 245.39,158.52 Z"/></g></svg>')

STYLE = """
QWidget { font-family: "%(font)s"; font-size: 10pt; color: %(text)s; }
QMainWindow, QScrollArea, QWidget#page { background: %(page)s; }
QScrollArea { border: none; }
QFrame#card { background: %(card)s; border: none; border-radius: 14px; }
QLabel#cardTitle { color: %(accent)s; font-size: 10.5pt; font-weight: 600; }
QLabel#appTitle { font-size: 15pt; font-weight: 600; }
QLabel#appSubtitle, QLabel[role="hint"] { color: %(secondary)s; font-size: 9pt; }
QLabel[role="warning"] { color: %(warning)s; background: %(warning_bg)s; border-radius: 10px; padding: 8px 10px;
    font-size: 9pt; }
QLabel#profileName { font-size: 11pt; font-weight: 600; }
QLabel#status { color: %(text)s; }
QLabel#progressText { color: %(secondary)s; font-size: 9pt; }
QLabel#modeBadge { border-radius: 10px; padding: 2px 10px; font-size: 9pt; font-weight: 600; }
QLabel#modeBadge[mode="fast"] { background: %(accent_soft)s; color: %(accent)s; }
QLabel#modeBadge[mode="usual"] { background: %(page)s; color: %(secondary)s; }
QLabel#modeBadge[mode="off"] { background: %(page)s; color: %(secondary)s; text-decoration: line-through; }
QLabel#qr { background: white; border: 1px solid %(border)s; border-radius: 14px; padding: 10px; }

QPushButton { border: none; border-radius: 10px; padding: 8px 16px; background: %(accent_soft)s; color: %(accent)s;
    font-weight: 600; }
QPushButton:hover { background: %(accent_soft_hover)s; }
QPushButton:pressed { background: %(accent_soft_pressed)s; }
QPushButton:disabled { background: %(disabled_bg)s; color: %(disabled)s; }
QPushButton[kind="primary"] { background: %(primary)s; color: %(on_primary)s; padding: 10px 18px; font-size: 10.5pt; }
QPushButton[kind="primary"]:hover { background: %(primary_hover)s; }
QPushButton[kind="primary"]:pressed { background: %(primary_pressed)s; }
QPushButton[kind="primary"]:disabled { background: %(primary_off)s; color: %(on_primary_off)s; }
QPushButton[kind="danger"] { background: transparent; color: %(danger)s; }
QPushButton[kind="danger"]:hover { background: %(danger_soft)s; }
QPushButton[kind="danger"]:disabled { background: transparent; color: %(disabled)s; }
QPushButton[kind="link"] { background: transparent; color: %(accent)s; padding: 6px 10px; font-weight: 500; }
QPushButton[kind="link"]:hover { background: %(accent_soft)s; }
QPushButton[kind="link"]:disabled { background: transparent; color: %(disabled)s; }
QPushButton[kind="help"] { background: %(page)s; color: %(secondary)s; border-radius: 11px; padding: 0;
    font-size: 9pt; font-weight: 700; }
QPushButton[kind="help"]:hover { background: %(accent_soft)s; color: %(accent)s; }

QLineEdit, QComboBox, QSpinBox, QDateTimeEdit { background: %(field)s; border: 1px solid %(border)s;
    border-radius: 10px; padding: 6px 10px; min-height: 22px; selection-background-color: %(primary)s;
    selection-color: %(on_primary)s; }
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDateTimeEdit:hover { border-color: %(border_hover)s; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateTimeEdit:focus { border: 2px solid %(accent)s;
    padding: 5px 9px; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDateTimeEdit:disabled { background: %(disabled_bg)s;
    color: %(disabled)s; border-color: %(border_off)s; }
QComboBox { combobox-popup: 0; }
QComboBox QLineEdit { border: none; background: transparent; padding: 0; min-height: 0; }
QComboBox QAbstractItemView::item { min-height: 24px; padding: 2px 6px; border-radius: 6px; }
QListView#chatSearch { background: %(card)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 10px;
    outline: 0; }
QListView#chatSearch::item { min-height: 26px; padding: 2px 8px; border-radius: 6px; }
QListView#chatSearch::item:selected, QListView#chatSearch::item:hover { background: %(accent_soft)s; color: %(text)s; }
QComboBox::drop-down, QDateTimeEdit::drop-down { border: none; width: 30px; }
QComboBox::down-arrow, QDateTimeEdit::down-arrow { image: url(%(arrows)s/down.png); width: 12px; height: 12px; }
QComboBox::down-arrow:disabled, QDateTimeEdit::down-arrow:disabled { image: url(%(arrows)s/down-off.png); }
QSpinBox { padding-right: 30px; }
QSpinBox::up-button, QSpinBox::down-button { border: none; background: transparent; width: 26px; }
QSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; margin: 4px 4px 0 0; }
QSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; margin: 0 4px 4px 0; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: %(accent_soft)s; border-radius: 6px; }
QSpinBox::up-arrow { image: url(%(arrows)s/up.png); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url(%(arrows)s/down.png); width: 10px; height: 10px; }
QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off { image: url(%(arrows)s/up-off.png); }
QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off { image: url(%(arrows)s/down-off.png); }
QComboBox QAbstractItemView { background: %(card)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 10px; padding: 4px;
    outline: 0; selection-background-color: %(accent_soft)s; selection-color: %(text)s; }
QCalendarWidget QWidget { alternate-background-color: %(page)s; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: %(card)s; }
QCalendarWidget QAbstractItemView { background: %(field)s; color: %(text)s; }
QCalendarWidget QToolButton { color: %(text)s; background: transparent; border-radius: 8px; padding: 4px 8px; }
QCalendarWidget QToolButton:hover { background: %(accent_soft)s; }
QCalendarWidget QAbstractItemView:enabled { selection-background-color: %(primary)s; selection-color: %(on_primary)s; }

QProgressBar { background: %(track)s; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: %(accent)s; border-radius: 3px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 2px 4px 0; }
QScrollBar::handle:vertical { background: %(scroll)s; border-radius: 4px; min-height: 40px; }
QScrollBar::handle:vertical:hover { background: %(scroll_hover)s; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 4px 2px 4px; }
QScrollBar::handle:horizontal { background: %(scroll)s; border-radius: 4px; min-width: 40px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QToolTip { background: %(tooltip)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 8px; padding: 6px 8px; }
QTextBrowser { background: %(card)s; color: %(text)s; border: none; border-radius: 14px; padding: 4px 6px; }
QDialog, QMessageBox { background: %(page)s; }
QMenu { background: %(card)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 10px; padding: 6px; }
QMenu::item { padding: 6px 18px 6px 28px; border-radius: 6px; }
QMenu::item:selected { background: %(accent_soft)s; color: %(accent)s; }
QMessageBox QLabel { color: %(text)s; }
"""

# The FAQ's HTML, as close to the window as Qt's rich text allows.
FAQ_CSS = """
h1 { font-size: 16pt; font-weight: 600; margin-bottom: 8px; color: %(text)s; }
h2 { font-size: 12pt; font-weight: 600; margin-top: 16px; margin-bottom: 4px; color: %(text)s; }
p { color: %(faq_text)s; }
a { color: %(accent)s; text-decoration: none; }
li { margin-bottom: 3px; color: %(faq_text)s; }
"""
_ARROWS = {}


def faq_css():
    """The help's style in the theme in use."""
    return FAQ_CSS % COLORS


def system_dark():
    """Whether Windows asks apps for their dark mode: Settings → Personalization → Colors."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except (ImportError, OSError):
        return False


def arrows():
    """Chevrons for drop-downs and number fields in the theme's greys, in a folder of their own; its path."""
    key = (COLORS["secondary"], COLORS["disabled"])
    if key in _ARROWS:
        return _ARROWS[key]
    folder = Path(tempfile.mkdtemp(prefix="savegram-exporter-ui-"))
    atexit.register(shutil.rmtree, folder, True)
    for name, direction, color in (("down", 1, COLORS["secondary"]), ("up", -1, COLORS["secondary"]),
                                   ("down-off", 1, COLORS["disabled"]), ("up-off", -1, COLORS["disabled"])):
        for suffix, scale in (("", 1), ("@2x", 2)):
            pixmap = QPixmap(12 * scale, 12 * scale)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.scale(scale, scale)
            painter.setPen(QPen(QColor(color), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            rise = 2.2 * direction
            painter.drawPolyline(QPointF(2, 6 - rise), QPointF(6, 6 + rise), QPointF(10, 6 - rise))
            painter.end()
            pixmap.save(str(folder / f"{name}{suffix}.png"))
    _ARROWS[key] = folder.as_posix()
    return _ARROWS[key]


def apply(app, dark=None):
    """Give the whole application the look: light or night as Windows is set, unless `dark` says."""
    dark = system_dark() if dark is None else bool(dark)
    COLORS.clear()
    COLORS.update(DARK if dark else LIGHT)
    app.setProperty("savegram_dark", dark)
    if app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")
    palette = QPalette()
    for role, color in ((QPalette.Window, COLORS["page"]), (QPalette.Base, COLORS["field"]),
                        (QPalette.AlternateBase, COLORS["page"]), (QPalette.Button, COLORS["accent_soft"]),
                        (QPalette.Text, COLORS["text"]), (QPalette.WindowText, COLORS["text"]),
                        (QPalette.ButtonText, COLORS["accent"]), (QPalette.Highlight, COLORS["primary"]),
                        (QPalette.HighlightedText, COLORS["on_primary"]), (QPalette.Link, COLORS["accent"]),
                        (QPalette.ToolTipBase, COLORS["tooltip"]), (QPalette.ToolTipText, COLORS["text"]),
                        (QPalette.PlaceholderText, COLORS["disabled"])):
        palette.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(COLORS["disabled"]))
    app.setPalette(palette)
    app.setFont(QFont(FONT, 10))
    app.setStyleSheet(STYLE % dict(COLORS, font=FONT, arrows=arrows()))
    app.setWindowIcon(mark_icon())


MODES = ("system", "light", "dark")


def set_mode(app, mode):
    """Light, night, or as Windows is set ("system"): the theme button's choice."""
    mode = mode if mode in MODES else "system"
    app.setProperty("savegram_theme_mode", mode)
    apply(app, None if mode == "system" else mode == "dark")


def follow_system(app, interval=2000):
    """Switch between light and night as Windows does while the app runs, unless one was chosen."""
    timer = QTimer(app)
    timer.timeout.connect(lambda: app.property("savegram_theme_mode") in (None, "system")
                          and system_dark() != app.property("savegram_dark") and apply(app))
    timer.start(interval)
    return timer


def mark_pixmap(size, ratio=1.0):
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(MARK_SVG.encode())).render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def mark_icon():
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(mark_pixmap(size))
    return icon


# Pictures drawn once and shrunk by Qt at 125 % display scaling came out with stepped edges: these
# draw themselves at the screen's own resolution, antialiased, whatever the scaling.


class Mark(QWidget):
    """SaveGram's mark, drawn from its vector shape."""

    def __init__(self, size=44):
        super().__init__()
        self.setFixedSize(size, size)
        self.renderer = QSvgRenderer(QByteArray(MARK_SVG.encode()), self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self.renderer.render(painter, QRectF(self.rect()))
        painter.end()


def initials(name):
    """What Telegram writes in a circle without a photo: the first letters of the first two words."""
    words = [word for word in name.split() if word[:1].isalnum()]
    return "".join(word[0] for word in words[:2]).upper() or name.strip()[:1].upper() or "?"


def face(side, name="", color_id=0, kind="user", photo=None):
    """Someone's or a chat's round picture, `side` pixels across: the photo if there is one, else what
    Telegram draws instead — initials on the colour of the ID, a bookmark for Saved Messages, a ghost
    for a deleted account.

    Drawn at the screen's own pixels, and cut round there: a clipped drawing leaves the edge stepped.
    """
    canvas = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.setPen(Qt.NoPen)
    circle = QRectF(0, 0, side, side)
    if photo is not None and not photo.isNull():
        painter.setBrush(QColor("white"))
        painter.drawEllipse(circle)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        picture = photo.scaled(side, side, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        painter.drawImage(QRect(0, 0, side, side), picture,
                          QRect((picture.width() - side) // 2, (picture.height() - side) // 2, side, side))
    else:
        painter.setBrush(QColor(SAVED_COLOR if kind == "self" else DELETED_COLOR if kind == "deleted"
                                else PEER_COLORS[abs(int(color_id or 0)) % len(PEER_COLORS)]))
        painter.drawEllipse(circle)
        if kind in SHAPES:
            inset = side * 0.21
            QSvgRenderer(QByteArray(SHAPES[kind].encode())).render(painter, circle.adjusted(inset, inset, -inset, -inset))
        else:
            font = QFont(FONT)
            font.setPixelSize(max(1, round(side * 0.38)))
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(circle, Qt.AlignCenter, initials(name))
    painter.end()
    return canvas


class Avatar(QWidget):
    """A person in a circle: their photo, or their initials on the colour Telegram gives them."""

    def __init__(self, size=44):
        super().__init__()
        self.setFixedSize(size, size)
        self.name, self.user_id, self.photo = "", 0, QImage()

    def show_person(self, name, user_id):
        self.name, self.user_id, self.photo = name, user_id, QImage()
        self.update()

    def set_photo(self, image):
        self.photo = image
        self.update()

    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        picture = face(max(1, round(self.width() * ratio)), self.name, self.user_id, photo=self.photo)
        picture.setDevicePixelRatio(ratio)
        painter = QPainter(self)
        painter.drawImage(QPointF(0, 0), picture)
        painter.end()


class ChatRow(QStyledItemDelegate):
    """A chat in the list, as Telegram draws one: its round picture, then its name.

    `picture(index, side)` gives the chat's picture `side` pixels across, drawn by face().
    """

    def __init__(self, picture, parent=None):
        super().__init__(parent)
        self.picture = picture

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width() + CHAT_PHOTO + 16, max(size.height(), CHAT_PHOTO + 8))

    def paint(self, painter, option, index):
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        area = QRectF(item.rect).adjusted(2, 1, -2, -1)
        if item.state & (QStyle.State_Selected | QStyle.State_MouseOver):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLORS["accent_soft"]))
            painter.drawRoundedRect(area, 8, 8)
        ratio = painter.device().devicePixelRatioF()
        spot = QRectF(area.left() + 6, round(area.center().y() - CHAT_PHOTO / 2), CHAT_PHOTO, CHAT_PHOTO)
        picture = self.picture(index, max(1, round(CHAT_PHOTO * ratio)))
        if picture is not None:
            picture.setDevicePixelRatio(ratio)
            painter.drawImage(spot.topLeft(), picture)  # At its own size: one of its pixels to one of the screen's.
        text = QRectF(spot.right() + 10, area.top(), area.right() - spot.right() - 16, area.height())
        painter.setFont(item.font)
        painter.setPen(QColor(COLORS["text"]))
        painter.drawText(text, Qt.AlignLeft | Qt.AlignVCenter,
                         item.fontMetrics.elidedText(item.text, Qt.ElideRight, max(0, int(text.width()))))
        painter.restore()


class ThemeButton(QToolButton):
    """The theme's button in the header: a half circle as Windows is set, a sun, or a moon."""

    def __init__(self):
        super().__init__()
        self.mode = "system"
        self.setFixedSize(32, 32)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setPopupMode(QToolButton.InstantPopup)

    def show_mode(self, mode):
        self.mode = mode
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        lit = self.underMouse() or self.isDown()
        if lit or self.hasFocus():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLORS["accent_soft"]))
            painter.drawEllipse(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        color = QColor(COLORS["accent"] if lit else COLORS["secondary"])
        center, radius = QRectF(self.rect()).center(), 6.5
        if self.mode == "light":
            painter.setPen(QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, 4.0, 4.0)
            for step in range(8):
                painter.save()
                painter.translate(center)
                painter.rotate(step * 45)
                painter.drawLine(QPointF(0, -6.5), QPointF(0, -9.0))
                painter.restore()
        elif self.mode == "dark":
            moon = QPainterPath()
            moon.addEllipse(center, radius, radius)
            bite = QPainterPath()
            bite.addEllipse(center + QPointF(4.2, -3.2), radius * 0.85, radius * 0.85)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPath(moon.subtracted(bite))
        else:
            painter.setPen(QPen(color, 1.6))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius, radius)
            half = QPainterPath()
            half.moveTo(center + QPointF(0, -radius))
            half.arcTo(QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2), 90, 180)
            half.closeSubpath()
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPath(half)
        painter.end()


class HelpButton(QPushButton):
    """The "?" beside a control, a small circle; hovering says what the control does."""

    def __init__(self):
        super().__init__("?")
        self.setFixedSize(22, 22)
        self.setProperty("kind", "help")
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        lit = self.underMouse() or self.isDown()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.hasFocus():
            painter.setPen(QPen(QColor(COLORS["accent"]), 1.5))
        else:
            painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLORS["accent_soft"] if lit else COLORS["page"]))
        painter.drawEllipse(rect)
        font = QFont(self.font())
        font.setPointSizeF(9)
        font.setWeight(QFont.Bold)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["accent"] if lit else COLORS["secondary"]))
        painter.drawText(QRectF(self.rect()), Qt.AlignCenter, "?")
        painter.end()


class Card(QFrame):
    """A rounded section with a blue title, like a group of Telegram's settings."""

    def __init__(self, title=""):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 12, 18, 14)
        self.body.setSpacing(10)
        self.title = QLabel(title)
        self.title.setObjectName("cardTitle")
        self.title.setVisible(bool(title))
        self.body.addWidget(self.title)

    def setTitle(self, text):
        self.title.setText(text)
        self.title.setVisible(bool(text))


class Switch(QCheckBox):
    """Telegram's switch: the label on the left, the toggle on the right, the whole row clickable."""
    TRACK = QSize(36, 20)

    def __init__(self, text=""):
        super().__init__(text)
        self.setCursor(Qt.PointingHandCursor)
        self._position = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(120)
        self._animation.valueChanged.connect(self._moved)
        self.toggled.connect(self._animate)

    def _animate(self, checked):
        self._animation.stop()
        if not self.isVisible():  # Set before the window shows: it opens already there.
            self._position = 1.0 if checked else 0.0
            self.update()
            return
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def _moved(self, value):
        self._position = float(value)
        self.update()

    def sizeHint(self):
        metrics = self.fontMetrics()
        return QSize(metrics.horizontalAdvance(self.text()) + 16 + self.TRACK.width(),
                     max(30, metrics.height() + 12))

    def minimumSizeHint(self):
        return QSize(self.TRACK.width() + 60, self.sizeHint().height())

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        enabled = self.isEnabled()
        if self._animation.state() != QVariantAnimation.Running:
            self._position = 1.0 if self.isChecked() else 0.0
        track = QRectF(self.width() - self.TRACK.width() - 2, (self.height() - self.TRACK.height()) / 2,
                       self.TRACK.width(), self.TRACK.height())
        off, on = QColor(COLORS["switch_off"]), QColor(COLORS["accent"])
        color = QColor(round(off.red() + (on.red() - off.red()) * self._position),
                       round(off.green() + (on.green() - off.green()) * self._position),
                       round(off.blue() + (on.blue() - off.blue()) * self._position))
        if not enabled:
            color.setAlpha(110)
        if self.hasFocus():
            painter.setPen(QPen(QColor(COLORS["accent_soft_hover"]), 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), 12, 12)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        path = QPainterPath()
        path.addRoundedRect(track, track.height() / 2, track.height() / 2)
        painter.drawPath(path)
        knob = track.height() - 4
        left = track.left() + 2 + (track.width() - knob - 4) * self._position
        painter.setBrush(QColor("white"))
        painter.drawEllipse(QRectF(left, track.top() + 2, knob, knob))
        painter.setPen(QColor(COLORS["text"] if enabled else COLORS["disabled"]))
        painter.setFont(self.font())
        painter.drawText(QRectF(0, 0, track.left() - 12, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        painter.end()
