# Arety Messenger

Arety Messenger is a self-hosted, WhatsApp-style chat application for a Twilio WhatsApp sender. A single-file Python server (aiohttp) receives incoming WhatsApp messages and delivery receipts from Twilio over signed webhooks, stores conversations and media in SQLite on disk, and serves a mobile-friendly single-page web app that lists chats, shows message bubbles with sent/delivered/read ticks, and lets you reply with text or attachments. A tiny Android app wraps the web app in a WebView so it installs like a native messenger. The problem it solves is using a business WhatsApp number from a phone or browser with a familiar interface, while keeping all message history on your own machine.

## Where it is used

In Abrar's setup the server runs as a user systemd service on the laptop and is published over a Cloudflare Tunnel on a dedicated subdomain. Twilio is pointed at that address by `connect_sender.py`, the Android APK is side-loaded on the phone, and incoming location pins are also saved to a JSON file so another local project (a phone ordering bot) can read the last shared address.

## How it works

### Server (`server.py`)

- **Configuration.** `~/.config/arety/wa-line.env` provides `PUBLIC_URL`, `APP_PASSWORD`, `WA_SENDER` (`whatsapp:+1...`) and optional `PORT` (default 8792). Twilio credentials are read from `~/Workspace/twilio/twilio.env` on every request so a rotated token is picked up without a restart. The server listens on `127.0.0.1` only; a reverse proxy or tunnel provides HTTPS.
- **Storage.** `data/chat.db` (SQLite) with tables `contacts(number, name, notes, wa_name, created)`, `messages(id, sid, number, direction, body, media, media_type, status, error, unread, ts, rev)` and `meta(key, value)`. Every change bumps a global revision counter (`next_rev`) and stamps the changed rows, so clients can ask for "everything after rev N". Downloaded inbound media goes to `data/media/<MessageSid>.<ext>`; outbound attachments go to `data/pub/<random-token>/<filename>` so Twilio can fetch them from an unguessable public URL.
- **Auth.** `POST /api/login` compares the password in constant time, rate-limits to 8 failed attempts per 5 minutes, and issues a random bearer token; the last 10 tokens are kept in `meta`. A middleware requires one of those tokens (header `Authorization: Bearer ...` or `?t=` query, used for `<img>` tags) on every other `/api/*` route.
- **App API.** `GET /api/sync?rev=N` returns changed messages, the full contact list with each contact's `window_ends` (last inbound time + 24 h, because WhatsApp only allows free-form replies inside that window) and the current rev. `POST /api/contacts` and `DELETE /api/contacts/{number}` manage contacts (deleting also removes their messages and media). `POST /api/read` clears unread flags. `POST /api/chats/{number}/send` accepts JSON `{body}` or a multipart form with `body` and `file`; it inserts a `sending` row, calls Twilio's Messages API with `StatusCallback` set, and records the SID and status or a `failed` state with the error text. `GET /api/media/{id}` streams a message's media.
- **Twilio webhooks.** `twilio_form` recomputes the `X-Twilio-Signature` (HMAC-SHA1 over the public URL plus sorted form fields) and rejects mismatches with 403. `/twilio/inbound` upserts the contact (recording the WhatsApp profile name), writes `data/last_location.json` when a location pin is shared, downloads the first media attachment with basic auth, and inserts the message as unread. `/twilio/status` updates `status` but only forwards along `sending -> queued -> accepted -> sent -> delivered -> read` (out-of-order callbacks never move a message backwards) and records `failed`/`undelivered` with the error code.
- Numbers are normalised to E.164 by `to_e164` (strip non-digits, `00` -> `+`, add `+` if missing).

### Web app (`static/index.html`)

A dependency-free single page with hash routing (`#/`, `#/chat/<n>`, `#/profile/<n>`, `#/edit/<n>`, `#/new`). It stores the token in `localStorage`, calls `/api/sync` every 2.5 s while visible (and on tab focus), merges changes by message id, renders the chat list sorted by last activity with unread counts and search, shows day separators and tick marks, remembers per-chat drafts, auto-grows the composer, and attaches a file via the clip button. Media is shown inline through `/api/media/<id>?t=<token>`.

