"""Allowlisted source inventory and automatic checks of what the program can do.

Run it to see, from the code itself, every Telegram request the program makes and every web address
it holds. It fails on a file outside the list, on anything that looks like a key, and on code outside
telegram.py that could reach the network, start a process or run generated code.
It reports paths and names only, never matched secret values.
"""
import ast
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[1]
TOP = ("README.md", "BUILD.md", "LICENSE", "THIRD_PARTY.md", "pyproject.toml", ".gitignore", ".gitattributes")
TREES = {"src": {".py"}, "scripts": {".py"}, "requirements": {".lock"}}
PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{35}\b"),
    re.compile(r"(?i)(?:api_hash|session_string)['\"]?\s*[:=]\s*['\"][a-zA-Z0-9+/=_-]{32,}['\"]"),
]

# Every Telegram request the program builds itself, all in telegram.py. Besides signing in, they only
# read; the takeout session is the one Telegram Desktop also opens to export an account's data.
TELEGRAM_REQUESTS = {
    "auth.ImportLoginTokenRequest",
    "account.InitTakeoutSessionRequest", "account.FinishTakeoutSessionRequest", "InvokeWithTakeoutRequest",
    "messages.GetHistoryRequest", "messages.GetForumTopicsRequest", "messages.GetFullChatRequest",
    "channels.GetFullChannelRequest", "users.GetFullUserRequest", "users.GetUsersRequest",
    "messages.GetMessageReactionsListRequest", "messages.GetPollVotesRequest",
    "messages.GetCustomEmojiDocumentsRequest", "messages.GetStickerSetRequest",
    "upload.GetFileRequest", "upload.GetCdnFileRequest",
}
# Every Telethon client method telegram.py calls: signing in and out, the connection, and reading
# chats, messages and profiles and downloading their files.
TELEGRAM_CLIENT = {
    "connect", "disconnect", "is_connected", "session", "_switch_dc", "is_user_authorized", "qr_login", "_on_login",
    "sign_in", "get_me", "log_out", "iter_dialogs", "get_entity", "get_messages", "download_media",
    "download_profile_photo", "_iter_download",
}
# The web addresses the program opens, in the browser, only when one is clicked: the site, from its button
# and the help; the site's page for this source code; and Telegram's page where a person gets an API ID and
# API Hash of their own, named in the help only while the program asks for them. The last one names the SVG
# format inside the drawn icons; nothing is fetched from it.
WEB_ADDRESSES = {"https://savegram.tech", "https://savegram.tech/source", "https://my.telegram.org",
                 "http://www.w3.org/2000/svg"}
