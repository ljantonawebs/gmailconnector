"""Command-line entry point: manage accounts and run the MCP server."""

from __future__ import annotations

import argparse
import sys

from .accounts import AccountStore, config_dir


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="gmail-multi", description="Multi-account Gmail MCP server for Claude."
    )
    sub = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add", help="Sign in to a Gmail account and save it under an alias.")
    p_add.add_argument("alias", help="Short name, e.g. personal, work, side-project")
    p_add.add_argument("--read-only", action="store_true", help="Request read-only access.")
    p_add.add_argument("--port", type=int, default=0, help="Local port for the OAuth redirect.")

    sub.add_parser("list", help="List configured accounts.")
    p_rm = sub.add_parser("remove", help="Forget an account and delete its token.")
    p_rm.add_argument("alias")
    sub.add_parser("check", help="Verify every account's token works.")
    sub.add_parser("serve", help="Run the MCP server over stdio (what Claude launches).")
    sub.add_parser("where", help="Print the config directory.")

    args = parser.parse_args(argv)
    store = AccountStore()

    # Running with no subcommand = serve, so MCP configs can just call `gmail-multi`.
    if args.cmd in (None, "serve"):
        from .server import run

        run()
        return

    try:
        if args.cmd == "add":
            print("Opening your browser - choose the Google account to connect...")
            acc = store.add(args.alias, read_only=args.read_only, port=args.port)
            mode = "read-only" if acc.read_only else "read/write"
            print(f"Added '{acc.alias}' -> {acc.email} ({mode})")
        elif args.cmd == "list":
            accounts = store.load()
            if not accounts:
                print("No accounts yet. Run: gmail-multi add <alias>")
            for a in accounts.values():
                print(f"{a.alias:16} {a.email}{'  (read-only)' if a.read_only else ''}")
        elif args.cmd == "remove":
            acc = store.remove(args.alias)
            print(f"Removed '{acc.alias}' ({acc.email}). You can also revoke access at "
                  "https://myaccount.google.com/permissions")
        elif args.cmd == "check":
            ok = True
            for a in store.load().values():
                try:
                    email = store.service(a).users().getProfile(userId="me").execute()["emailAddress"]
                    print(f"OK    {a.alias:16} {email}")
                except Exception as e:  # noqa: BLE001
                    ok = False
                    print(f"FAIL  {a.alias:16} {e}")
            sys.exit(0 if ok else 1)
        elif args.cmd == "where":
            print(config_dir())
    except (ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
