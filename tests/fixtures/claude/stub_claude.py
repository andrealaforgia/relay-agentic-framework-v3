#!/usr/bin/env python3
"""A stand-in for the Claude CLI. It records how it was called and replays a stream
recorded from a real run. What it does is set through its environment:

STUB_RECORD   a file to write the call to (arguments, standard input, directory, environment)
STUB_STREAM   a file whose lines are printed as the run's output
STUB_HANG     if set, never finish after printing the stream
STUB_EXIT     the exit status (default 0)
STUB_HELP     a file to print for --help, in place of the flags Relay asks for
STUB_STDERR   text to write to standard error
"""

import json
import os
import sys
import time
from pathlib import Path

FLAGS = """
  --agent <agent>  --agents <json>  --model <model>  --effort <level>  --json-schema <schema>
  --session-id <uuid>  --tools <tools...>  --settings <file-or-json>  --strict-mcp-config
  --disable-slash-commands  --permission-prompts <target>  --setting-sources <sources>
  --no-session-persistence  --max-budget-usd <amount>  --output-format <format>  --verbose
  -p, --print  --restricted
"""

if "--version" in sys.argv:
    print("9.9.9 (stub)")
    sys.exit(0)
if "--help" in sys.argv:
    helped = os.environ.get("STUB_HELP")
    print(Path(helped).read_text() if helped else FLAGS)
    sys.exit(0)

said = sys.stdin.read()
if os.environ.get("STUB_RECORD"):
    with open(os.environ["STUB_RECORD"], "w") as record:
        json.dump(
            {"argv": sys.argv[1:], "stdin": said, "cwd": os.getcwd(), "env": dict(os.environ)},
            record,
        )
if os.environ.get("STUB_STDERR"):
    sys.stderr.write(os.environ["STUB_STDERR"])
if os.environ.get("STUB_STREAM"):
    with open(os.environ["STUB_STREAM"]) as stream:
        for line in stream:
            sys.stdout.write(line)
            sys.stdout.flush()
if os.environ.get("STUB_HANG"):
    # A child of its own, as a real run has: killing the stub alone would leave it.
    if os.fork() == 0:
        time.sleep(600)
        os._exit(0)
    time.sleep(600)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
