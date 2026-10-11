"""Build a reviewable onedir release from the explicit public source inventory.

The same source, Python and locked packages build the same files, byte for byte. To check a downloaded
release, rebuild it with --config-from pointing at its SaveGramExporter.exe and compare SHA256SUMS.txt.
"""
import argparse
import dis
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

from PyQt5.QtCore import QByteArray, Qt
from PyQt5.QtGui import QImage, QPainter
from PyQt5.QtSvg import QSvgRenderer

from check_source import ROOT, source_files, check_network_boundary, check_known_credentials

sys.path.insert(0, str(ROOT / "src"))
from savegram_exporter.credentials import ConfigurationError, load_config, masked_config, unmask_config
from savegram_exporter.theme import MARK_SVG

# PyInstaller stamps the EXE with this time instead of the moment of the build (1980-01-01, the
# earliest date a ZIP holds), and a fixed hash seed keeps the order of the files in its archives.
BUILD_TIMESTAMP = 315532800
HASH_SEED = 0
# The EXE's icon: the window's mark, at each size Windows shows an app's icon at any display scaling.
ICON_SIZES = (16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 256)
# The release's top holds SaveGramExporter.exe; the libraries it loads are in app\.
EXE_NAME = "SaveGramExporter.exe"
APP_DIR = "app"


def write_build_config(directory, credentials, masked=None):
    payload, mask = masked or (masked_config(credentials) if credentials else (b"", b""))
    path = directory / "_savegram_build_config.py"
    path.write_text(f"# Generated locally; never publish this file.\nPAYLOAD = {payload!r}\nMASK = {mask!r}\n", encoding="ascii")
    return path


def built_config(exe):
    """The masked settings a released SaveGramExporter.exe carries, exactly as its build wrote them.

    Reads the compiled module's constants without running anything from the file.
    """
    from PyInstaller.archive.readers import CArchiveReader
    code = CArchiveReader(str(exe)).open_embedded_archive("PYZ.pyz").extract("_savegram_build_config")
    values, loaded = {}, None
    for instruction in dis.get_instructions(code):
        if instruction.opname == "LOAD_CONST":
            loaded = instruction.argval
        elif instruction.opname == "STORE_NAME":
            values[instruction.argval] = loaded
    return values["PAYLOAD"], values["MASK"]


def mark_image(side):
    """The mark as the window draws it (theme.MARK_SVG), `side` pixels across."""
    image = QImage(side, side, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(MARK_SVG.encode())).render(painter)
    painter.end()
    return image


def mark_ico(sizes=ICON_SIZES):
    """The EXE's icon: a Windows .ico file holding the mark as a PNG picture of each size.

    Qt draws the pixels, the same on every processor. The PNG pictures and the .ico are written here:
    Qt's own PNG writer gives other bytes on other processors, and a rebuild would no longer match.
    """
    pictures = [png(mark_image(side)) for side in sizes]
    header = struct.pack("<HHH", 0, 1, len(pictures))
    entries, offset = [], len(header) + 16 * len(pictures)
    for side, picture in zip(sizes, pictures):
        # Width and height 0 stand for 256; no palette; one plane of 32-bit colour.
        entries.append(struct.pack("<BBBBHHII", side % 256, side % 256, 0, 0, 1, 32, len(picture), offset))
        offset += len(picture)
    return header + b"".join(entries) + b"".join(pictures)


