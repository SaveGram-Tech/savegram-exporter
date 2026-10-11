# Third-party components

SaveGram Exporter code is GPL-3.0-only. Distribution includes the original notices of its dependencies; component versions and collected license files are recorded in `COMPONENTS.json` and `LICENSES/` in each release.

| Component | Purpose | License family / upstream |
| --- | --- | --- |
| Telethon | Telegram MTProto client | MIT, https://github.com/LonamiWebs/Telethon |
| pyaes, rsa, pyasn1 | Telethon cryptographic/protocol dependencies | MIT / Apache-2.0 / BSD-2-Clause; see bundled notices |
| cryptg | Native AES for Telethon (replaces pure-Python AES at run time) | CC0-1.0, https://github.com/cher-nov/cryptg |
| PyQt5 | Windows GUI | GPL-3.0, https://www.riverbankcomputing.com/software/pyqt/intro |
| Qt 5 | GUI runtime included by PyQt5 wheels | LGPL-3.0 and third-party notices; https://www.qt.io/licensing/ |
| PyQt5-sip | Python bindings runtime | BSD-2-Clause; see bundled notice |
| qasync | Qt/asyncio integration | BSD-2-Clause, https://github.com/CabbageDevelopment/qasync |
| qrcode, colorama | Local QR generation / dependency | BSD, https://github.com/lincolnloop/python-qrcode |
| pywin32 | Windows DPAPI access | Python Software Foundation license; see bundled notices |
| CPython | Python runtime | PSF and bundled third-party notices |
| PyInstaller | Build-time packager and bootloader | GPL with bootloader exception, https://pyinstaller.org/en/stable/license.html |

The application is distributed under GPL-3.0, consistent with the GPL PyQt5 option. Keep the corresponding source, build instructions and original notices together with redistributed builds. Qt and Python include additional upstream components; their bundled notices remain applicable. Qt source for the pinned 5.15.2 runtime is available at https://download.qt.io/archive/qt/5.15/5.15.2/single/ .
