import json
import logging
import secrets
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer
 
import requests
 
from src.config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_REDIRECT_URI,
    SPOTIFY_SCOPES,
    SPOTIFY_TOKEN_CACHE_PATH,
)
 
log = logging.getLogger(__name__)
 
TOKEN_URL    = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
 
# Refresh this many seconds before actual expiry, so a token never goes
# stale mid-request due to clock drift or request latency.
_EXPIRY_SAFETY_MARGIN_SECONDS = 60
 
# How long to wait for the user to complete the browser consent step
# before giving up. Generous, since this is a one-time interactive step.
_AUTH_FLOW_TIMEOUT_SECONDS = 120
 
 
class _CallbackHandler(BaseHTTPRequestHandler):
    """
    Catches exactly one GET request to the OAuth redirect URI, extracts
    `code` and `state` from the query string, and stores them on the
    server instance for the waiting main thread to read.
    """
 
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
 
        self.server.oauth_code  = params.get("code", [None])[0]
        self.server.oauth_state = params.get("state", [None])[0]
        self.server.oauth_error = params.get("error", [None])[0]
 
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
 
        if self.server.oauth_error:
            body = (
                b"<html><body><h2>Spotify authorization failed.</h2>"
                b"<p>You can close this tab and try again.</p></body></html>"
            )
        else:
            body = (
                b"<html><body><h2>Spotify authorized \xe2\x9c\x93</h2>"
                b"<p>You can close this tab and return to the agent.</p>"
                b"</body></html>"
            )
        self.wfile.write(body)
 
    def log_message(self, format, *args):
        # Silence the default per-request stderr logging — this fires
        # exactly once and we already log the meaningful outcome ourselves.
        pass
 
 
