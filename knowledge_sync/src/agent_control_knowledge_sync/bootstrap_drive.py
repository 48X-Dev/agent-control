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

**Which account you consent as is a design decision, not a detail.** The consent
page offers whichever Google session the browser already has, and the script
prints the result because a signed-in browser is how somebody authorizes the
wrong identity without noticing. `company-knowledge.md` 2.1 records what this
deployment chose: the agent's own account, under this separate `drive.readonly`
client, with `agent-drive.md`'s inbound canary amended from an invariant to an
allowlist as the stated cost. A deployment that wants the invariant back mints
this token for a dedicated reader account instead; nothing in the sync depends
on which account it belongs to.
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
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help=(
            "Fixed loopback port. Only needed for a WEB APPLICATION client, "
            "which accepts a redirect URI only if it was registered exactly - "
            "so register http://localhost:<port>/ and pass the same port here. "
            "A Desktop app client needs none of this: Google accepts any "
            "127.0.0.1 port for it, which is why Desktop is the recommended type."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=["127.0.0.1", "localhost"],
        help=(
            "Loopback spelling. Google treats 127.0.0.1 and localhost as "
            "different redirect URIs, so this has to match the registration "
            "character for character."
        ),
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

    port = args.port or _free_port()
    redirect_uri = f"http://{args.host}:{port}/"
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
        print(
            "No redirect arrived within five minutes.\n\n"
            "If the browser showed Google's own 400 page, nothing ever reached "
            "this listener and the error is in the client, not here. Open "
            "'Error details' on that page:\n"
            "  redirect_uri_mismatch -> the client is a Web application, which "
            "accepts only pre-registered redirect URIs. Either recreate it as a "
            "Desktop app, or register "
            f"{redirect_uri} exactly and re-run with --port {port}.\n"
            "  access_blocked / org policy -> the consent screen is not "
            "published Internal, or the account is outside the org.\n"
            "  invalid_client -> the id and secret are not from the same client.",
            file=sys.stderr,
        )
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
    print(
        "Check that against company-knowledge.md 2.1. This deployment reads the\n"
        "corpus with the agent's own account under a separate read-only client,\n"
        "so agent.control@ is expected here - but the allowlist, not the sharing,\n"
        "is what decides which folders are indexed."
    )
    print("=" * 68)
    print("\nAdd to .env at the repo root:\n")
    print(f"{_CLIENT_ID_ENV}={args.client_id}")
    print(f"{_CLIENT_SECRET_ENV}={args.client_secret}")
    print(f"AGENT_KNOWLEDGE_DRIVE_REFRESH_TOKEN={refresh_token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
