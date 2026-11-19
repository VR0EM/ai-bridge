"""Fake agy for tests. Behaviour depends on words in the prompt:
SLEEP<n>  wait n seconds first      FAIL     exit 1 without a result
PARTIAL   answer, then status ERROR  EMPTYERR status ERROR without an answer
It records its arguments, environment and cwd in $AI_BRIDGE_HOME/fake_agy.log.
"""
import json
import os
import re
import sys
import time
import uuid

args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
if os.environ.get("AI_BRIDGE_HOME"):
    with open(os.path.join(os.environ["AI_BRIDGE_HOME"], "fake_agy.log"), "a") as f:
        f.write(json.dumps({"args": args, "env": dict(os.environ), "cwd": os.getcwd()}) + "\n")
m = re.search(r"SLEEP(\d+)", prompt)
if m:
    time.sleep(int(m.group(1)))
if "FAIL" in prompt:
    print("simulated failure", file=sys.stderr)
    sys.exit(1)
conv = args[args.index("--conversation") + 1] if "--conversation" in args else str(uuid.uuid4())
status, response, error = "SUCCESS", "Fake answer\nVERDICT: APPROVE", None
if "PARTIAL" in prompt:
    status, error = "ERROR", "The stream was interrupted."
if "EMPTYERR" in prompt:
    status, response, error = "ERROR", "", "API error"
print(json.dumps({"event": "init", "conversation_id": conv}))
print(json.dumps({"event": "result", "result": {
    "conversation_id": conv, "status": status, "response": response,
    "error": error, "usage": {"total_tokens": 7}}}))
