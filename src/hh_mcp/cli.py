from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import threading
import time
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .applications import draft_view
from .auth import OAuthTokens, make_pkce_request
from .config import environment_access, load_settings, save_settings
from .errors import HHMCPError
from .runtime import Runtime
from .security import ensure_private_directory
from .server import main as serve


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hh-mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the MCP stdio server")
    configure = commands.add_parser("configure", help="store local application settings")
    configure.add_argument("--callback-url", default="http://127.0.0.1:8765/callback")
    auth = commands.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("status")
    auth_sub.add_parser("login")
    auth_sub.add_parser("logout")
    commands.add_parser("doctor", help="check local configuration without contacting HH")
    application = commands.add_parser("application")
    app_sub = application.add_subparsers(dest="application_command", required=True)
    for name in ("show", "status", "submit"):
        item = app_sub.add_parser(name)
        item.add_argument("draft_id")
    return parser


def _configure(callback_url: str) -> None:
    if environment_access() is not None:
        raise HHMCPError(
            "configuration",
            "Unset HH_MCP_ACCESS_TOKEN and HH_MCP_USER_AGENT before configuring OAuth credentials",
        )
    current = load_settings()
    user_agent = input("HH User-Agent (for example: MyHHMCP/0.1 contact@example.com): ").strip()
    client_id = input("HH application client ID: ").strip()
    client_secret = getpass.getpass("HH application client secret (hidden): ").strip()
    settings = replace(current, user_agent=user_agent, callback_url=callback_url)
    settings.validate()
    ensure_private_directory(settings.home)
    save_settings(settings)
    runtime = Runtime(settings)
    try:
        runtime.auth.configure_application(client_id, client_secret)
        _json(
            {
                "ok": True,
                "config": str(settings.home / "config.json"),
                "secrets": "AES-256-GCM with master key in system credentials",
                "credential_backend": runtime.credential_backend_name,
            }
        )
    finally:
        asyncio.run(runtime.close())


class _CallbackResult:
    def __init__(self) -> None:
        self.code: str | None = None
        self.error: str | None = None
        self.finished = threading.Event()


def _handler(expected_path: str, expected_host: str, expected_state: str, result: _CallbackResult):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != expected_path or self.headers.get("Host") != expected_host:
                self.send_error(404)
                return
            received_state = query.get("state", [None])[0]
            if received_state != expected_state:
                self._reply(400, "Authorization rejected. The listener is still waiting.")
                return
            error = query.get("error", [None])[0]
            code = query.get("code", [None])[0]
            if error or not code:
                result.error = str(error or "authorization code missing")
                self._finish(400, "Authorization failed. Return to the terminal.")
                return
            result.code = str(code)
            self._finish(200, "HH authorization received. You may close this tab.")

        def _finish(self, status: int, message: str) -> None:
            self._reply(status, message)
            result.finished.set()

        def _reply(self, status: int, message: str) -> None:
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            return

    return Handler


async def _login() -> None:
    runtime = Runtime()
    callback = urlparse(runtime.settings.callback_url)
    client_id, client_secret = runtime.auth.application_credentials()
    request = make_pkce_request(runtime.settings, client_id)
    result = _CallbackResult()
    server = HTTPServer(
        ("127.0.0.1", int(callback.port)),
        _handler(callback.path, f"127.0.0.1:{callback.port}", request.state, result),
    )
    server.timeout = 1
    try:
        print("Open this URL in your browser if it did not open automatically:", file=sys.stderr)
        print(request.authorize_url, file=sys.stderr)
        webbrowser.open(request.authorize_url)
        deadline = time.monotonic() + 180
        while not result.finished.is_set() and time.monotonic() < deadline:
            await asyncio.to_thread(server.handle_request)
        if not result.finished.is_set():
            raise HHMCPError("oauth_timeout", "No OAuth callback was received within 180 seconds")
        if result.error or not result.code:
            raise HHMCPError("oauth_rejected", result.error or "OAuth callback did not contain a code")
        response = await runtime.client.token_request(
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": runtime.settings.callback_url,
                "code": result.code,
                "code_verifier": request.verifier,
            }
        )
        tokens = OAuthTokens.from_response(response)
        me = await runtime.client.me(tokens.access_token)
        if me.get("is_applicant") is not True or me.get("auth_type") != "applicant":
            raise HHMCPError("wrong_account_role", "The authorized HH account is not an applicant account")
        account_id = me.get("id")
        if not isinstance(account_id, str):
            account_id = str(account_id) if account_id is not None else ""
        generation = runtime.auth.commit_login(account_id, tokens)
        _json({"ok": True, "account_id": account_id, "generation": generation})
    finally:
        server.server_close()
        await runtime.close()


def _doctor() -> None:
    runtime = Runtime()
    if runtime.mode == "environment_access_token":
        result = {
            "ok": True,
            "python": sys.version.split()[0],
            "mode": runtime.mode,
            "auth": runtime.auth.status(),
            "local_state_initialized": False,
            "network_checked": False,
        }
    else:
        assert runtime.state is not None
        marker = b"hh-mcp-aes-gcm-roundtrip"
        encrypted = runtime.state.protector.protect(marker, entropy=b"doctor")
        encryption_ok = runtime.state.protector.unprotect(encrypted, entropy=b"doctor") == marker
        result = {
            "ok": encryption_ok,
            "python": sys.version.split()[0],
            "home": str(runtime.settings.home),
            "encryption_roundtrip": encryption_ok,
            "credential_backend": runtime.credential_backend_name,
            "auth": runtime.auth.status(),
            "state": runtime.state.counts(),
            "network_checked": False,
        }
    _json(result)
    asyncio.run(runtime.close())


async def _application(command: str, draft_id: str) -> None:
    runtime = Runtime()
    try:
        applications = await runtime.ready_applications()
        draft = applications.state.get_draft(draft_id)
        view = draft_view(draft)
        if command == "show":
            _json(view)
            return
        if command == "status":
            _json(await applications.status(draft_id))
            return
        _json(view)
        print(
            "Manual boundary only: a shell-capable assistant could automate this prompt. "
            "Proceed only when you personally launched and reviewed it.",
            file=sys.stderr,
        )
        phrase = f"SEND {draft.message_hash[-8:]}"
        answer = input(f"Type exactly {phrase} to send: ")
        if answer != phrase:
            raise HHMCPError("cancelled", "Submission cancelled")
        _json(await applications.submit_from_cli(draft_id))
    finally:
        await runtime.close()


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            serve()
        elif args.command == "configure":
            _configure(args.callback_url)
        elif args.command == "doctor":
            _doctor()
        elif args.command == "auth" and args.auth_command == "status":
            runtime = Runtime()
            _json({"ok": True, **runtime.auth.status()})
            asyncio.run(runtime.close())
        elif args.command == "auth" and args.auth_command == "login":
            asyncio.run(_login())
        elif args.command == "auth" and args.auth_command == "logout":
            runtime = Runtime()
            _json({"ok": True, "generation": runtime.auth.logout(), "remote_revoked": False})
            asyncio.run(runtime.close())
        elif args.command == "application":
            asyncio.run(_application(args.application_command, args.draft_id))
    except HHMCPError as exc:
        _json(exc.as_dict())
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
