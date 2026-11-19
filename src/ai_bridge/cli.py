"""The `bridge` command: on, off, status, note, tail, jobs, result, cancel."""
from __future__ import annotations

import argparse
import time

from . import jobs, store


def cmd_status(_args) -> None:
    state = "ON" if store.is_enabled() else "OFF"
    active = [j for j in jobs.list_jobs(200) if j["status"] in jobs.ACTIVE]
    print(f"Bridge: {state}")
    print(f"Data:   {store.HOME}")
    print(f"Log:    {len(store.read_all())} messages")
    print(f"Jobs:   {len(active)} queued or running")


def cmd_on(_args) -> None:
    store.set_enabled(True)
    print("Bridge is ON.")


def cmd_off(_args) -> None:
    store.set_enabled(False)
    stopped = jobs.cancel_all("Bridge was switched off.")
    print("Bridge is OFF. Agents can no longer use the bridge.")
    if stopped:
        print(f"Stopped {len(stopped)} running or queued Gemini job(s).")


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


def cmd_jobs(args) -> None:
    found = jobs.list_jobs(args.n)
    print("\n".join(jobs.summary_line(j) for j in found) if found else "No jobs yet.")


def cmd_result(args) -> None:
    job, text = jobs.result(args.job_id)
    print(jobs.summary_line(job))
    for key in ("warning", "error"):
        if job.get(key):
            print(f"{key}: {job[key]}")
    if text:
        print()
        print(text)


def cmd_cancel(args) -> None:
    print(jobs.summary_line(jobs.cancel(args.job_id)))


def main() -> None:
    store.ensure_home()
    p = argparse.ArgumentParser(prog="bridge", description="Manage the Claude-Gemini bridge.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show status").set_defaults(func=cmd_status)
    sub.add_parser("on", help="turn the bridge on").set_defaults(func=cmd_on)
    sub.add_parser("off", help="turn the bridge off and stop all Gemini jobs").set_defaults(func=cmd_off)
    n = sub.add_parser("note", help="write a message to the log yourself")
    n.add_argument("text", nargs="+")
    n.add_argument("-t", "--thread")
    n.set_defaults(func=cmd_note)
    t = sub.add_parser("tail", help="show the latest messages")
    t.add_argument("-n", type=int, default=20)
    t.add_argument("-f", "--follow", action="store_true", help="keep following live")
    t.add_argument("-t", "--thread")
    t.set_defaults(func=cmd_tail)
    j = sub.add_parser("jobs", help="list Gemini jobs")
    j.add_argument("-n", type=int, default=20)
    j.set_defaults(func=cmd_jobs)
    r = sub.add_parser("result", help="show the result of a Gemini job")
    r.add_argument("job_id")
    r.set_defaults(func=cmd_result)
    c = sub.add_parser("cancel", help="cancel a Gemini job")
    c.add_argument("job_id")
    c.set_defaults(func=cmd_cancel)
    args = p.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        raise SystemExit(f"Error: {e}")


if __name__ == "__main__":
    main()
