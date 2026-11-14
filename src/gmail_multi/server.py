"""MCP server exposing several Gmail accounts to Claude."""

from __future__ import annotations

import base64
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated

from googleapiclient.errors import HttpError
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .accounts import AccountStore
from .gmail import build_raw, extract_attachments, full_message, headers_of, summarize

store = AccountStore()

mcp = MCPServer(
    name="gmail-multi",
    instructions=(
        "Access to several Gmail accounts. Call list_accounts first to learn the "
        "configured accounts. Every tool that touches mail takes an `account` "
        "argument (alias or email address). search_emails can search all "
        "accounts at once when `account` is omitted. Message and thread IDs are "
        "only valid within the account they came from. Always confirm with the "
        "user before sending email or trashing messages."
    ),
)

READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

AccountArg = Annotated[str, Field(description="Account alias or email address (see list_accounts).")]


def _explain(e: HttpError) -> str:
    return f"Gmail API error {e.resp.status}: {e.reason or e}"


def _writable(account: str):
    acc = store.resolve(account)
    if acc.read_only:
        raise ValueError(f"Account '{acc.alias}' was added read-only; this action is not allowed.")
    return acc


def _label_ids(svc, names: list[str] | None) -> list[str]:
    """Map label names (or IDs) to label IDs, case-insensitively."""
    if not names:
        return []
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    by_key = {}
    for lab in labels:
        by_key[lab["id"].lower()] = lab["id"]
        by_key[lab["name"].lower()] = lab["id"]
    out = []
    for n in names:
        if n.lower() not in by_key:
            raise ValueError(f"Label '{n}' not found. Use list_labels to see available labels.")
        out.append(by_key[n.lower()])
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=READ)
def list_accounts() -> list[dict]:
    """List the Gmail accounts this server can access."""
    return [
        {"alias": a.alias, "email": a.email, "read_only": a.read_only}
        for a in store.load().values()
    ]


def _search_one(alias: str, query: str, max_results: int, include_spam_trash: bool) -> dict:
    acc = store.resolve(alias)
    try:
        svc = store.service(acc)
        resp = (
            svc.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results, includeSpamTrash=include_spam_trash)
            .execute()
        )
        results = []
        for ref in resp.get("messages", []):
            msg = (
                svc.users()
                .messages()
                .get(userId="me", id=ref["id"], format="metadata",
                     metadataHeaders=["From", "To", "Cc", "Subject", "Date"])
                .execute()
            )
            results.append(summarize(msg, acc.alias))
        return {"account": acc.alias, "email": acc.email, "messages": results,
                "estimated_total": resp.get("resultSizeEstimate", 0)}
    except HttpError as e:
        return {"account": acc.alias, "email": acc.email, "error": _explain(e)}
    except Exception as e:  # keep other accounts' results if one fails
        return {"account": acc.alias, "email": acc.email, "error": str(e)}


@mcp.tool(annotations=READ)
def search_emails(
    query: Annotated[str, Field(description="Gmail search syntax, e.g. 'from:bob is:unread newer_than:7d'. Empty string = most recent.")] = "",
    account: Annotated[str | None, Field(description="Alias or email to search. Omit to search ALL accounts.")] = None,
    max_results: Annotated[int, Field(ge=1, le=100, description="Max messages per account.")] = 10,
    include_spam_trash: bool = False,
) -> list[dict]:
    """Search email in one account or across all accounts at once.

    Returns message summaries (sender, subject, date, snippet, IDs) grouped by account.
    """
    aliases = [store.resolve(account).alias] if account else list(store.load())
    if not aliases:
        raise ValueError("No accounts configured. Run `gmail-multi add <alias>` first.")
    with ThreadPoolExecutor(max_workers=min(8, len(aliases))) as pool:
        return list(pool.map(lambda a: _search_one(a, query, max_results, include_spam_trash), aliases))


@mcp.tool(annotations=READ)
def read_email(
    account: AccountArg,
    message_id: str,
    max_body_chars: Annotated[int, Field(ge=500, le=200_000)] = 20_000,
) -> dict:
    """Read the full content of one email (headers, plain-text body, attachment list)."""
    acc = store.resolve(account)
    try:
        msg = store.service(acc).users().messages().get(userId="me", id=message_id, format="full").execute()
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    return full_message(msg, acc.alias, max_body_chars)


