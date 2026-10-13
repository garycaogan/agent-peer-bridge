"""Minimal relay HTTP client shared by the executor and the MCP adapter (stdlib only)."""
from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class RelayError(Exception):
    """A failed relay call. `uncertain` means a write may have landed: inspect, never blindly resend."""

    def __init__(self, code, message, *, uncertain=False):
        super().__init__(message)
        self.code, self.uncertain = code, uncertain


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never forward the bearer token elsewhere
        return None


def read_secret(path):
    """Read a token file that must be a private regular file (chmod 600) owned by this user."""
    path = Path(path).expanduser()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid())):
        raise RelayError("unsafe_token_file", f"{path} must be a regular file readable only by you (chmod 600)")
    return path.read_text(encoding="utf-8").strip()


class RelayClient:
    def __init__(self, url, token, *, timeout=30):
        parsed = urllib.parse.urlsplit(url)
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise RelayError("insecure_url", "relay URL must be https (plain http is allowed only for localhost)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise RelayError("invalid_url", "relay URL must be a bare origin like https://relay.example.com")
        self.base = f"{parsed.scheme}://{parsed.netloc}"
        self._token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect())

    def redact(self, text):
        return text.replace(self._token, "[REDACTED]") if isinstance(text, str) else text

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token, "Content-Type": "application/json", "Accept": "application/json"})
        write = method != "GET"
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read() or b"{}").get("error", "")
            except ValueError:
                message = ""
            error.close()
            raise RelayError(f"http_{error.code}", self.redact(message) or f"relay returned {error.code}",
                             uncertain=write and error.code >= 500) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise RelayError("transport", "relay unreachable or timed out; the write may or may not have landed",
                             uncertain=write) from None
