"""Helpers for turning Gmail API payloads into readable data and back."""

from __future__ import annotations

import base64
import html
import re
from email.message import EmailMessage
from email.utils import getaddresses
from html.parser import HTMLParser

SUMMARY_HEADERS = ["From", "To", "Cc", "Subject", "Date"]


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def headers_of(payload: dict) -> dict[str, str]:
    return {h["name"]: h["value"] for h in payload.get("headers", [])}


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head"}
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.startswith("http") else None

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href:
            self.parts.append(f" ({self._href})")
            self._href = None

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        text = "".join(parser.parts)
    except Exception:
        text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _walk(part: dict):
    yield part
    for sub in part.get("parts", []) or []:
        yield from _walk(sub)


def extract_body(payload: dict) -> str:
    plain, rich = [], []
    for part in _walk(payload):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if not data or part.get("filename"):
            continue
        text = _b64decode(data).decode("utf-8", errors="replace")
        if mime == "text/plain":
            plain.append(text)
        elif mime == "text/html":
            rich.append(text)
    if plain:
        return "\n".join(plain).strip()
    if rich:
        return html_to_text("\n".join(rich))
    return ""


def extract_attachments(payload: dict) -> list[dict]:
    out = []
    for part in _walk(payload):
        body = part.get("body", {})
        if part.get("filename") and body.get("attachmentId"):
            out.append(
                {
                    "filename": part["filename"],
                    "mime_type": part.get("mimeType"),
                    "size_bytes": body.get("size"),
                    "attachment_id": body["attachmentId"],
                }
            )
    return out


def summarize(msg: dict, account: str) -> dict:
    h = headers_of(msg.get("payload", {}))
    return {
        "account": account,
        "message_id": msg["id"],
        "thread_id": msg.get("threadId"),
        **{k.lower(): h[k] for k in SUMMARY_HEADERS if k in h},
        "snippet": html.unescape(msg.get("snippet", "")),
        "labels": msg.get("labelIds", []),
        "unread": "UNREAD" in msg.get("labelIds", []),
    }


def full_message(msg: dict, account: str, max_body_chars: int) -> dict:
    payload = msg.get("payload", {})
    body = extract_body(payload)
    truncated = len(body) > max_body_chars
    data = summarize(msg, account)
    data.pop("snippet", None)
    h = headers_of(payload)
    for extra in ("Bcc", "Reply-To", "Message-ID", "List-Unsubscribe"):
        if extra in h:
            data[extra.lower().replace("-", "_")] = h[extra]
    data["body"] = body[:max_body_chars] + ("\n\n[...truncated]" if truncated else "")
    data["attachments"] = extract_attachments(payload)
    return data


def split_addresses(value: str | list[str] | None) -> str | None:
    if not value:
        return None
    if isinstance(value, list):
        value = ", ".join(value)
    pairs = getaddresses([value])
    if not any(addr for _, addr in pairs):
        return None
    return ", ".join(f"{n} <{a}>" if n else a for n, a in pairs if a)


def build_raw(
    *,
    sender: str,
    to: str | list[str] | None,
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    html_body: bool = False,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> str:
    msg = EmailMessage()
    msg["From"] = sender
    if (v := split_addresses(to)):
        msg["To"] = v
    if (v := split_addresses(cc)):
        msg["Cc"] = v
    if (v := split_addresses(bcc)):
        msg["Bcc"] = v
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip() if references else in_reply_to
    if html_body:
        msg.set_content(html_to_text(body))
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()
