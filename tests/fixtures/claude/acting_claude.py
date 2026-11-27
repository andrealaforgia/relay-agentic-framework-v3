#!/usr/bin/env python3
"""A stand-in for the Claude CLI that acts a role: it reads its briefing, finds the first
line of its script whose words are in it, and gives the next reply scripted for that.

The script is `script.json` beside this file: {"words of a briefing": [reply, ...]}. A
reply {"__files__": {name: content}, "reply": {...}} first writes those files in the
working directory, as a Builder would. How many replies each line has given is kept in
`script.state.json`, under a lock: several of these run at once.

It says what the real CLI says about itself when it starts (its session, its tools, no
servers, its version), so that what Relay checks of a run is checked of this one.
"""

import fcntl
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION = "9.9.9"
FLAGS = """
  --agent <agent>  --agents <json>  --model <model>  --effort <level>  --json-schema <schema>
  --session-id <uuid>  --tools <tools...>  --settings <file-or-json>  --strict-mcp-config
  --disable-slash-commands  --permission-prompts <target>  --setting-sources <sources>
  --no-session-persistence  --max-budget-usd <amount>  --output-format <format>  --verbose
  -p, --print  --restricted
"""

if "--version" in sys.argv:
    print(f"{VERSION} (acting)")
    sys.exit(0)
if "--help" in sys.argv:
    print(FLAGS)
    sys.exit(0)


def following(flag: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else ""


briefing = sys.stdin.read()
session, model = following("--session-id"), following("--model")
tools = [each for each in following("--tools").split(",") if each]
print(
    json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session,
            "model": model,
            "tools": [*tools, "StructuredOutput"],
            "mcp_servers": [],
            "claude_code_version": VERSION,
        }
    ),
    flush=True,
)
with open(HERE / "script.lock", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    script = json.loads((HERE / "script.json").read_text())
    state_file = HERE / "script.state.json"
    given = json.loads(state_file.read_text()) if state_file.exists() else {}
    line = next((each for each in script if each in briefing), None)
    if line is None or given.get(line, 0) >= len(script[line]):
        sys.stderr.write(f"nothing more scripted for: {briefing[:160]!r}\n")
        sys.exit(3)
    reply = script[line][given.get(line, 0)]
    given[line] = given.get(line, 0) + 1
    state_file.write_text(json.dumps(given))
if isinstance(reply, dict) and "__files__" in reply:
    for name, content in reply["__files__"].items():
        Path(name).write_text(content)
    reply = reply["reply"]
print(
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": session,
            "result": "",
            "structured_output": reply,
            "total_cost_usd": 0.001,
            "modelUsage": {
                model: {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                    "cacheCreationEphemeral5mInputTokens": 0,
                    "cacheCreationEphemeral1hInputTokens": 0,
                }
            },
        }
    ),
    flush=True,
)
