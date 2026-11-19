"""Fake `claude` (Claude Code) for tests. Words in the question:
SLEEP<n> wait first   CFAIL exit 1 without output
Records args/env/cwd in $AI_BRIDGE_HOME/fake_claude.log.
"""
import json
import os
import re
import sys
import time
import uuid

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("2.1.321 (Claude Code)")
    sys.exit(0)
if args[:1] == ["mcp"]:
    path = os.path.join(os.environ["HOME"], ".claude.json")
    data = json.load(open(path)) if os.path.exists(path) else {}
    servers = data.setdefault("mcpServers", {})
    if args[1] == "add-json":
        servers[args[2]] = json.loads(args[3])
    elif args[1] == "remove":
        if servers.pop(args[2], None) is None:
            sys.exit(1)
    json.dump(data, open(path, "w"))
    sys.exit(0)
question = args[-1]
if os.environ.get("AI_BRIDGE_HOME"):
    with open(os.path.join(os.environ["AI_BRIDGE_HOME"], "fake_claude.log"), "a") as f:
        f.write(json.dumps({"args": args, "env": dict(os.environ), "cwd": os.getcwd()}) + "\n")
m = re.search(r"SLEEP(\d+)", question)
if m:
    time.sleep(int(m.group(1)))
if "CFAIL" in question:
    print("boom", file=sys.stderr)
    sys.exit(1)
sid = args[args.index("--resume") + 1] if "--resume" in args else str(uuid.uuid4())
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "OK" if "Reply with exactly: OK" in question else "Claude says: looks fine.", "session_id": sid, "num_turns": 1}))
