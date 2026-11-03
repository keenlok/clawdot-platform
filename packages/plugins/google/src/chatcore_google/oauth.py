"""Operator-only desktop OAuth setup; no bot command ever handles credentials."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from chatcore_google.client import GoogleError, GoogleHttp

SCOPES = (
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
)


def authorization_request(client_id: str, redirect_uri: str) -> tuple[str, str, str]:
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return "https://accounts.google.com/o/oauth2/v2/auth?" + query, state, verifier


def validate_callback(target: str, state: str) -> str:
    parsed = urlsplit(target)
    query = parse_qs(parsed.query)
    values = query.get("state", [])
    codes = query.get("code", [])
    if (
        parsed.path != "/callback"
        or len(values) != 1
        or not secrets.compare_digest(values[0], state)
        or "error" in query
        or len(codes) != 1
        or not codes[0]
        or len(codes[0]) > 8192
    ):
        raise ValueError("Invalid or rejected Google authorization callback.")
    return codes[0]


async def exchange_code(http, config, code, verifier, redirect_uri, owner_email):
    value = await http.request(
        "POST",
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if not isinstance(value.get("refresh_token"), str) or not value.get("refresh_token"):
        raise GoogleError("Google did not grant offline access; authorize again with consent.")
    if not isinstance(value.get("access_token"), str) or not value.get("access_token"):
        raise GoogleError("Google authorization response is invalid.")
    about = await http.request(
        "GET",
        "https://www.googleapis.com/drive/v3/about",
        headers={"Authorization": "Bearer " + value["access_token"]},
        params={"fields": "user(emailAddress,permissionId)"},
    )
    user = about.get("user", {})
    if (
        str(user.get("emailAddress", "")).casefold() != owner_email.casefold()
        or not isinstance(user.get("permissionId"), str)
        or not user["permissionId"]
    ):
        raise GoogleError("Signed-in Google account does not match the configured owner.")
    return {
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": value["refresh_token"],
        "owner_email": owner_email,
        "permission_id": user["permissionId"],
        "scopes": list(SCOPES),
    }


def save_profile(path: Path, profile: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".google-oauth-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(profile, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def authorize(config: dict, owner_email: str) -> dict:
    result = {}
    state = ""

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log callback query strings or authorization codes.

        def do_GET(self):
            try:
                code = validate_callback(self.path, state)
                if "code" in result:
                    raise ValueError("Callback already handled")
                result["code"] = code
                status, message = 200, b"Authorization received. Return to the terminal."
            except ValueError:
                status, message = 400, b"Invalid authorization callback."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(message)

    with HTTPServer(("127.0.0.1", 0), Callback) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        url, state, verifier = authorization_request(config["client_id"], redirect_uri)
        print("Authorize the configured owner in your browser. No credentials are sent to chat.")
        print(url)
        webbrowser.open(url)
        deadline = time.monotonic() + 180
        while "code" not in result and time.monotonic() < deadline:
            server.handle_request()
        if "code" not in result:
            raise GoogleError("Google authorization timed out.")

    async def exchange():
        async with GoogleHttp(max_bytes=64_000) as http:
            return await exchange_code(
                http, config, result["code"], verifier, redirect_uri, owner_email
            )

    return asyncio.run(exchange())


def main():
    parser = argparse.ArgumentParser(description="Authorize one owner Google account locally")
    parser.add_argument("--client-file", type=Path, required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        value = json.loads(args.client_file.read_text())
        config = value["installed"]
        if not all(
            isinstance(config.get(key), str) and config[key]
            for key in ("client_id", "client_secret")
        ):
            raise ValueError
        profile = authorize(config, args.owner_email)
        save_profile(args.output, profile)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # GoogleError strings are sanitized; other exception messages could include credentials.
        parser.exit(
            1,
            (
                str(exc)
                if isinstance(exc, GoogleError)
                else "Authorization failed; check the desktop OAuth client file and owner account."
            )
            + "\n",
        )
    print("Owner authorization verified. Private credential profile saved.")
