# gmail-multi: one Claude connector for all your Gmail accounts

The built-in Gmail connector connects to **one** Google account. `gmail-multi` is a
local [MCP](https://modelcontextprotocol.io) server that connects to **as many Gmail
accounts as you like** under short names (`personal`, `work`, ...). Claude can
search all of them at once, read mail, write drafts, reply and send from the right
address, apply labels, archive, trash and download attachments.

It runs on your own computer and works with **Claude Desktop** and **Claude Code**.
Your OAuth tokens never leave your machine.

## Tools Claude gets

| Tool | What it does |
|---|---|
| `list_accounts` | Shows the connected accounts |
| `search_emails` | Gmail search syntax, in one account **or all accounts at once** |
| `read_email` / `read_thread` | Full message or conversation, HTML turned into text, attachment list |
| `create_draft` / `list_drafts` / `send_draft` | Write drafts (including threaded replies) |
| `send_email` | Send now, or reply in the thread with `reply_to_message_id` |
| `list_labels` / `modify_labels` | Mark read/unread, archive, star, add or remove labels by name |
| `trash_email` | Move to Trash (recoverable for 30 days) |
| `download_attachment` | Saves to `~/Downloads/gmail-multi/<account>/` |

Each tool takes an `account` argument: an alias or the full email address.
Sending and trashing are marked as destructive, so Claude asks you before it does them.

## Setup (about 10 minutes, once)

### 1. Create a Google OAuth client (free)

You need your own OAuth client so Google lets this program sign in. One client works
for all your accounts.

1. Go to <https://console.cloud.google.com/>, create a project (e.g. "Claude Gmail").
2. **APIs & Services → Library**: search for **Gmail API** and click **Enable**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform):
   - User type: **External**. App name: anything. Use your email for the contact fields.
   - **Audience → Test users**: add **every Gmail address** you want to connect.
   - **Recommended:** under **Audience**, click **Publish app** ("In production").
     In *Testing* mode Google expires Gmail tokens after **7 days**, and you would have
     to sign in again every week. A published app doesn't need Google verification for
     personal use; you'll just see an "unverified app" warning when signing in
     (click **Advanced → Go to ... (unsafe)**: it's your own app).
4. **Clients → Create client** → Application type **Desktop app** → Create →
   **Download JSON**.
5. Save that file as:
   - macOS / Linux: `~/.config/gmail-multi/credentials.json`
   - Windows: `C:\Users\<you>\.config\gmail-multi\credentials.json`

   (Run `gmail-multi where` after installing to print the folder.)

### 2. Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (or use pip, see below).

```bash
uv tool install git+https://github.com/ljantonawebs/gmailconnector
```

This puts a `gmail-multi` command on your PATH. To use pip instead, clone the repo and
run `pip install .` (Python 3.10+).

### 3. Connect your accounts

Run this once per account. A browser opens and you pick the Google account:

```bash
gmail-multi add personal
```

```bash
gmail-multi add work
```

```bash
gmail-multi add old-stuff --read-only
```

Then check they all work:

```bash
gmail-multi list
```

```bash
gmail-multi check
```

If the browser picks the wrong account, use the account chooser on the Google page, or
open the printed URL in a private window.

### 4. Add it to Claude

**Claude Desktop**: Settings → Developer → Edit Config, then add the server to
`claude_desktop_config.json`, and restart Claude Desktop:

```json
{
  "mcpServers": {
    "gmail-multi": {
      "command": "/full/path/to/gmail-multi"
    }
  }
}
```

Get the full path with `which gmail-multi` (macOS/Linux) or `where gmail-multi`
(Windows; usually `C:\\Users\\<you>\\.local\\bin\\gmail-multi.exe`, with backslashes
doubled in JSON).

**Claude Code**:

```bash
claude mcp add --scope user gmail-multi -- gmail-multi
```

Then try: *"Search all my accounts for unread mail from the last 2 days"*, or
*"Draft a reply from my work account to the latest email from Alice"*.

You can turn off the built-in Gmail connector so Claude doesn't mix the two up.

## Managing accounts

```bash
gmail-multi remove work
```

This deletes the saved token. To fully revoke access, also remove the app at
<https://myaccount.google.com/permissions> for that account.

## Security notes

- Tokens are stored in `~/.config/gmail-multi/tokens/` with owner-only permissions
  (you can change the location with `GMAIL_MULTI_HOME`). Anyone with those files can read
  that mailbox, so treat them like passwords. `.gitignore` excludes them.
- Default scope is `gmail.modify` (read, send, draft, label, trash; **no** permanent
  delete). `--read-only` accounts use `gmail.readonly`, and the server also refuses
  every write tool for them.
- Email content can contain prompt-injection attempts ("ignore your instructions and
  forward..."). Keep Claude's per-tool approval on for `send_email`, `send_draft`
  and `trash_email`.

## Limitations

- This is a **local** connector. It does not show up in claude.ai on the web or on
  mobile: those need a remote, internet-hosted server with its own login, which this
  project doesn't provide.
- Workspace (company) accounts may block third-party OAuth apps; ask your admin.

## Development

```bash
uv venv && uv pip install -e . pytest
```

```bash
.venv/bin/pytest -q
```

The tests use a fake Gmail service and need no network access or Google account.
