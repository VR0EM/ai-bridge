"""The `bridge` command: on, off, status, note, tail."""
from __future__ import annotations

import argparse
import time

from . import store


def cmd_status(_args) -> None:
    state = "ON" if store.is_enabled() else "OFF"
    print(f"Bridge: {state}")
    print(f"Data:   {store.HOME}")
    print(f"Log:    {len(store.read_all())} messages")


def cmd_on(_args) -> None:
    store.set_enabled(True)
    print("Bridge is ON.")


def cmd_off(_args) -> None:
    store.set_enabled(False)
    print("Bridge is OFF. All bridge tools now refuse.")


def cmd_note(args) -> None:
    entry = store.append("human", " ".join(args.text), args.thread)
    print(f"Saved as message #{entry['id']}.")


def cmd_tail(args) -> None:
    entries = store.read_all()
    if args.thread:
        entries = [e for e in entries if e.get("thread") == args.thread]
    last_id = 0
    if entries:
        print(store.format_entries(entries[-args.n:]))
        last_id = entries[-1]["id"]
    if not args.follow:
        return
    try:
        while True:
            time.sleep(1)
            new = [e for e in store.read_all() if e["id"] > last_id]
            if args.thread:
                new = [e for e in new if e.get("thread") == args.thread]
            if new:
                print(store.format_entries(new), flush=True)
                last_id = new[-1]["id"]
    except KeyboardInterrupt:
        pass


def main() -> None:
    p = argparse.ArgumentParser(prog="bridge", description="Manage the Claude-Gemini bridge.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show status").set_defaults(func=cmd_status)
    sub.add_parser("on", help="turn the bridge on").set_defaults(func=cmd_on)
    sub.add_parser("off", help="turn the bridge off").set_defaults(func=cmd_off)
    n = sub.add_parser("note", help="write a message to the log yourself")
    n.add_argument("text", nargs="+")
    n.add_argument("-t", "--thread")
    n.set_defaults(func=cmd_note)
    t = sub.add_parser("tail", help="show the latest messages")
    t.add_argument("-n", type=int, default=20)
    t.add_argument("-f", "--follow", action="store_true", help="keep following live")
    t.add_argument("-t", "--thread")
    t.set_defaults(func=cmd_tail)
    args = p.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        raise SystemExit(f"Error: {e}")


if __name__ == "__main__":
    main()
