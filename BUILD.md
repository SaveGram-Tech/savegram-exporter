# Run and build from source

You need Windows 10 or 11 x64 and Python 3.13.3 x64 from
[python.org](https://www.python.org/downloads/release/python-3133/). Run the commands in PowerShell from the
project folder.

## Install

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/bootstrap.lock
.\.venv\Scripts\python.exe -m pip install --no-build-isolation --require-hashes -r requirements/build.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation .
```

Every dependency is pinned by version and checksum in `requirements/*.lock`.

## Run

```powershell
.\.venv\Scripts\python.exe -m savegram_exporter
```

To sign in to Telegram, the application needs the API ID and API Hash of a Telegram app. Get your own at
[my.telegram.org](https://my.telegram.org) under API development tools and enter them in the application
window before signing in: it remembers them on this computer.

## Check the code

```powershell
.\.venv\Scripts\python.exe scripts/check_source.py
```

The command checks that the project contains no unexpected files or keys and that only `telegram.py` reaches
the network, then lists every Telegram request and every web address found in the code. It does not connect to
Telegram.

## Build the EXE

```powershell
.\.venv\Scripts\python.exe scripts/build_release.py --label my-build --test-build
```

The result is the `release\my-build` folder and its ZIP: `SaveGramExporter.exe` at the top, the application's
libraries in `app\`, the source it was built from in `source\`, build details in `SOURCE.json` and
`COMPONENTS.json`, licenses in `LICENSES\`, and the checksums of all files in `SHA256SUMS.txt`. The script
publishes nothing and sends nothing anywhere.

An application built with `--test-build` asks for an API ID and API Hash in its sign-in window. To build your
own into it, leave out `--test-build` and put them in `.private\telegram-api.json`:
`{"api_id": 12345, "api_hash": "your API Hash"}`. Git ignores the `.private` folder.

## Compare with the downloaded application

The same code, Python 3.13.3 from python.org, and the pinned dependencies produce the same files, byte for
byte. This lets you confirm that the downloaded application was built from this code.

1. Unpack the downloaded `SaveGramExporter.zip`, for example into `C:\check\official`.
2. Take the source of the same version: this repository, or the `C:\check\official\source` folder, which holds
   the same files. Install the dependencies as described above.
3. Build the application with the sign-in settings built into the downloaded one. The script reads them from
   the EXE without running anything from it:

   ```powershell
   .\.venv\Scripts\python.exe scripts/build_release.py --label check --config-from C:\check\official\SaveGramExporter.exe
   ```

4. Compare the checksums of the application's files:

   ```powershell
   Compare-Object ((Get-Content C:\check\official\SHA256SUMS.txt) -match ' (app/|SaveGramExporter\.exe$)') ((Get-Content release\check\SHA256SUMS.txt) -match ' (app/|SaveGramExporter\.exe$)')
   ```

   No output means every file of the application matches.