### Android wrapper (`android/`)

`MainActivity.java` is a full-screen `WebView` with JavaScript and DOM storage enabled (so the login survives), cookie acceptance, a file chooser hooked to `onShowFileChooser` for attachments (images, video, audio, PDF), back-button navigation, state restore on rotation, an offline error page, and external links opened in the system browser. `build.sh` builds the APK without Gradle: `aapt2 compile/link` -> `javac` -> `d8` -> `zipalign` -> `apksigner`, generating a release keystore in `~/.config/arety/` on first use so later builds install over the old one. The web root URL is a constant in `MainActivity.java`.

### `connect_sender.py`

Lists the account's WhatsApp senders via the Messaging v2 Senders API, finds `WA_SENDER`, and sets its inbound and status webhook URLs to `PUBLIC_URL/twilio/inbound` and `PUBLIC_URL/twilio/status`.

| Path | Role |
|---|---|
| `server.py` | aiohttp server: auth, API, Twilio webhooks, storage |
| `static/index.html` | the web client |
| `static/AretyMessenger.apk` | APK served for download at `/static/AretyMessenger.apk` |
| `connect_sender.py` | one-off webhook registration |
| `android/` | WebView app sources and Gradle-less build script |
| `data/` | runtime: `chat.db`, `media/`, `pub/`, `last_location.json` |

## Setup

Requirements: Python 3.10+, `aiohttp`, a Twilio account with a number approved as a WhatsApp sender, a public HTTPS URL that forwards to the server port. For the APK: Android SDK build-tools and platform 34, a JDK.

```bash
cd ~/wa-line
python3 -m venv .venv && .venv/bin/pip install aiohttp

mkdir -p ~/.config/arety
cat > ~/.config/arety/wa-line.env <<'EOF'
PUBLIC_URL=https://messenger.example.com
APP_PASSWORD=choose-a-long-password
WA_SENDER=+10000000000
PORT=8792
EOF
```

`~/Workspace/twilio/twilio.env` must contain `ACCOUNT_SID=` and `AUTH_TOKEN=` (shared with the `twilio-sms-bridge` project).

User service:

```ini
# ~/.config/systemd/user/wa-line.service
[Unit]
Description=Arety Messenger
After=network-online.target

[Service]
WorkingDirectory=%h/wa-line
ExecStart=%h/wa-line/.venv/bin/python server.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Point your tunnel or reverse proxy at `http://127.0.0.1:8792`, then register the webhooks:

```bash
.venv/bin/python connect_sender.py
```

Android build (edit `HOME` in `MainActivity.java` to your URL first):

```bash
android/build.sh 2        # version code 2 -> ../AretyMessenger.apk
cp AretyMessenger.apk static/
```

## Usage

```bash
systemctl --user enable --now wa-line
journalctl --user -u wa-line -f
```

Open `https://messenger.example.com`, enter the app password, press "+" to add a contact with its full international number, and start chatting. The 24-hour reply window is shown per contact; outside it WhatsApp only accepts approved templates, so free-form sends will come back as `failed` with Twilio's error text. Install the APK from `/static/AretyMessenger.apk` on the phone for the app experience.

## Project structure

```
wa-line/
├── server.py
├── connect_sender.py
├── static/
│   ├── index.html
│   └── AretyMessenger.apk
├── android/
│   ├── build.sh
│   ├── AndroidManifest.xml
│   ├── res/{drawable/icon.xml, values/styles.xml}
│   └── src/.../MainActivity.java
└── data/                 # runtime, not committed
```

## Limitations and notes

- Single user, single password; tokens never expire but only the latest 10 are valid.
- Polling (2.5 s) rather than push; the app only updates while open and visible.
- Only the first media attachment of an inbound message is stored; outbound uploads are capped at 20 MB by the server.
- Template messages, reactions, group chats and read receipts *to* the other side are not implemented.
- Files in `data/pub/` stay publicly reachable (by their random token) as long as they exist; delete the contact to remove its media.
- The web root URL and Android package id are hard-coded in the Android sources and must be edited before building for another host.
