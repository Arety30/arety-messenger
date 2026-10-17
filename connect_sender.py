"""Point the Twilio WhatsApp sender at this app, so incoming messages and delivery updates reach it.
Run once after the number is registered as a WhatsApp sender in the Twilio Console:

    .venv/bin/python connect_sender.py
"""
import base64
import json
import urllib.error
import urllib.request
from pathlib import Path


def read_env(path):
    return dict(line.strip().split("=", 1) for line in Path(path).expanduser().read_text().splitlines()
                if "=" in line and not line.startswith("#"))


cfg, tw = read_env("~/.config/arety/wa-line.env"), read_env("~/Workspace/twilio/twilio.env")
AUTH = "Basic " + base64.b64encode(f"{tw['ACCOUNT_SID']}:{tw['AUTH_TOKEN']}".encode()).decode()
BASE = "https://messaging.twilio.com/v2/Channels/Senders"


def call(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Twilio error {e.code}: {e.read().decode()[:300]}")


senders = call(f"{BASE}?Channel=whatsapp").get("senders", [])
match = [s for s in senders if s["sender_id"] == cfg["WA_SENDER"]]
if not match:
    raise SystemExit(f"{cfg['WA_SENDER']} is not registered as a WhatsApp sender yet. "
                     f"Senders on the account: {[s['sender_id'] for s in senders] or 'none'}")
updated = call(f"{BASE}/{match[0]['sid']}", {"webhook": {
    "callback_url": f"{cfg['PUBLIC_URL']}/twilio/inbound", "callback_method": "POST",
    "status_callback_url": f"{cfg['PUBLIC_URL']}/twilio/status", "status_callback_method": "POST"}})
print(f"{updated['sender_id']} ({updated['status']}) now delivers to {updated['webhook']['callback_url']}")