@mcp.tool(annotations=READ)
def read_thread(
    account: AccountArg,
    thread_id: str,
    max_body_chars_per_message: Annotated[int, Field(ge=500, le=100_000)] = 8_000,
) -> dict:
    """Read every message in a conversation thread, oldest first."""
    acc = store.resolve(account)
    try:
        thread = store.service(acc).users().threads().get(userId="me", id=thread_id, format="full").execute()
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    return {
        "account": acc.alias,
        "thread_id": thread_id,
        "messages": [full_message(m, acc.alias, max_body_chars_per_message) for m in thread.get("messages", [])],
    }


def _compose(acc, svc, to, subject, body, cc, bcc, html, reply_to_message_id):
    thread_id = in_reply_to = references = None
    if reply_to_message_id:
        orig = svc.users().messages().get(
            userId="me", id=reply_to_message_id, format="metadata",
            metadataHeaders=["Subject", "Message-ID", "References", "From", "Reply-To"],
        ).execute()
        h = headers_of(orig["payload"])
        thread_id = orig["threadId"]
        in_reply_to = h.get("Message-ID")
        references = h.get("References")
        if not subject:
            s = h.get("Subject", "")
            subject = s if re.match(r"(?i)^re:", s) else f"Re: {s}"
        if not to:
            to = h.get("Reply-To") or h.get("From")
    if not to and not cc and not bcc:
        raise ValueError("At least one recipient (to/cc/bcc) is required.")
    raw = build_raw(sender=acc.email, to=to, subject=subject or "", body=body, cc=cc, bcc=bcc,
                    html_body=html, in_reply_to=in_reply_to, references=references)
    out = {"raw": raw}
    if thread_id:
        out["threadId"] = thread_id
    return out


@mcp.tool(annotations=WRITE)
def create_draft(
    account: Annotated[str, Field(description="Account to draft FROM (alias or email).")],
    body: str,
    to: Annotated[str | list[str] | None, Field(description="Recipient(s). Optional when replying.")] = None,
    subject: Annotated[str | None, Field(description="Optional when replying (defaults to 'Re: ...').")] = None,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    html: Annotated[bool, Field(description="Treat body as HTML.")] = False,
    reply_to_message_id: Annotated[str | None, Field(description="Message ID (same account) to reply to; keeps the thread.")] = None,
) -> dict:
    """Create a draft in the given account (not sent). Safest way to prepare an email."""
    acc = _writable(account)
    svc = store.service(acc)
    try:
        message = _compose(acc, svc, to, subject, body, cc, bcc, html, reply_to_message_id)
        draft = svc.users().drafts().create(userId="me", body={"message": message}).execute()
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    return {"account": acc.alias, "from": acc.email, "draft_id": draft["id"],
            "message_id": draft["message"]["id"], "status": "draft saved"}


@mcp.tool(annotations=DESTRUCTIVE)
def send_email(
    account: Annotated[str, Field(description="Account to send FROM (alias or email).")],
    body: str,
    to: Annotated[str | list[str] | None, Field(description="Recipient(s). Optional when replying.")] = None,
    subject: Annotated[str | None, Field(description="Optional when replying (defaults to 'Re: ...').")] = None,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    html: Annotated[bool, Field(description="Treat body as HTML.")] = False,
    reply_to_message_id: Annotated[str | None, Field(description="Message ID (same account) to reply to; keeps the thread.")] = None,
) -> dict:
    """Send an email immediately from the given account. Confirm with the user first."""
    acc = _writable(account)
    svc = store.service(acc)
    try:
        message = _compose(acc, svc, to, subject, body, cc, bcc, html, reply_to_message_id)
        sent = svc.users().messages().send(userId="me", body=message).execute()
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    return {"account": acc.alias, "from": acc.email, "message_id": sent["id"],
            "thread_id": sent.get("threadId"), "status": "sent"}