class SpotifyAuth:
    """
    Manages the Spotify Authorization Code flow and local token cache.
 
    Usage:
        auth = SpotifyAuth()
        token = auth.get_valid_token()   # None if not configured at all
        headers = {"Authorization": f"Bearer {token}"}
    """
 
    def __init__(self):
        self._cache_path = Path(SPOTIFY_TOKEN_CACHE_PATH)
 
    def is_configured(self) -> bool:
        """True if client ID/secret are present — doesn't mean authorized yet."""
        return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)
 
    def get_valid_token(self) -> Optional[str]:
        """
        Return a valid access token, refreshing or running the full
        interactive authorization flow as needed. Returns None only if
        SPOTIFY_CLIENT_ID/SECRET aren't configured at all — a genuine
        "Spotify isn't set up" case the caller should surface clearly,
        not silently swallow.
        """
        if not self.is_configured():
            log.warning(
                "Spotify not configured — SPOTIFY_CLIENT_ID/SECRET missing from .env"
            )
            return None
 
        cached = self._load_cache()
 
        if cached and cached.get("access_token"):
            if time.time() < cached.get("expires_at", 0) - _EXPIRY_SAFETY_MARGIN_SECONDS:
                return cached["access_token"]
 
            # Expired or about to be — refresh rather than re-authorize.
            if cached.get("refresh_token"):
                refreshed = self._refresh_access_token(cached["refresh_token"])
                if refreshed:
                    return refreshed["access_token"]
                # Refresh failed (token revoked?) — fall through to a
                # full re-authorization rather than failing outright.
                log.warning("Spotify token refresh failed — re-authorizing")
 
        # No cache, or refresh failed: run the full interactive flow.
        result = self._run_authorization_flow()
        return result["access_token"] if result else None
 
    # -----------------------------------------------------------------
    # Token cache
    # -----------------------------------------------------------------
 
    def _load_cache(self) -> Optional[dict]:
        if not self._cache_path.exists():
            return None
        try:
            return json.loads(self._cache_path.read_text())
        except Exception as e:
            log.warning("Could not read Spotify token cache: %s", e)
            return None
 
    def _save_cache(self, token_data: dict) -> None:
        try:
            self._cache_path.write_text(json.dumps(token_data))
        except Exception as e:
            # Not fatal — worst case, next call re-authorizes. Still
            # want to know if the disk write is silently failing though.
            log.warning("Could not write Spotify token cache: %s", e)
 
    # -----------------------------------------------------------------
    # Token acquisition
    # -----------------------------------------------------------------
 
    def _refresh_access_token(self, refresh_token: str) -> Optional[dict]:
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id":     SPOTIFY_CLIENT_ID,
                    "client_secret": SPOTIFY_CLIENT_SECRET,
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
 
            token_data = {
                "access_token":  data["access_token"],
                # Spotify doesn't always return a new refresh_token on
                # refresh — keep the old one if it didn't send a new one.
                "refresh_token": data.get("refresh_token", refresh_token),
                "expires_at":    time.time() + data.get("expires_in", 3600),
            }
            self._save_cache(token_data)
            log.info("Spotify access token refreshed")
            return token_data
 
        except Exception as e:
            log.error("Spotify token refresh failed: %s", e)
            return None
 
    def _run_authorization_flow(self) -> Optional[dict]:
        """
        Full interactive Authorization Code flow. Blocks the calling
        thread for up to _AUTH_FLOW_TIMEOUT_SECONDS while the user
        completes the consent step in their browser. This is a one-time
        cost — every subsequent call hits the cache or a silent refresh.
        """
        state = secrets.token_urlsafe(16)
 
        auth_params = {
            "client_id":     SPOTIFY_CLIENT_ID,
            "response_type": "code",
            "redirect_uri":  SPOTIFY_REDIRECT_URI,
            "scope":         SPOTIFY_SCOPES,
            "state":         state,
        }
        auth_url = f"{AUTHORIZE_URL}?{urlencode(auth_params)}"
 
        parsed_redirect = urlparse(SPOTIFY_REDIRECT_URI)
        host = parsed_redirect.hostname or "127.0.0.1"
        port = parsed_redirect.port or 8888
 
        server = HTTPServer((host, port), _CallbackHandler)
        server.oauth_code  = None
        server.oauth_state = None
        server.oauth_error = None
        server.timeout = _AUTH_FLOW_TIMEOUT_SECONDS
 
        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        server_thread.start()
 
        print(
            "\n[SPOTIFY] First-time setup — opening your browser to authorize "
            "this agent with your Spotify account."
        )
        print(
            f"[SPOTIFY] Waiting up to {_AUTH_FLOW_TIMEOUT_SECONDS}s for you to "
            f"click 'Agree' (listening on {SPOTIFY_REDIRECT_URI}) ..."
        )
        webbrowser.open(auth_url)
 
        server_thread.join(timeout=_AUTH_FLOW_TIMEOUT_SECONDS)
 
        if server.oauth_error:
            print(f"[SPOTIFY] Authorization was denied or failed: {server.oauth_error}")
            log.error("Spotify authorization error: %s", server.oauth_error)
            return None
 
        if not server.oauth_code:
            print(
                "[SPOTIFY] Timed out waiting for authorization. "
                "Run 'python scripts/spotify_login.py' to try again."
            )
            log.error("Spotify authorization timed out — no code received")
            return None
 
        if server.oauth_state != state:
            print("[SPOTIFY] Authorization state mismatch — possible tampering, aborting.")
            log.error("Spotify OAuth state mismatch (possible CSRF)")
            return None
 
        try:
            response = requests.post(
                TOKEN_URL,
                data={
                    "grant_type":    "authorization_code",
                    "code":          server.oauth_code,
                    "redirect_uri":  SPOTIFY_REDIRECT_URI,
                    "client_id":     SPOTIFY_CLIENT_ID,
                    "client_secret": SPOTIFY_CLIENT_SECRET,
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
 
            token_data = {
                "access_token":  data["access_token"],
                "refresh_token": data.get("refresh_token"),
                "expires_at":    time.time() + data.get("expires_in", 3600),
            }
            self._save_cache(token_data)
            print("[SPOTIFY] Authorized successfully — you won't need to do this again.")
            log.info("Spotify authorization completed and cached")
            return token_data
 
        except Exception as e:
            print(f"[SPOTIFY] Failed to exchange authorization code: {e}")
            log.error("Spotify token exchange failed: %s", e)
            return None