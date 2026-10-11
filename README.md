# SaveGram Exporter

A Windows application that saves a Telegram chat of your choice to a `.tgex` file on your computer.
You can then upload the file to [savegram.tech](https://savegram.tech) yourself to read the chat in a browser.

## Install

You need 64-bit Windows 10 or 11.

1. Download `SaveGramExporter.zip` from [savegram.tech](https://savegram.tech).
2. Unpack the archive into any folder.
3. Run `SaveGramExporter.exe` at the top of the unpacked folder.

The application installs nothing into the system and does not ask for administrator rights. To remove it,
select **Sign out and forget account** if you chose to remember the sign-in, then delete the application folder.
If you entered your own API ID and API Hash, also delete the `%LOCALAPPDATA%\SaveGramExporter` folder.

## Export a chat

1. **Sign in.** Select **Sign in with QR**. In Telegram on your phone, open
   **Settings → Devices → Link Desktop Device** and scan the code. If two-step verification is enabled, enter
   its password and select **Continue**.

   If **API ID** and **API Hash** fields appear above the button, this copy has no connection settings or
   Telegram no longer accepts them. Download the latest version from [savegram.tech](https://savegram.tech),
   or enter the API ID and API Hash of your own Telegram app: the **?** next to the fields explains how to get
   them at [my.telegram.org](https://my.telegram.org).
2. **Choose a chat.** Select **Load chats**, then choose a chat, group, or channel. You can start typing its name.
3. **Choose a period.** The **From** and **To** fields use your computer's local time.
4. **Choose media.** **All media** includes photos, videos, and files. **Text, avatars and custom emoji**
   leaves attachments out. **Text only, no files** downloads no files at all.
5. **Choose an output folder.** The default is `%USERPROFILE%\Downloads\SaveGram`.
6. Select **Create TGEX** and keep the application open until it finishes. A large chat can take several
   hours. If Telegram asks the application to wait, it waits and continues by itself.
7. When **Done** appears, select **Open result folder**. The new `SaveGram_…` folder contains `export.tgex`;
   upload it to [savegram.tech](https://savegram.tech).

After a new sign-in, Telegram sends a **Data export request** message to your phone: the application is asking
for the fast export mode. The window shows the message before the export starts. Select **Allow** in it and the
history is read about twice as fast; the export then starts by itself. You can also select
**Start at the usual speed** instead of waiting.

The **Help & FAQ** button and the **?** icons next to the fields explain every setting.

### Other settings

- **Remember on this computer** skips the QR code the next time you start the application. Do not turn it on
  on a shared computer.
- **Hide who exported the chat** removes from the archive what points to your account. Participants are shown
  by @username or ID, without names, avatars, or statuses. The conversation itself may still show who exported
  it, especially in a private chat.
- **Maximum file size (MB)** skips larger attachments and keeps their messages.
- **Split archive into parts** cuts the `.tgex` into parts of the chosen size. Upload all the parts together.
- **Keep each archive within a size limit** (on by default) saves a large chat as several archives
  (`SaveGram_…_01`, `SaveGram_…_02`, and so on). Upload them one at a time.

### If an export is interrupted

Progress is kept in a `.savegram-resume-…` folder inside the output folder. Start an export of the same chat
with the same settings and it continues where it stopped. After restarting the application, sign in with the
same account, load the chats, select **Resume saved export…**, and choose `checkpoint.sqlite` in that folder.

The `.savegram-resume-…` folder holds the conversation unencrypted: do not share it. It is removed
automatically after a successful export. To abandon an unfinished export, close the application and delete the
folder.

## Sign-in and data

- The application connects only to Telegram. It does not upload the export to the website, collect statistics,
  or update itself. The website opens in your browser only when you select a button.
- The application only reads: it does not send messages, join chats, or mark messages as read.
- A remembered sign-in is stored in `%LOCALAPPDATA%\SaveGramExporter\account.dpapi`, encrypted by Windows for
  your user account. Your own API ID and API Hash, if you entered them, are stored the same way in `app.dpapi`
  next to it. The two-step verification password is never saved. The chosen theme is stored in
  `%APPDATA%\SaveGram`; you can delete that folder too after removing the application.
- **Sign out and forget account** ends the application's Telegram session and deletes the remembered sign-in.
  Finished exports stay where they are.
- The application's session is listed in Telegram under **Settings → Devices**, where you can end it at any
  time.

## Check the application

- The source code is in this repository. The downloaded ZIP contains the same code in its `source` folder and
  the checksums of all its files in `SHA256SUMS.txt`.
- Only `src/savegram_exporter/telegram.py` talks to Telegram. `python scripts/check_source.py` verifies this
  from the code and lists every Telegram request and every web address the application contains. It needs
  nothing but Python.
- You can run the application from source without the EXE, or build the EXE yourself and compare it with the
  downloaded one byte for byte: see [BUILD.md](BUILD.md).

## License

GPL-3.0-only; see [LICENSE](LICENSE). Third-party components and their licenses are listed in
[THIRD_PARTY.md](THIRD_PARTY.md).