@mcp.tool(annotations=DESTRUCTIVE)
def send_draft(account: AccountArg, draft_id: str) -> dict:
    """Send an existing draft. Confirm with the user first."""
    acc = _writable(account)
    try:
        sent = store.service(acc).users().drafts().send(userId="me", body={"id": draft_id}).execute()
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    return {"account": acc.alias, "message_id": sent["id"], "thread_id": sent.get("threadId"), "status": "sent"}


@mcp.tool(annotations=READ)
def list_drafts(account: AccountArg, max_results: Annotated[int, Field(ge=1, le=100)] = 20) -> list[dict]:
    """List drafts in an account."""
    acc = store.resolve(account)
    svc = store.service(acc)
    try:
        drafts = svc.users().drafts().list(userId="me", maxResults=max_results).execute().get("drafts", [])
        out = []
        for d in drafts:
            full = svc.users().drafts().get(userId="me", id=d["id"], format="metadata").execute()
            out.append({"draft_id": d["id"], **summarize(full["message"], acc.alias)})
        return out
    except HttpError as e:
        raise ValueError(_explain(e)) from e


@mcp.tool(annotations=READ)
def list_labels(account: AccountArg) -> list[dict]:
    """List labels (system and user-created) in an account."""
    acc = store.resolve(account)
    try:
        labels = store.service(acc).users().labels().list(userId="me").execute().get("labels", [])
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    return sorted(({"id": l["id"], "name": l["name"], "type": l.get("type")} for l in labels),
                  key=lambda l: (l["type"] != "system", l["name"].lower()))


@mcp.tool(annotations=WRITE)
def modify_labels(
    account: AccountArg,
    message_ids: Annotated[list[str], Field(min_length=1, max_length=1000)],
    add_labels: Annotated[list[str] | None, Field(description="Label names or IDs to add, e.g. ['STARRED', 'Receipts'].")] = None,
    remove_labels: Annotated[list[str] | None, Field(description="Label names or IDs to remove. Remove 'UNREAD' to mark read, 'INBOX' to archive.")] = None,
) -> dict:
    """Add/remove labels on messages: mark read/unread, archive, star, categorize."""
    acc = _writable(account)
    svc = store.service(acc)
    try:
        body = {"ids": message_ids, "addLabelIds": _label_ids(svc, add_labels),
                "removeLabelIds": _label_ids(svc, remove_labels)}
        svc.users().messages().batchModify(userId="me", body=body).execute()
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    return {"account": acc.alias, "modified": len(message_ids), "added": add_labels or [],
            "removed": remove_labels or []}


@mcp.tool(annotations=DESTRUCTIVE)
def trash_email(account: AccountArg, message_id: str) -> dict:
    """Move a message to Trash (recoverable for 30 days). Confirm with the user first."""
    acc = _writable(account)
    try:
        store.service(acc).users().messages().trash(userId="me", id=message_id).execute()
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    return {"account": acc.alias, "message_id": message_id, "status": "trashed"}


@mcp.tool(annotations=WRITE)
def download_attachment(
    account: AccountArg,
    message_id: str,
    attachment_id: Annotated[str, Field(description="From read_email's attachments list.")],
    filename: Annotated[str | None, Field(description="File name to save as; defaults to the original name.")] = None,
) -> dict:
    """Save an attachment to the local downloads folder and return its path."""
    acc = store.resolve(account)
    svc = store.service(acc)
    try:
        if not filename:
            msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
            match = [a for a in extract_attachments(msg["payload"]) if a["attachment_id"] == attachment_id]
            filename = match[0]["filename"] if match else f"attachment-{message_id}"
        att = svc.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id).execute()
    except HttpError as e:
        raise ValueError(_explain(e)) from e
    data = base64.urlsafe_b64decode(att["data"] + "=" * (-len(att["data"]) % 4))
    out_dir = Path(os.environ.get("GMAIL_MULTI_DOWNLOADS", Path.home() / "Downloads" / "gmail-multi")) / acc.alias
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\- ()]+", "_", Path(filename).name).strip() or "attachment"
    path = out_dir / safe
    stem, suffix, n = path.stem, path.suffix, 1
    while path.exists():
        path = out_dir / f"{stem} ({n}){suffix}"
        n += 1
    path.write_bytes(data)
    return {"account": acc.alias, "saved_to": str(path), "size_bytes": len(data)}


def run() -> None:
    mcp.run("stdio")
