# Home Mail for Home Assistant

Independent, lightweight Gmail client for Home Assistant Ingress. It is still experimental, not a Tachyon fork. The existing Tachyon add-on is unaffected.

## Install

Add `https://github.com/BrainDeLook/ha-mail-client` as a custom Home Assistant add-on repository, install **Home Mail**, then enter the Gmail address and its **app password** in the add-on configuration. Start the add-on and open it from the HA sidebar. Do not enter the normal Google account password. No second login or in-app administrator panel is used.

For Raspberry Pi 5, the published `aarch64` image must be available before installation. The repository's build workflow publishes the exact `0.3.3` tag used by `mail/config.yaml`.

## Features

- Ingress-only web interface, no published host port, full-screen HA panel with a Home Assistant sidebar button.
- IMAP sync of the newest 50 messages per standard Gmail folder (configurable 10–200); custom folders load when opened.
- Persistent SQLite cache in `/data/mail.db`, reused after add-on restarts. Account changes clear the previous account's cached mail.
- Background polling continues while the web panel is closed. Normal polls fetch only UIDs newer than the last cached UID; a metadata-only reconciliation every six hours detects deletions and flag changes without downloading old message bodies again. Open panels check the cache revision every eight seconds and refresh only when mail changes.
- Inbox, Sent, Drafts, Spam and Trash where Gmail exposes those folders; local search of cached messages; read/unread and star flags.
- Compose and reply through Gmail SMTP. Settings, including app password and sync interval, live exclusively in the HA add-on configuration.
- Gmail special folders and IMAP modified UTF-7 names are displayed in readable form.
- Sanitized HTML mail with basic sender formatting, cached inline images/audio/video and downloadable cached attachments. External HTTPS media loads by default; set `show_external_media: false` to block it until clicking **Show external media** for a message. Remote images can reveal your IP address to the sender. Email scripts, forms and frames are blocked.
- The add-on settings provide a `theme` dropdown: `system` (follows the device), `light`, `dark`, or `ha_dark` (Home Assistant's default dark palette). `ha_dark` matches the default HA colors, not an installed custom HA theme.
- On mobile, tap the shaded area outside the folder panel (or press Escape) to close it.
- `log_level` selects `error`, `warning`, `info` (default) or `debug`. Routine HTTP requests and empty sync polls appear only at `debug`; request query strings and message contents are not logged.

## Current limitations

This version has no attachment sending, Gmail OAuth, draft editing, message move/delete, or remote search of older messages. Messages larger than 12 MB and MIME parts larger than 10 MB are not cached. The interface shows only cached messages and may remain empty until the first sync finishes. Live behavior on Home Assistant/Gmail needs user verification; automated tests use a fake IMAP server.

The add-on relies on Home Assistant Ingress authentication; do not expose its internal port directly. Its SQLite cache and Home Assistant options, including the app password, are part of the add-on's cold backup. Treat backups accordingly.

## Development

Run `python3 -m unittest discover -s mail/tests -v` and `node --check mail/app/app.js`. The app uses the Python standard library only.
