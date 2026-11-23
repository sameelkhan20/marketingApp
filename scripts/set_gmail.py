"""Gmail API sign-in for email alerts (same method as the Social Agent's set-gmail).

Needs a Google Cloud OAuth client of type "Desktop app" with the Gmail API enabled. You can reuse
the Social Agent's client. Run from the project folder:

    .\\.venv\\Scripts\\python.exe scripts\\set_gmail.py

It asks for the client ID and secret, opens Google's sign-in in the browser, catches the redirect
on http://127.0.0.1:8765/, and saves GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN /
GMAIL_SENDER in .env. Restart the app afterwards, then use "Send test email" in the Meta tab.
"""

import base64
import getpass
import hashlib
import http.server
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"
PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}/"
SCOPES = "https://www.googleapis.com/auth/gmail.send openid email"


def main() -> None:
    if not ENV.exists():
        sys.exit("No .env yet: run start.ps1 once (it creates .env), then run this again.")
    client_id = input("Google OAuth Client ID: ").strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        sys.exit("That is not a Google OAuth Client ID (it ends with .apps.googleusercontent.com). Nothing saved.")
    client_secret = getpass.getpass("Client secret (not shown): ").strip()
    if len(client_secret) < 10:
        sys.exit("That client secret looks too short. Nothing saved.")

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})

    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>Done. You can close this tab and go back to the terminal.</h3>".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    print("\nSign in with the Gmail that should SEND the alerts. If the browser did not open, use this link:\n")
    print(url + "\n")
    webbrowser.open(url)
    input("After Google says 'Done', press Enter here... ")
    server.server_close()

    if got.get("error"):
        sys.exit(f"Google says: {got['error']}. Nothing saved; run this again.")
    if got.get("state") != state or not got.get("code"):
        sys.exit("No sign-in came back from Google. Nothing saved; run this again.")
    body = urllib.parse.urlencode({"code": got["code"], "client_id": client_id, "client_secret": client_secret,
                                   "redirect_uri": REDIRECT, "grant_type": "authorization_code",
                                   "code_verifier": verifier}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body),
                                    timeout=30) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Google refused the sign-in: {e.read().decode()[:300]}. Nothing saved.")
    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit("Google gave no refresh token. Remove the app's access at myaccount.google.com/permissions "
                 "and run this again.")
    payload = tok.get("id_token", "..").split(".")[1]
    email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")
    if not email:
        sys.exit("Could not read the Google account's address. Nothing saved.")

    values = {"GMAIL_CLIENT_ID": client_id, "GMAIL_CLIENT_SECRET": client_secret,
              "GMAIL_REFRESH_TOKEN": refresh, "GMAIL_SENDER": email}
    lines = ENV.read_text(encoding="utf-8").splitlines()
    out = [l for l in lines if not any(l.startswith(k + "=") for k in values)]
    out += [f"{k}={v}" for k, v in values.items()]
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nSaved. Alerts will be sent from {email}. Restart the app, then press 'Send test email'.")


if __name__ == "__main__":
    main()
