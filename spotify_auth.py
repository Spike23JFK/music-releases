#!/usr/bin/env python3
"""
One-time Spotify authentication helper.
Run this once to get your refresh token, then add it to .env and GitHub secrets.

Usage:
    python spotify_auth.py
"""

import requests
import base64
import urllib.parse
import http.server
import threading
import webbrowser
import sys
import os

REDIRECT_URI = "http://localhost:8888/callback"
SCOPE = "playlist-read-private playlist-read-collaborative"

# ── grab credentials ────────────────────────────────────────────────────────
print("\n=== Spotify Auth Setup ===\n")
print("1. Go to https://developer.spotify.com/dashboard")
print("2. Click 'Create app'")
print("3. Fill in any App name and App description")
print("4. Set Redirect URI to:  http://localhost:8888/callback")
print("5. Tick 'Web API' under APIs used, then Save")
print("6. Open the app → Settings → copy Client ID and Client Secret\n")

client_id     = input("Paste your Client ID:     ").strip()
client_secret = input("Paste your Client Secret: ").strip()

# ── build auth URL ───────────────────────────────────────────────────────────
params = {
    "client_id":     client_id,
    "response_type": "code",
    "redirect_uri":  REDIRECT_URI,
    "scope":         SCOPE,
}
auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)

# ── local server to catch the callback ──────────────────────────────────────
code_holder = {}

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if "code" in qs:
            code_holder["code"] = qs["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h2>Done! You can close this tab.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h2>No code found. Try again.</h2>")

    def log_message(self, *_):  # silence request logs
        pass

server = http.server.HTTPServer(("localhost", 8888), _Handler)
thread = threading.Thread(target=server.handle_request)
thread.start()

print("\nOpening Spotify login in your browser...")
webbrowser.open(auth_url)
print("(If the browser didn't open, go to this URL manually:)")
print(f"  {auth_url}\n")
thread.join(timeout=120)
server.server_close()

if "code" not in code_holder:
    print("ERROR: Did not receive auth code in time. Try again.")
    sys.exit(1)

# ── exchange code for tokens ─────────────────────────────────────────────────
creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
r = requests.post(
    "https://accounts.spotify.com/api/token",
    headers={"Authorization": f"Basic {creds}"},
    data={
        "grant_type":   "authorization_code",
        "code":         code_holder["code"],
        "redirect_uri": REDIRECT_URI,
    },
    timeout=10,
)
r.raise_for_status()
tokens = r.json()
refresh_token = tokens["refresh_token"]

# ── ask for playlist ID ──────────────────────────────────────────────────────
print("\nAuthentication successful!\n")
print("Now open the playlist in Spotify, click '...' → Share → Copy link to playlist")
print("It looks like: https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
playlist_url = input("\nPaste playlist link (or just the ID): ").strip()
playlist_id = playlist_url.split("/playlist/")[-1].split("?")[0].strip()

# ── write .env ───────────────────────────────────────────────────────────────
env_path = os.path.join(os.path.dirname(__file__), ".env")
with open(env_path, "w") as f:
    f.write(f"SPOTIFY_CLIENT_ID={client_id}\n")
    f.write(f"SPOTIFY_CLIENT_SECRET={client_secret}\n")
    f.write(f"SPOTIFY_REFRESH_TOKEN={refresh_token}\n")
    f.write(f"SPOTIFY_PLAYLIST_ID={playlist_id}\n")

print(f"\n.env saved to: {env_path}")
print("\n=== Next: add these as GitHub Secrets ===")
print("Go to: https://github.com/Spike23JFK/music-releases/settings/secrets/actions")
print("Add 4 secrets (New repository secret):\n")
print(f"  SPOTIFY_CLIENT_ID     = {client_id}")
print(f"  SPOTIFY_CLIENT_SECRET = {client_secret}")
print(f"  SPOTIFY_REFRESH_TOKEN = {refresh_token}")
print(f"  SPOTIFY_PLAYLIST_ID   = {playlist_id}")
print("\nDone! Run music_releases.py to test it locally.")
