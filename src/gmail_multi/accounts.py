"""Account registry and per-account OAuth credentials.

Layout of the config directory (default ~/.config/gmail-multi, override with
GMAIL_MULTI_HOME):

    credentials.json        OAuth "Desktop app" client from Google Cloud Console
    accounts.json           {"alias": {"email": "...", "read_only": false}, ...}
    tokens/<alias>.json     Refreshable OAuth token for that account
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def config_dir() -> Path:
    path = Path(os.environ.get("GMAIL_MULTI_HOME", Path.home() / ".config" / "gmail-multi"))
    path.mkdir(parents=True, exist_ok=True)
    (path / "tokens").mkdir(exist_ok=True)
    return path


def _private_write(path: Path, text: str) -> None:
    """Write a file readable only by the current user."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


@dataclass
class Account:
    alias: str
    email: str
    read_only: bool = False

    @property
    def scopes(self) -> list[str]:
        return [SCOPE_READONLY] if self.read_only else [SCOPE_MODIFY]

    @property
    def token_path(self) -> Path:
        return config_dir() / "tokens" / f"{self.alias}.json"


class AccountStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._creds: dict[str, Credentials] = {}

    # ---- registry -------------------------------------------------------
    @property
    def _registry_path(self) -> Path:
        return config_dir() / "accounts.json"

    def load(self) -> dict[str, Account]:
        if not self._registry_path.exists():
            return {}
        data = json.loads(self._registry_path.read_text())
        return {
            alias: Account(alias=alias, email=v["email"], read_only=v.get("read_only", False))
            for alias, v in data.items()
        }

    def _save(self, accounts: dict[str, Account]) -> None:
        data = {a.alias: {"email": a.email, "read_only": a.read_only} for a in accounts.values()}
        _private_write(self._registry_path, json.dumps(data, indent=2))

    def resolve(self, account: str) -> Account:
        """Find an account by alias or email address (case-insensitive)."""
        key = account.strip().lower()
        accounts = self.load()
        if key in accounts:
            return accounts[key]
        for acc in accounts.values():
            if acc.email.lower() == key:
                return acc
        known = ", ".join(f"{a.alias} <{a.email}>" for a in accounts.values()) or "none"
        raise ValueError(f"Unknown account '{account}'. Configured accounts: {known}")

    # ---- auth -----------------------------------------------------------
    def add(self, alias: str, read_only: bool = False, port: int = 0) -> Account:
        alias = alias.strip().lower()
        if not ALIAS_RE.match(alias):
            raise ValueError("Alias must be 1-32 chars: lowercase letters, digits, '-' or '_'.")
        client_file = config_dir() / "credentials.json"
        if not client_file.exists():
            raise FileNotFoundError(
                f"OAuth client file not found at {client_file}. "
                "Download a 'Desktop app' OAuth client JSON from Google Cloud Console "
                "and save it there (see README)."
            )
        acc = Account(alias=alias, email="", read_only=read_only)
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), acc.scopes)
        # prompt=select_account forces the Google account chooser so you can
        # pick a different account each time instead of the browser default.
        creds = flow.run_local_server(
            port=port, prompt="consent select_account", access_type="offline", open_browser=True
        )
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        acc.email = service.users().getProfile(userId="me").execute()["emailAddress"]

        accounts = self.load()
        for other in accounts.values():
            if other.email.lower() == acc.email.lower() and other.alias != alias:
                raise ValueError(f"{acc.email} is already configured as '{other.alias}'.")
        _private_write(acc.token_path, creds.to_json())
        accounts[alias] = acc
        self._save(accounts)
        return acc

    def remove(self, alias: str) -> Account:
        accounts = self.load()
        acc = accounts.pop(self.resolve(alias).alias)
        acc.token_path.unlink(missing_ok=True)
        self._save(accounts)
        self._creds.pop(acc.alias, None)
        return acc

    def service(self, acc: Account):
        """Return an authorized Gmail API client for the account.

        Credentials are cached; the API client is built per call because
        googleapiclient/httplib2 objects are not thread-safe and MCP runs
        sync tools in worker threads.
        """
        with self._lock:
            creds = self._creds.get(acc.alias)
            if creds is None:
                if not acc.token_path.exists():
                    raise RuntimeError(
                        f"No token for '{acc.alias}'. Run: gmail-multi add {acc.alias}"
                    )
                creds = Credentials.from_authorized_user_file(str(acc.token_path), acc.scopes)
            if not creds.valid:
                if not creds.refresh_token:
                    raise RuntimeError(
                        f"Token for '{acc.alias}' is invalid. Re-run: gmail-multi add {acc.alias}"
                    )
                try:
                    creds.refresh(Request())
                except Exception as e:
                    raise RuntimeError(
                        f"Could not refresh token for '{acc.alias}' ({e}). "
                        f"Re-run: gmail-multi add {acc.alias}"
                    ) from e
                _private_write(acc.token_path, creds.to_json())
            self._creds[acc.alias] = creds
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
