"""Arety Messenger — a chat app for your Twilio WhatsApp number.

    phone app / browser  <->  this server  <->  Twilio WhatsApp API  <->  the other person's WhatsApp

  /                      the app (static/index.html)
  /api/...               used by the app, needs the login token
  /twilio/inbound        Twilio posts incoming WhatsApp messages here (signature checked)
  /twilio/status         Twilio posts sent / delivered / read updates here
  /pub/<token>/<file>    files you send; Twilio downloads them from here

Run by the wa-line user service. Settings: ~/.config/arety/wa-line.env and ~/Workspace/twilio/twilio.env
"""
import asyncio
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

from aiohttp import BasicAuth, ClientSession, ClientTimeout, web

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
(DATA / "media").mkdir(parents=True, exist_ok=True)
(DATA / "pub").mkdir(parents=True, exist_ok=True)


def read_env(path):
    return dict(line.strip().split("=", 1) for line in Path(path).expanduser().read_text().splitlines()
                if "=" in line and not line.startswith("#"))


CFG = read_env("~/.config/arety/wa-line.env")
PUBLIC_URL = CFG["PUBLIC_URL"].rstrip("/")
PASSWORD = CFG["APP_PASSWORD"]
SENDER = CFG["WA_SENDER"]                      # e.g. +10000000000
PORT = int(CFG.get("PORT", "8792"))
WINDOW_SECONDS = 24 * 3600                     # WhatsApp only allows free-form replies this long after their last message


def twilio_creds():
    tw = read_env("~/Workspace/twilio/twilio.env")   # re-read each time so a rotated token is picked up
    return tw["ACCOUNT_SID"], tw["AUTH_TOKEN"]


# ---------------------------------------------------------------- storage

db = sqlite3.connect(DATA / "chat.db")
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS contacts (
    number TEXT PRIMARY KEY, name TEXT DEFAULT '', notes TEXT DEFAULT '', wa_name TEXT DEFAULT '', created REAL);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sid TEXT UNIQUE, number TEXT, direction TEXT, body TEXT DEFAULT '',
    media TEXT DEFAULT '', media_type TEXT DEFAULT '', status TEXT DEFAULT '', error TEXT DEFAULT '',
    unread INTEGER DEFAULT 0, ts REAL, rev INTEGER);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS messages_rev ON messages(rev);
