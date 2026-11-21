# What the real Claude CLI enforces: probes of 2026-11-21

Provenance: run by Claude (Fable 5.2) against Claude Code 2.1.330 on macOS (Seatbelt), with the cheapest model, four runs, about one cent in all. This is evidence gathered before step 2 of slice 3b was designed in detail. It is not the confinement suite: nothing here is automated, and each observation is one run. The suite that step 2 builds repeats these as checks.

Every run was launched as the adapter launches one: `-p`, `--tools <list>`, `--settings <json>`, `--setting-sources ""`, `--strict-mcp-config`, `--disable-slash-commands`, `--permission-prompts none`, `--no-session-persistence`, a session id chosen beforehand. What is reported is what the stream showed (the tool call made and the result it got) and what was then on disk, not what the model said about it.

## What held

| Claim | Observed |
|---|---|
| `--tools` is the whole inventory | The start event listed exactly the tools asked for, no connected services and no skills |
| File tools refuse reads outside the working directory, with `permissions.blockReadsOutsideWorkingDirectories` | `Read` and `Grep` of a file outside were refused; a file inside was read |
| The `Write` tool refuses a path outside | Refused: "no approval surface" |
| A shell command has no network, with `sandbox.network.allowedDomains: []` | `curl` to a public site failed at the sandbox proxy |
| Nothing is loaded from the working directory, with `--setting-sources ""` | A `CLAUDE.md` there carrying an instruction had no effect in two runs; a `.claude/settings.json` there allowing everything changed nothing |
| A shell command cannot read what `sandbox.filesystem.denyRead` names | `cat` of the home directory, `~/.ssh` and a sibling of the workspace: "Operation not permitted" |

## What did not hold as I had assumed

1. **`denyWrite: ["/"]` with `allowWrite: ["."]` blocks the workspace too.** Deny wins. With it, the builder could not write inside its own workspace, and `python3` could not start.
2. **A rule that works for the builder's shell**: `denyRead: ["~/", <the workspace's parent>]` with `allowRead: [<the workspace>]`, and no write rules at all. Reads inside work, reads of the home directory and of siblings are refused, and writes outside the workspace are refused by the sandbox's own default. `allowRead` does re-allow inside a denied region; `allowWrite` did not, against a denied root.
3. **The sandbox does not withhold the environment.** A variable set in the launching process was printed by `printenv` in the sandboxed shell. What the run's environment contains is decided only by what Relay passes.
4. **Every sandboxed shell can write to one shared temporary directory** (`/tmp/claude-<uid>`), and can read it unless it is denied. Two builder runs can therefore leave things for each other there. A reviewer has no shell, and its file tools are confined to its own tree.
5. **System files outside the home directory are readable** by a sandboxed shell (`/etc/hosts`).
6. **`git` does not run in the sandboxed shell** once the home directory is denied: it cannot read `~/.gitconfig`. The builder is not meant to use Git (the supervisor builds the commit), so this is acceptable, and a reason not to tell the builder to commit.
7. **The start event names a per-directory memory location** under the user's `~/.claude/projects`. Nothing was read from it in these runs (it was empty), and the file tools cannot reach it. Whether the CLI would load memory from it into a later run in the same directory was not tested; workspaces are fresh directories, so the location is new each time.

## What this decides for step 2

- Workspaces live under a root of Relay's own, outside the home directory and outside the sandbox's shared temporary directory, one directory per invocation.
- The builder's settings are those of point 2, with the mirrors and any other directory of the supervisor's named in `denyRead`.
- The environment handed to a run is an allowlist, not the supervisor's environment with some names removed.
- Point 4 is recorded as a limit of 3b.
