"""Commando 'bridge' voor Dali: on, off, status, note, tail."""
from __future__ import annotations

import argparse
import time

from . import store


def cmd_status(_args) -> None:
    state = "AAN" if store.is_enabled() else "UIT"
    count = len(store._read_all())
    print(f"Bridge: {state}")
    print(f"Map:    {store.HOME}")
    print(f"Log:    {count} berichten")


def cmd_on(_args) -> None:
    store.set_enabled(True)
    print("Bridge staat AAN.")


def cmd_off(_args) -> None:
    store.set_enabled(False)
    print("Bridge staat UIT. Alle bridge-tools weigeren nu.")


def cmd_note(args) -> None:
    entry = store.append("dali", " ".join(args.text), args.thread)
    print(f"Opgeslagen als bericht #{entry['id']}.")


def cmd_tail(args) -> None:
    entries = store._read_all()
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
            new = [e for e in store._read_all() if e["id"] > last_id]
            if args.thread:
                new = [e for e in new if e.get("thread") == args.thread]
            if new:
                print(store.format_entries(new), flush=True)
                last_id = new[-1]["id"]
    except KeyboardInterrupt:
        pass


def main() -> None:
    p = argparse.ArgumentParser(prog="bridge", description="Beheer de Claude-Gemini bridge.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="toon de status").set_defaults(func=cmd_status)
    sub.add_parser("on", help="zet de bridge aan").set_defaults(func=cmd_on)
    sub.add_parser("off", help="zet de bridge uit").set_defaults(func=cmd_off)
    n = sub.add_parser("note", help="schrijf zelf een bericht in het logboek")
    n.add_argument("text", nargs="+")
    n.add_argument("-t", "--thread")
    n.set_defaults(func=cmd_note)
    t = sub.add_parser("tail", help="toon de laatste berichten")
    t.add_argument("-n", type=int, default=20)
    t.add_argument("-f", "--follow", action="store_true", help="blijf live volgen")
    t.add_argument("-t", "--thread")
    t.set_defaults(func=cmd_tail)
    args = p.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        raise SystemExit(f"Fout: {e}")


if __name__ == "__main__":
    main()