def png(image):
    """A PNG file of a square premultiplied QImage, whose pixels are blue, green, red and alpha bytes."""
    side = image.width()
    pixels = bytearray(image.constBits().asstring(image.sizeInBytes()))
    for start in range(0, len(pixels), 4):
        alpha = pixels[start + 3]
        if 0 < alpha < 255:  # Undo the multiplication: PNG keeps colours whole.
            for at in range(start, start + 3):
                pixels[at] = (pixels[at] * 255 + alpha // 2) // alpha
    pixels[0::4], pixels[2::4] = pixels[2::4], pixels[0::4]  # Red, green, blue, alpha.
    row = side * 4
    rows = b"".join(b"\0" + pixels[top:top + row] for top in range(0, len(pixels), row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as inp:
        while block := inp.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def inventory(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def place_bundle(bundle, target):
    """Move PyInstaller's folder into the release: the EXE on top, everything it loads in app\\ beside it.

    Someone who has just unpacked the ZIP sees the one program file, with its name and icon, first.
    A shortcut cannot do that: a .lnk keeps the absolute path of the folder it was made in, which
    exists on no other computer, and the time it was made, which no rebuild repeats.
    """
    names = sorted(item.name for item in bundle.iterdir())
    if names != sorted([APP_DIR, EXE_NAME]):
        raise RuntimeError(f"Unexpected PyInstaller output: {names}")
    for name in names:
        shutil.move(str(bundle / name), str(target / name))
    return target / EXE_NAME


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="local")
    parser.add_argument("--commit", help="Full source commit the build is made from; omitted for an unversioned local build")
    parser.add_argument("--api-config", type=Path, default=ROOT / ".private" / "telegram-api.json",
                        help="Your application's API ID and API Hash (never copied to public source)")
    parser.add_argument("--test-build", action="store_true",
                        help="Build without API settings: the program asks for an API ID and API Hash in its window")
    parser.add_argument("--config-from", type=Path, metavar="EXE",
                        help="Reuse the Telegram settings built into a downloaded SaveGramExporter.exe, "
                             "to rebuild that release byte for byte")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.label):
        parser.error("Invalid build label")
    if args.commit and not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("Expected a full source commit")
    if args.config_from and args.test_build:
        parser.error("--config-from rebuilds a release that can sign in; leave out --test-build")
    if platform.system() != "Windows" or platform.architecture()[0] != "64bit":
        parser.error("Use Windows x64")
    files = source_files()
    check_network_boundary()
    credentials = masked = None
    if args.config_from:
        try:
            masked = built_config(args.config_from)
            credentials = unmask_config(*masked)
        except ConfigurationError:
            parser.error("That EXE has no Telegram settings: rebuild it with --test-build instead")
        except Exception:
            parser.error("Not a SaveGramExporter.exe built by this script")
        check_known_credentials(files, credentials)
    elif not args.test_build:
        try:
            credentials = load_config(args.api_config)
        except ConfigurationError:
            parser.error("Fill .private/telegram-api.json with your application credentials; see BUILD.md")
        check_known_credentials(files, credentials)
    locked = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^\s\\]+)", (ROOT / "requirements" / "build.lock").read_text())
    for name, expected in locked:
        if metadata.version(name) != expected:
            raise RuntimeError(f"Install build.lock first: version mismatch for {name}")
    if args.commit:
        actual = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        if actual != args.commit:
            raise RuntimeError("Requested commit is not the current checkout")
        subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--", "."], check=True)
        subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", "--",
                        *(p.relative_to(ROOT).as_posix() for p in files)], check=True, stdout=subprocess.DEVNULL)
    target = ROOT / "release" / args.label
    target.mkdir(parents=True, exist_ok=False)
    source = target / "source"
    for path in files:
        out = source / path.relative_to(ROOT)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, out)
    source_hashes = inventory(source)
    provenance = dict(commit=args.commit, source_state="commit" if args.commit else "unversioned-local",
                      python=platform.python_version(), windows=platform.platform(),
                      pyinstaller=metadata.version("pyinstaller"), source_sha256=source_hashes,
                      telegram_configured=credentials is not None, test_build=args.test_build)
    (target / "SOURCE.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    icon = ROOT / "build" / args.label / "SaveGram.ico"
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(mark_ico())
    # Build the copied allowlisted source, not the mutable original checkout. None of the caller's
    # PYTHON* settings reach the build; only the fixed ones below.
    build_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
    build_env.update(PYINSTALLER_CONFIG_DIR=str(ROOT / ".tmp-pyinstaller"), PYTHONDONTWRITEBYTECODE="1",
                     PYTHONHASHSEED=str(HASH_SEED), SOURCE_DATE_EPOCH=str(BUILD_TIMESTAMP))
    # Injection lives outside the public snapshot. Only compiled masked data ships.
    private = ROOT / ".private"
    private.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=private) as temporary:
        config_dir = Path(temporary)
        write_build_config(config_dir, credentials, masked)
        # -s -P: no user site-packages and no current folder on the import path, as -I would, but
        # -I would also ignore PYTHONHASHSEED.
        subprocess.run([sys.executable, "-s", "-P", "-B", "-m", "PyInstaller", "--clean", "--noconfirm", "--onedir", "--windowed", "--noupx",
                        "--name", "SaveGramExporter", "--icon", str(icon), "--contents-directory", APP_DIR,
                        "--paths", str(source / "src"), "--paths", str(config_dir),
                        "--hidden-import", "_savegram_build_config",
                        # Telethon imports it only if present and otherwise falls back to pure-Python AES, which is
                        # far slower and caps every download.
                        "--hidden-import", "cryptg",
                        "--distpath", str(ROOT / "build" / args.label / "dist"), "--workpath", str(ROOT / "build" / args.label),
                        "--specpath", str(ROOT / "build" / args.label), str(source / "scripts" / "launcher.py")],
                       cwd=source, env=build_env, check=True)
    place_bundle(ROOT / "build" / args.label / "dist" / "SaveGramExporter", target)
    if not list((target / APP_DIR).rglob("cryptg*.pyd")):
        raise RuntimeError("The build lacks cryptg: Telegram downloads would use pure-Python AES")
    if inventory(source) != source_hashes:
        raise RuntimeError("Build modified the public source snapshot")
    licenses = target / "LICENSES"
    licenses.mkdir()
    shutil.copyfile(ROOT / "LICENSE", licenses / "SaveGram-GPL-3.0.txt")
    # Include license/notice files from all locked runtime/build distributions.
    package_names = re.findall(r"(?m)^([A-Za-z0-9_.-]+)==", (ROOT / "requirements" / "build.lock").read_text())
    components = []
    for name in package_names:
        dist = metadata.distribution(name)
        copied = []
        notice = licenses / name / "METADATA.txt"
        notice.parent.mkdir(parents=True, exist_ok=True)
        notice.write_text(dist.read_text("METADATA") or "", encoding="utf-8")
        for item in dist.files or []:
            if not any(token in Path(str(item)).name.lower() for token in ("license", "licence", "copying", "notice")):
                continue
            path = Path(dist.locate_file(item))
            if path.is_file() and path.suffix.lower() not in {".py", ".pyc", ".pyd", ".dll", ".exe"}:
                out = licenses / name / str(item).replace("../", "").replace("..\\", "")
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, out)
                copied.append(out.relative_to(target).as_posix())
        if name.lower() == "pyqt5" and not copied:
            out = licenses / name / "GPL-3.0.txt"
            shutil.copyfile(ROOT / "LICENSE", out)
            copied.append(out.relative_to(target).as_posix())
        if not copied:
            raise RuntimeError(f"No license text collected for {name}")
        components.append(dict(name=dist.metadata["Name"], version=dist.version,
                               license=dist.metadata.get("License-Expression") or dist.metadata.get("License"),
                               license_files=copied))
    # Python's bundled runtime is part of the EXE distribution too.
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("Python runtime license not found")
    shutil.copyfile(python_license, licenses / "Python-LICENSE.txt")
    (target / "COMPONENTS.json").write_text(json.dumps(components, indent=2), encoding="utf-8")
    hashes = inventory(target)
    (target / "SHA256SUMS.txt").write_text("\n".join(f"{v}  {k}" for k, v in hashes.items()) + "\n", encoding="utf-8")
    archive = target.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as out:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                out.write(path, path.relative_to(target))
    archive.with_suffix(".zip.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    source_zip = target.parent / f"{args.label}-source.zip"
    with zipfile.ZipFile(source_zip, "x", zipfile.ZIP_DEFLATED) as out:
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix != ".pyc":
                out.write(path, path.relative_to(source))
    source_zip.with_suffix(".zip.sha256").write_text(f"{sha256(source_zip)}  {source_zip.name}\n", encoding="ascii")
    print(f"Built {archive.name}; no upload or publication performed")


if __name__ == "__main__":
    main()
