# Home Mail for Home Assistant

Independent, lightweight Gmail client for Home Assistant Ingress. This is an **experimental first version**, not a Tachyon fork. The existing Tachyon add-on is unaffected.

## Install

Add `https://github.com/BrainDeLook/ha-mail-client` as a custom Home Assistant add-on repository, install **Home Mail**, then enter the Gmail address and its **app password** in the add-on configuration. Start the add-on and open it from the HA sidebar. Do not enter the normal Google account password. No second login or in-app administrator panel is used.

For Raspberry Pi 5, the published `aarch64` image must be available before installation. The repository's build workflow publishes the exact `0.1.0` tag used by `mail/config.yaml`.

## First-version features

- Ingress-only web interface, no published host port, full-screen HA panel with a Home Assistant sidebar button.
- IMAP sync of the newest 50 messages per standard Gmail folder (configurable 10–200); custom folders load when opened.
- Persistent SQLite cache in `/data/mail.db`, reused after add-on restarts. Account changes clear the previous account's cached mail.
- Inbox, Sent, Drafts, Spam and Trash where Gmail exposes those folders; local search of cached messages; read/unread and star flags.
- Compose and reply through Gmail SMTP. Settings, including app password and sync interval, live exclusively in the HA add-on configuration.
- HTML-only mail is converted to plain text before display. No scripts or remote images from emails are rendered.

## Current limitations

This first version has no attachment viewing/sending, Gmail OAuth, rich HTML rendering, draft editing, message move/delete, or remote search of older messages. Messages larger than 12 MB are not cached. The interface shows only cached messages and may remain empty until the first sync finishes. It has not yet been tested against a real Gmail account on a Home Assistant device; automated tests use a fake IMAP server.

The add-on relies on Home Assistant Ingress authentication; do not expose its internal port directly. Its SQLite cache and Home Assistant options, including the app password, are part of the add-on's cold backup. Treat backups accordingly.

## Development

Run `python3 -m unittest discover -s mail/tests -v` and `node --check mail/app/app.js`. The app uses the Python standard library only.