FORBIDDEN_MODULES = {"requests", "httpx", "aiohttp", "urllib", "urllib3", "http", "socket", "ssl", "ftplib", "smtplib",
                     "poplib", "imaplib", "xmlrpc", "webbrowser", "subprocess", "multiprocessing", "ctypes",
                     "importlib", "PyQt5.QtNetwork", "PyQt5.QtWebEngineWidgets", "PyQt5.QtWebEngineCore",
                     "PyQt5.QtWebKit", "PyQt5.QtWebKitWidgets"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
PROCESS_CALLS = re.compile(r"system|popen|startfile|fork|(posix_)?spawn.*|exec.*")
CONNECTIONS = {"open_connection", "start_server", "create_connection", "create_server", "create_datagram_endpoint",
               "create_subprocess_exec", "create_subprocess_shell", "sock_connect"}


def source_files(root=ROOT):
    paths = [root / name for name in TOP]
    for directory, suffixes in TREES.items():
        for path in (root / directory).rglob("*"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"Link in source tree: {path.relative_to(root)}")
            if "__pycache__" in path.parts or any(part.endswith(".egg-info") for part in path.parts) or path.is_dir():
                continue
            if path.suffix not in suffixes or path.name == "_savegram_build_config.py":
                raise ValueError(f"Unexpected public source file: {path.relative_to(root)}")
            paths.append(path)
    for path in paths:
        if not path.is_file():
            raise ValueError(f"Missing public source file: {path.name}")
        # A release lists each file's SHA-256 and git keeps them with LF (.gitattributes). A file an
        # editor saved with CRLF builds the same program but lists other hashes than the repository.
        if b"\r\n" in path.read_bytes():
            raise ValueError(f"CRLF line endings: {path.relative_to(root).as_posix()}; save it with LF")
        content = path.read_text(encoding="utf-8")
        if any(pattern.search(content) for pattern in PATTERNS):
            raise ValueError(f"Potential secret: {path.relative_to(root)}")
    return sorted(paths)


def check_known_credentials(paths, credentials):
    # Catch even renamed variables or accidental copies in docs. Never log values.
    api_id, api_hash = credentials
    needles = (str(api_id).encode(), api_hash.encode())
    for path in paths:
        content = path.read_bytes()
        if any(value in content for value in needles):
            raise ValueError(f"Application credential found in public source: {path.name}")


def _modules(root):
    for path in sorted((root / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node
        yield path, tree


def _dotted(node):
    """The attribute names that follow a name: `functions.messages.GetHistoryRequest` → messages.GetHistoryRequest."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.parent
    return ".".join(parts)


def check_network_boundary(root=ROOT):
    """Only telegram.py talks to Telegram, through the requests and client methods listed above."""
    for path, tree in _modules(root):
        network = path.name == "telegram.py"
        for node in ast.walk(tree):
            modules, telethon = [], False
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
                telethon = any(module.split(".")[0] == "telethon" for module in modules)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                modules = [base] + [f"{base}.{alias.name}" for alias in node.names]
                # Elsewhere only Telegram's data types and error classes: neither can send anything.
                telethon = base.split(".")[0] == "telethon" and (
                    base != "telethon" or bool({alias.name for alias in node.names} - {"types", "errors"}))
            if any(m == f or m.startswith(f + ".") for m in modules for f in FORBIDDEN_MODULES):
                raise ValueError(f"Unexpected network/process dependency: {path.name}")
            if telethon and not network:
                raise ValueError(f"Telegram client outside telegram.py: {path.name}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                raise ValueError(f"Dynamic code execution: {path.name}")
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == "os" and PROCESS_CALLS.fullmatch(node.attr):
                    raise ValueError(f"Process start: {path.name}")
                if node.attr in CONNECTIONS:
                    raise ValueError(f"Unexpected network/process dependency: {path.name}")
                if node.attr == "_client" and not network:
                    raise ValueError(f"Telethon client escaped network boundary: {path.name}")
    requests, methods = telegram_calls(root)
    if requests - TELEGRAM_REQUESTS:
        raise ValueError(f"Unlisted Telegram request: {', '.join(sorted(requests - TELEGRAM_REQUESTS))}")
    if methods - TELEGRAM_CLIENT:
        raise ValueError(f"Unlisted Telethon client method: {', '.join(sorted(methods - TELEGRAM_CLIENT))}")
    addresses = web_addresses(root)
    if addresses - WEB_ADDRESSES:
        raise ValueError(f"Unlisted web address: {', '.join(sorted(addresses - WEB_ADDRESSES))}")


def telegram_calls(root=ROOT):
    """Telegram requests built and Telethon client methods called in telegram.py."""
    requests, methods = set(), set()
    for path, tree in _modules(root):
        if path.name != "telegram.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "functions":
                name = _dotted(node.parent)
                if not name.endswith("Request") or name.count(".") > 1:
                    raise ValueError(f"Telegram request built indirectly in {path.name}")
                requests.add(name)
            elif isinstance(node, ast.Attribute) and node.attr == "_client" and not isinstance(node.ctx, ast.Store):
                if isinstance(node.parent, ast.Attribute):
                    methods.add(node.parent.attr)
                elif not (isinstance(node.parent, ast.Call) and node.parent.func is node):
                    raise ValueError(f"Telethon client handed on in {path.name}")
    return requests, methods


def web_addresses(root=ROOT):
    found = set()
    for _, tree in _modules(root):
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.update(re.findall(r"\w+://[^\s\"'<>)]+", node.value))
    return found


if __name__ == "__main__":
    files = source_files()
    check_network_boundary()
    requests, methods = telegram_calls()
    print(f"Public source guard passed: {len(files)} files")
    print("Only telegram.py talks to Telegram. Requests it builds:\n  " + "\n  ".join(sorted(requests)))
    print("Telethon client methods it calls:\n  " + "\n  ".join(sorted(methods)))
    print("Web addresses in the program:\n  " + "\n  ".join(sorted(web_addresses())))
