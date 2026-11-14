"""Offline tests: a fake Gmail service stands in for the Google API."""

import base64
import json
from email import message_from_bytes

import pytest

from gmail_multi import accounts, server
from gmail_multi.gmail import extract_body, html_to_text


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


MSG = {
    "id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "snippet": "Hi &amp; hello",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "From", "value": "Bob <bob@x.com>"}, {"name": "Subject", "value": "Lunch"},
                    {"name": "Message-ID", "value": "<abc@x.com>"}],
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>Hello <a href='https://e.com'>link</a></p><style>x{}</style>")}},
            {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"attachmentId": "att1", "size": 3}},
        ],
    },
}


class Call:
    def __init__(self, result, log=None, name=None, kw=None):
        self.result, self.log, self.name, self.kw = result, log, name, kw

    def execute(self):
        if self.log is not None:
            self.log.append((self.name, self.kw))
        return self.result


class FakeService:
    def __init__(self, email):
        self.email, self.log = email, []

    def users(self): return self
    def messages(self): return self
    def drafts(self): return self
    def labels(self): return self
    def threads(self): return self
    def attachments(self): return self

    def getProfile(self, **kw): return Call({"emailAddress": self.email})
    def list(self, **kw):
        if "q" in kw:
            return Call({"messages": [{"id": "m1"}], "resultSizeEstimate": 1})
        return Call({"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"},
                                {"id": "Label_1", "name": "Receipts", "type": "user"}]})
    def get(self, **kw):
        if "messageId" in kw:
            return Call({"data": base64.urlsafe_b64encode(b"PDF").decode()})
        return Call({**MSG, "messages": [MSG]})
    def send(self, **kw): return Call({"id": "sent1", "threadId": kw["body"].get("threadId")}, self.log, "send", kw)
    def create(self, **kw): return Call({"id": "d1", "message": {"id": "m9"}}, self.log, "draft", kw)
    def batchModify(self, **kw): return Call({}, self.log, "modify", kw)
    def trash(self, **kw): return Call({}, self.log, "trash", kw)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_MULTI_HOME", str(tmp_path))
    monkeypatch.setenv("GMAIL_MULTI_DOWNLOADS", str(tmp_path / "dl"))
    (tmp_path / "accounts.json").write_text(json.dumps({
        "personal": {"email": "me@gmail.com"},
        "work": {"email": "me@work.com"},
        "archive": {"email": "old@gmail.com", "read_only": True},
    }))
    fakes = {a: FakeService(e) for a, e in
             [("personal", "me@gmail.com"), ("work", "me@work.com"), ("archive", "old@gmail.com")]}
    monkeypatch.setattr(server.store, "service", lambda acc: fakes[acc.alias])
    return fakes


def test_resolve_by_alias_and_email(env):
    assert server.store.resolve("WORK").email == "me@work.com"
    assert server.store.resolve("me@gmail.com").alias == "personal"
    with pytest.raises(ValueError, match="Unknown account"):
        server.store.resolve("nope")


def test_search_all_accounts(env):
    res = server.search_emails("is:unread")
    assert [r["account"] for r in res] == ["personal", "work", "archive"]
    m = res[0]["messages"][0]
    assert m["subject"] == "Lunch" and m["unread"] and m["snippet"] == "Hi & hello"


def test_read_email_html_and_attachments(env):
    m = server.read_email("personal", "m1")
    assert "Hello link (https://e.com)" in m["body"] and "x{}" not in m["body"]
    assert m["attachments"][0]["filename"] == "a.pdf"


def test_reply_threads_and_sets_headers(env):
    out = server.send_email("work", body="Sure!", reply_to_message_id="m1")
    assert out["status"] == "sent" and out["thread_id"] == "t1"
    _, kw = env["work"].log[-1]
    mime = message_from_bytes(base64.urlsafe_b64decode(kw["body"]["raw"]))
    assert mime["To"] == "Bob <bob@x.com>" and mime["Subject"] == "Re: Lunch"
    assert mime["From"] == "me@work.com" and mime["In-Reply-To"] == "<abc@x.com>"


def test_draft_requires_recipient(env):
    with pytest.raises(ValueError, match="recipient"):
        server.create_draft("personal", body="x", subject="s")
    assert server.create_draft("personal", body="x", to=["a@b.com", "c@d.com"])["draft_id"] == "d1"


def test_read_only_account_blocks_writes(env):
    with pytest.raises(ValueError, match="read-only"):
        server.send_email("archive", body="x", to="a@b.com")
    with pytest.raises(ValueError, match="read-only"):
        server.trash_email("old@gmail.com", "m1")


def test_modify_labels_by_name(env):
    server.modify_labels("personal", ["m1"], add_labels=["receipts"], remove_labels=["inbox"])
    _, kw = env["personal"].log[-1]
    assert kw["body"]["addLabelIds"] == ["Label_1"] and kw["body"]["removeLabelIds"] == ["INBOX"]
    with pytest.raises(ValueError, match="not found"):
        server.modify_labels("personal", ["m1"], add_labels=["missing"])


def test_download_attachment(env, tmp_path):
    a = server.download_attachment("personal", "m1", "att1")
    b = server.download_attachment("personal", "m1", "att1")
    assert a["saved_to"].endswith("personal/a.pdf") and b["saved_to"].endswith("a (1).pdf")
    assert open(a["saved_to"], "rb").read() == b"PDF"


def test_html_to_text_nested_link():
    assert html_to_text("<a href='https://x.io'><b>Go</b></a>") == "Go (https://x.io)"
    assert extract_body({"mimeType": "text/plain", "body": {"data": b64("plain")}}) == "plain"