""")


def next_rev():
    """Every change gets a higher number, so the app can ask for 'everything after rev N'."""
    row = db.execute("SELECT value FROM meta WHERE key='rev'").fetchone()
    rev = int(row["value"]) + 1 if row else 1
    db.execute("INSERT OR REPLACE INTO meta VALUES ('rev', ?)", (str(rev),))
    return rev


def tokens():
    row = db.execute("SELECT value FROM meta WHERE key='tokens'").fetchone()
    return json.loads(row["value"]) if row else []


def to_e164(text):
    number = re.sub(r"[^\d+]", "", text or "")
    if number.startswith("00"):
        number = "+" + number[2:]
    return number if number.startswith("+") else "+" + number


def ensure_contact(number, wa_name=""):
    if not db.execute("SELECT 1 FROM contacts WHERE number=?", (number,)).fetchone():
        db.execute("INSERT INTO contacts (number, wa_name, created) VALUES (?, ?, ?)", (number, wa_name, time.time()))
    elif wa_name:
        db.execute("UPDATE contacts SET wa_name=? WHERE number=?", (wa_name, number))


# ---------------------------------------------------------------- auth

_failed_logins = []


@web.middleware
async def auth(request, handler):
    if request.path.startswith("/api/") and request.path != "/api/login":
        given = request.headers.get("Authorization", "")[7:] or request.query.get("t", "")
        if given not in tokens():
            return web.json_response({"error": "login required"}, status=401)
    return await handler(request)


async def login(request):
    now = time.time()
    _failed_logins[:] = [t for t in _failed_logins if now - t < 300]
    if len(_failed_logins) >= 8:
        return web.json_response({"error": "too many attempts, wait a few minutes"}, status=429)
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")).encode(), PASSWORD.encode()):
        _failed_logins.append(now)
        return web.json_response({"error": "wrong password"}, status=403)
    token = secrets.token_urlsafe(32)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('tokens', ?)", (json.dumps((tokens() + [token])[-10:]),))
    db.commit()
    return web.json_response({"token": token, "sender": SENDER.removeprefix("whatsapp:")})


# ---------------------------------------------------------------- app API

async def sync(request):
    """Everything that changed after ?rev=N: changed messages plus the full contact list."""
    after = int(request.query.get("rev", "0"))
    msgs = [dict(r) for r in db.execute("SELECT * FROM messages WHERE rev > ? ORDER BY id", (after,))]
    contacts = []
    for c in db.execute("SELECT * FROM contacts"):
        last_in = db.execute("SELECT MAX(ts) AS t FROM messages WHERE number=? AND direction='in'", (c["number"],)).fetchone()["t"]
        contacts.append({**dict(c), "window_ends": (last_in + WINDOW_SECONDS) if last_in else 0})
    row = db.execute("SELECT value FROM meta WHERE key='rev'").fetchone()
    return web.json_response({"rev": int(row["value"]) if row else 0, "messages": msgs, "contacts": contacts, "now": time.time()})


async def save_contact(request):
    body = await request.json()
    number = to_e164(body.get("number", ""))
    if len(number) < 8:
        return web.json_response({"error": "enter the full number with country code, e.g. ++10000000000"}, status=400)
    ensure_contact(number)
    db.execute("UPDATE contacts SET name=?, notes=? WHERE number=?", (body.get("name", "").strip(), body.get("notes", "").strip(), number))
    next_rev()
    db.commit()
    return web.json_response({"number": number})


async def delete_contact(request):
    number = to_e164(request.match_info["number"])
    for row in db.execute("SELECT media FROM messages WHERE number=? AND media != ''", (number,)):
        (DATA / "media" / row["media"]).unlink(missing_ok=True)
    db.execute("DELETE FROM messages WHERE number=?", (number,))
    db.execute("DELETE FROM contacts WHERE number=?", (number,))
    next_rev()
    db.commit()
    return web.json_response({"ok": True})


async def mark_read(request):
    number = to_e164((await request.json()).get("number", ""))
    if db.execute("SELECT 1 FROM messages WHERE number=? AND unread=1", (number,)).fetchone():
        db.execute("UPDATE messages SET unread=0, rev=? WHERE number=? AND unread=1", (next_rev(), number))
        db.commit()
    return web.json_response({"ok": True})


async def send(request):
    """Text (JSON) or a file with an optional caption (multipart form)."""
    number = to_e164(request.match_info["number"])
    text, media_name, media_type, media_url = "", "", "", ""
    if request.content_type.startswith("multipart/"):
        async for part in (await request.multipart()):
            if part.name == "body":
                text = (await part.text()).strip()
            elif part.name == "file" and part.filename:
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", part.filename)[-80:]
                folder = DATA / "pub" / secrets.token_urlsafe(24)
                folder.mkdir()
                with open(folder / safe, "wb") as f:
                    while chunk := await part.read_chunk():
                        f.write(chunk)
                media_type = part.headers.get("Content-Type") or mimetypes.guess_type(safe)[0] or "application/octet-stream"
                media_name = f"{folder.name}/{safe}"
                media_url = f"{PUBLIC_URL}/pub/{media_name}"
    else:
        text = str((await request.json()).get("body", "")).strip()
    if not text and not media_url:
        return web.json_response({"error": "nothing to send"}, status=400)

    ensure_contact(number)
    cur = db.execute("INSERT INTO messages (number, direction, body, media, media_type, status, ts, rev) VALUES (?, 'out', ?, ?, ?, 'sending', ?, ?)",
                     (number, text, ("pub:" + media_name) if media_name else "", media_type, time.time(), next_rev()))
    msg_id = cur.lastrowid
    db.commit()

    form = {"From": SENDER, "To": f"whatsapp:{number}", "StatusCallback": f"{PUBLIC_URL}/twilio/status"}
    if text:
        form["Body"] = text
    if media_url:
        form["MediaUrl"] = media_url
    sid, token = twilio_creds()
    try:
        async with request.app["http"].post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                                            data=form, auth=BasicAuth(sid, token)) as r:
            reply = await r.json()
        if r.status >= 400:
            raise RuntimeError(reply.get("message", f"Twilio error {r.status}"))
        db.execute("UPDATE messages SET sid=?, status=?, rev=? WHERE id=?", (reply["sid"], reply.get("status", "queued"), next_rev(), msg_id))
    except Exception as e:  # noqa: BLE001
        db.execute("UPDATE messages SET status='failed', error=?, rev=? WHERE id=?", (str(e)[:300], next_rev(), msg_id))
    db.commit()
    return web.json_response({"id": msg_id})


async def media(request):
    row = db.execute("SELECT media, media_type FROM messages WHERE id=?", (request.match_info["id"],)).fetchone()
    if not row or not row["media"]:
        raise web.HTTPNotFound()
    path = DATA / "pub" / row["media"][4:] if row["media"].startswith("pub:") else DATA / "media" / row["media"]
    return web.FileResponse(path, headers={"Content-Type": row["media_type"] or "application/octet-stream",
                                           "Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- Twilio webhooks

async def twilio_form(request, path):
    """The posted fields, or None when the request was not signed by Twilio."""
    form = dict(await request.post())
    payload = PUBLIC_URL + path + "".join(k + form[k] for k in sorted(form))
    expected = base64.b64encode(hmac.new(twilio_creds()[1].encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return form if hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")) else None


async def inbound(request):
    form = await twilio_form(request, "/twilio/inbound")
    if form is None:
        return web.Response(status=403, text="bad signature")
    number = to_e164(form.get("From", "").removeprefix("whatsapp:"))
    ensure_contact(number, form.get("ProfileName", ""))
    if form.get("Latitude") and form.get("Longitude"):
        # a shared location pin: remembered for the phone AI's food ordering, and shown in the chat
        (DATA / "last_location.json").write_text(json.dumps({
            "lat": float(form["Latitude"]), "lng": float(form["Longitude"]), "from": number,
            "label": form.get("Label") or form.get("Address") or "", "time": time.time()}))
        form["Body"] = form.get("Body") or "📍 Location shared"
    media_file, media_type = "", ""
    if int(form.get("NumMedia", "0") or 0) > 0:
        media_type = form.get("MediaContentType0", "")
        media_file = form["MessageSid"] + (mimetypes.guess_extension(media_type) or "")
        sid, token = twilio_creds()
        try:
            async with request.app["http"].get(form["MediaUrl0"], auth=BasicAuth(sid, token)) as r:
                (DATA / "media" / media_file).write_bytes(await r.read())
        except Exception as e:  # noqa: BLE001
            print(f"could not download media: {e}", flush=True)
            media_file = ""
    db.execute("INSERT OR IGNORE INTO messages (sid, number, direction, body, media, media_type, status, unread, ts, rev) "
               "VALUES (?, ?, 'in', ?, ?, ?, 'received', 1, ?, ?)",
               (form.get("MessageSid"), number, form.get("Body", ""), media_file, media_type, time.time(), next_rev()))
    db.commit()
    return web.Response(text="<Response/>", content_type="text/xml")


async def status(request):
    form = await twilio_form(request, "/twilio/status")
    if form is None:
        return web.Response(status=403, text="bad signature")
    order = ["sending", "queued", "accepted", "sent", "delivered", "read"]
    new = form.get("MessageStatus", "")
    row = db.execute("SELECT status FROM messages WHERE sid=?", (form.get("MessageSid"),)).fetchone()
    if row:
        failed = new in ("failed", "undelivered")
        # updates can arrive out of order: never go back from "read" to "delivered"
        if failed or (new in order and order.index(new) > (order.index(row["status"]) if row["status"] in order else -1)):
            error = f"{form.get('ErrorCode', '')} {form.get('ErrorMessage', '')}".strip() if failed else ""
            db.execute("UPDATE messages SET status=?, error=?, rev=? WHERE sid=?", ("failed" if failed else new, error, next_rev(), form["MessageSid"]))
            db.commit()
    return web.Response(text="")


# ---------------------------------------------------------------- static

async def index(request):
    return web.FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


async def public_file(request):
    path = (DATA / "pub" / request.match_info["token"] / request.match_info["name"]).resolve()
    if DATA / "pub" not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def http_session(app):
    app["http"] = ClientSession(timeout=ClientTimeout(total=60))
    yield
    await app["http"].close()


def main():
    app = web.Application(middlewares=[auth], client_max_size=20 * 1024 * 1024)
    app.cleanup_ctx.append(http_session)
    app.router.add_get("/", index)
    app.router.add_static("/static", HERE / "static")
    app.router.add_get("/pub/{token}/{name}", public_file)
    app.router.add_post("/api/login", login)
    app.router.add_get("/api/sync", sync)
    app.router.add_post("/api/contacts", save_contact)
    app.router.add_delete("/api/contacts/{number}", delete_contact)
    app.router.add_post("/api/read", mark_read)
    app.router.add_post("/api/chats/{number}/send", send)
    app.router.add_get("/api/media/{id}", media)
    app.router.add_post("/twilio/inbound", inbound)
    app.router.add_post("/twilio/status", status)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
