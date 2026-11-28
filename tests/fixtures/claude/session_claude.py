"""A stand-in for the Claude CLI in a session: it starts the server it is given, as the
real CLI does, says what it started with, and plays a script against that server.

Everything it keeps is in the directory `SESSION_STANDIN` names:

- `script.json`: a list of rules. The first whose `when` is in what it was told is played:
  `calls` (tools of the server to call, in order, with their arguments), `say` (its reply,
  where `{results}` is what the calls returned and `{turns}` how many turns this provider
  session has had), and optionally `extra_tool` (a tool it was not given, reported at the
  start), `missing_tool` (one of the server's it starts without), `say_first` (something
  it says before it has said what it started with), `vanish` (it ends without a word),
  `refuse` (it ends in an error before it starts, `saying` something, at a `cost`),
  `no_start` (it never says what it started with), `start_as` and `end_as` (it starts, or
  ends, as another session than the one it was asked to be), `no_server` (it starts without the server), `lose_result` (it ends without a
  result) or `fail` (its result is an error).
- `sessions/<id>.json`: what each provider session was told, turn by turn. A session that
  is not there cannot be resumed, as with the real CLI.
- `launched.jsonl`: what each run was launched with and told.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ["SESSION_STANDIN"])
VERSION = "9.9.9"

if "--version" in sys.argv:
    print(f"{VERSION} (session stand-in)")
    sys.exit(0)


def following(flag: str) -> str:
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else ""


resumed = "--resume" in sys.argv
session = following("--resume") if resumed else following("--session-id")
model = following("--model")
kept = HOME / "sessions" / f"{session}.json"
if resumed and not kept.exists():
    # As the real CLI does: an error and nothing else, before anything is started.
    with open(HOME / "launched.jsonl", "a") as launched:
        launched.write(
            json.dumps({"argv": sys.argv[1:], "told": None, "session": session, "resumed": True})
            + "\n"
        )
    print(
        json.dumps(
            {
                # As CLI 2.1.332 says it, with its counters of nought.
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "num_turns": 0,
                "session_id": session,
                "total_cost_usd": 0,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "modelUsage": {},
                "errors": [f"No conversation found with session ID: {session}"],
            }
        ),
        flush=True,
    )
    sys.exit(1)

told = sys.stdin.read()
script = json.loads((HOME / "script.json").read_text())
# A rule is chosen by what the person said: the last part of what it was told, when it
# was told Relay's record first.
asked = told.rsplit("The Product Owner now says:\n\n", 1)[-1]
rule = next((each for each in script if each["when"] in asked), {"say": "I have nothing to add."})
history = json.loads(kept.read_text()) if kept.exists() else []
history.append(told)
with open(HOME / "launched.jsonl", "a") as launched:
    launched.write(
        json.dumps({"argv": sys.argv[1:], "told": told, "session": session, "resumed": resumed})
        + "\n"
    )

if rule.get("vanish"):
    sys.exit(0)  # launched, and gone without a word
if "refuse" in rule:
    # An error before anything is started, as the CLI gives one: with what it says is
    # wrong, and what it says it cost.
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "num_turns": 0,
                "session_id": session,
                "total_cost_usd": rule["refuse"].get("cost", 0),
                "modelUsage": {},
                "errors": [rule["refuse"]["saying"]],
            }
        ),
        flush=True,
    )
    sys.exit(1)
configured = json.loads(Path(following("--mcp-config")).read_text())["mcpServers"]
[(name, how)] = configured.items()
server = subprocess.Popen(
    [how["command"], *how["args"]],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    env={**os.environ, **how.get("env", {})},
    text=True,
)
sent = 0


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def ask(method: str, **params: object) -> dict:
    global sent
    sent += 1
    assert server.stdin is not None and server.stdout is not None
    server.stdin.write(
        json.dumps({"jsonrpc": "2.0", "id": sent, "method": method, "params": params}) + "\n"
    )
    server.stdin.flush()
    return json.loads(server.stdout.readline())


ask("initialize", protocolVersion="2025-06-18", capabilities={}, clientInfo={"name": "stand-in"})
offered = [
    each["name"]
    for each in ask("tools/list")["result"]["tools"]
    if each["name"] != rule.get("missing_tool")
]
if "say_first" in rule:
    emit(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": rule["say_first"]}]}}
    )
if not rule.get("no_start"):
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": rule.get("start_as", session),
            "model": model,
            "tools": [f"mcp__{name}__{each}" for each in offered]
            + ([rule["extra_tool"]] if "extra_tool" in rule else []),
            "mcp_servers": [] if rule.get("no_server") else [{"name": name, "status": "connected"}],
            "claude_code_version": VERSION,
        }
    )
results = []
for number, each in enumerate(rule.get("calls", [])):
    emit(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"call-{number}",
                        "name": f"mcp__{name}__{each['tool']}",
                        "input": each.get("arguments", {}),
                    }
                ]
            },
        }
    )
    reply = ask("tools/call", name=each["tool"], arguments=each.get("arguments", {}))
    result = reply.get("result") or {"isError": True, "content": [{"text": json.dumps(reply)}]}
    text = result["content"][0]["text"]
    results.append(text)
    emit(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"call-{number}",
                        "is_error": result.get("isError", False),
                        "content": text,
                    }
                ]
            },
        }
    )
said = rule["say"].replace("{results}", "\n".join(results)).replace("{turns}", str(len(history)))
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": said}]}})
server.stdin.close()
server.wait(timeout=20)
if rule.get("lose_result"):
    sys.exit(0)
kept.parent.mkdir(parents=True, exist_ok=True)
kept.write_text(json.dumps(history))
emit(
    {
        "type": "result",
        "subtype": "error_during_execution" if rule.get("fail") else "success",
        "is_error": bool(rule.get("fail")),
        "session_id": rule.get("end_as", session),
        "result": said,
        "total_cost_usd": 0.002,
        "modelUsage": {
            model: {
                "inputTokens": 100,
                "outputTokens": 20,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "cacheCreationEphemeral5mInputTokens": 0,
                "cacheCreationEphemeral1hInputTokens": 0,
                "costUSD": 0.002,
            }
        },
    }
)
