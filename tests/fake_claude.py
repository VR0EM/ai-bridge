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
                  "result": "Claude says: looks fine.", "session_id": sid, "num_turns": 1}))
