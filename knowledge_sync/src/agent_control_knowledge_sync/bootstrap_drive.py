"""One-time: turn an OAuth client into a refresh token a human can paste.

Run by a person, once, on a machine with a browser. It prints a refresh token
and writes nothing: the operator puts it in `.env`, which is the only place this
deployment keeps secrets, and this script never learns where that is.

    uv run --package agent-control-knowledge-sync agent-knowledge-sync-bootstrap

**The loopback flow, not the device flow and not a pasted code.** Google
deprecated the out-of-band `urn:ietf:wg:oauth:2.0:oob` copy-paste flow, and the
device flow is not offered to Desktop clients for Drive scopes. Loopback is what
remains and what Desktop clients are for: this binds an ephemeral port on
127.0.0.1, opens the consent page, and catches the redirect. Nothing listens
afterwards.

**Log in as the reader account, not the agent's.** The consent page will offer
whichever Google session the browser already has. `agent-drive.md` 4.4.1's
inbound canary asserts `sharedWithMe` stays empty on the agent account forever,
so consenting as the agent and then sharing the corpus to it would latch the
Drive server off. The script prints which account it ended up as, because a
browser that was already signed in is the easy way to get this wrong.
"""

from __future__ import annotations

import argparse
import http.server
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser

import httpx

from .drive_auth import DRIVE_READONLY_SCOPE, GOOGLE_TOKEN_URL

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_CLIENT_ID_ENV = "AGENT_KNOWLEDGE_DRIVE_CLIENT_ID"
_CLIENT_SECRET_ENV = "AGENT_KNOWLEDGE_DRIVE_CLIENT_SECRET"

_DONE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Agent Control</title>"
    b"<body style='font:14px system-ui;padding:3rem'>"
    b"<p>Authorized. Close this tab and return to the terminal.</p>"
)


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Catches exactly one redirect and holds its query."""

    query: dict[str, list[str]] = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib's spelling
        type(self).query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_DONE_PAGE)

    def log_message(self, *args: object) -> None:
        """Silence the stdlib access log; it would print the auth code."""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-knowledge-sync-bootstrap",
        description=(
            "Mint a Drive refresh token for the corpus reader. Reads the client "
            f"from {_CLIENT_ID_ENV} and {_CLIENT_SECRET_ENV}, or --client-id and "
            "--client-secret."
        ),
    )
    parser.add_argument("--client-id", default=os.environ.get(_CLIENT_ID_ENV, ""))
    parser.add_argument("--client-secret", default=os.environ.get(_CLIENT_SECRET_ENV, ""))
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the URL instead of opening it, for a machine with no browser.",
    )
    args = parser.parse_args(argv)

    if not args.client_id or not args.client_secret:
        print(
            f"Missing the OAuth client. Set {_CLIENT_ID_ENV} and "
            f"{_CLIENT_SECRET_ENV}, or pass --client-id/--client-secret.\n"
            "Create one at: Google Cloud console > APIs & Services > Credentials "
            "> Create credentials > OAuth client ID > Desktop app.\n"
            "The consent screen must be publishing status INTERNAL: a Testing "
            "app's refresh tokens expire after seven days.",
            file=sys.stderr,
        )
        return 2

    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(24)

    consent = f"{GOOGLE_AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": f"{DRIVE_READONLY_SCOPE} openid email",
            # Without both of these Google returns no refresh token at all on a
            # repeat consent, which is the single most common way this script
            # appears to succeed and produces nothing usable.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )

    server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"Listening on {redirect_uri}")
    print("Sign in as the CORPUS READER account - not the agent's account.\n")
    if args.no_browser:
        print(consent)
    else:
        webbrowser.open(consent)
        print("(browser opened; if nothing happened, run again with --no-browser)")

    thread.join(timeout=300)
    server.server_close()
    query = _Catcher.query
    if not query:
        print("No redirect arrived within five minutes.", file=sys.stderr)
        return 1
    if query.get("state", [""])[0] != state:
        print("State mismatch; refusing the response.", file=sys.stderr)
        return 1
    if "error" in query:
        print(f"Google refused consent: {query['error'][0]}", file=sys.stderr)
        return 1

    code = query.get("code", [""])[0]
    if not code:
        print("Redirect carried no authorization code.", file=sys.stderr)
        return 1

    with httpx.Client(timeout=30) as client:
        exchanged = client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": args.client_id,
                "client_secret": args.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        if exchanged.status_code != 200:
            print(
                f"Token exchange failed (HTTP {exchanged.status_code}): "
                f"{exchanged.json().get('error', '')}",
                file=sys.stderr,
            )
            return 1
        payload = exchanged.json()
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            print(
                "Google returned no refresh_token. This happens when the account "
                "has already consented to this client: revoke it at "
                "https://myaccount.google.com/permissions and run again.",
                file=sys.stderr,
            )
            return 1

        who = "unknown"
        try:
            info = client.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {payload['access_token']}"},
            )
            who = str(info.json().get("email") or "unknown")
        except Exception:
            pass

    print("\n" + "=" * 68)
    print(f"Authorized as: {who}")
    print("If that is the agent's account, STOP: revoke it and redo as the reader.")
    print("=" * 68)
    print("\nAdd to .env at the repo root:\n")
    print(f"{_CLIENT_ID_ENV}={args.client_id}")
    print(f"{_CLIENT_SECRET_ENV}={args.client_secret}")
    print(f"AGENT_KNOWLEDGE_DRIVE_REFRESH_TOKEN={refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
