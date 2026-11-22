"""What bounds a model's process, by role.

A working directory bounds nothing by itself. What does is what the CLI is launched with:
the tools that exist for the run at all, a setting that makes the file tools refuse
anything outside the working directory, and, for the one role that has a shell, an OS
sandbox with no network and no way to be asked out of. These are the settings that were
seen to hold against the real CLI, written down in
`docs/consensus/slice-3/3b-confinement-probes.md`.

Two things the sandbox does not do are done here instead. It does not withhold the
launching process's environment from a shell, so the run is given an environment made
for it. And a rule that denies writes everywhere also denies them in the workspace, so
nothing is said about writes: outside the workspace the sandbox refuses them itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from relay.providers.claude import Confinement
from relay.providers.definitions import AgentDefinition

_READS = ("Read", "Grep", "Glob")
ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "analyst": _READS,
    "verifier": _READS,
    "qa": _READS,
    "sentinel": _READS,
    # The only role that changes files and runs anything.
    "builder": (*_READS, "Edit", "Write", "Bash"),
}
# What a run needs to find its programs, its home and its language. Nothing else is
# passed unless the operator names it.
_PASSED = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TZ")
# Every sandboxed shell on the host can write to `claude-<uid>` under these.
_SHARED_BY_EVERY_SANDBOX = (Path("/tmp"), Path("/private/tmp"))


def confinement_for(role: str, workspace: Path, *, withheld: Sequence[Path] = ()) -> Confinement:
    """The bounds of one run of a role in one workspace. `withheld` names directories of
    the supervisor's own that a shell must not read: its mirrors, its configuration."""
    if role not in ROLE_TOOLS:
        raise LookupError(f"there are no bounds for a {role} to run within")
    _require_a_workspace_the_bounds_hold_for(workspace)
    tools = ROLE_TOOLS[role]
    permissions: dict[str, object] = {
        # The file tools refuse anything outside the working directory, in every mode.
        "blockReadsOutsideWorkingDirectories": True,
        # No subagent and no skill: each would be a run Relay did not bound.
        "deny": ["Agent", "Skill"],
    }
    if "Bash" not in tools:
        return Confinement(tools, {"permissions": permissions})
    return Confinement(
        tools,
        {
            # Edits inside the workspace need nobody's approval; nothing else is approved.
            "permissions": {**permissions, "defaultMode": "acceptEdits"},
            "sandbox": {
                "enabled": True,
                # No sandbox, no shell: never a shell without one.
                "failIfUnavailable": True,
                # The model's own request to run something outside it is ignored.
                "allowUnsandboxedCommands": False,
                "autoAllowBashIfSandboxed": True,
                "network": {"allowedDomains": []},
                "filesystem": {
                    "denyRead": ["~/", str(workspace.parent), *(str(each) for each in withheld)],
                    "allowRead": [str(workspace)],
                },
            },
        },
    )


def _require_a_workspace_the_bounds_hold_for(workspace: Path) -> None:
    if not workspace.is_absolute():
        raise ValueError("a workspace is named by its whole path")
    if workspace.is_relative_to(Path.home()):
        # The rule that hides the home directory from a shell would hide the workspace.
        raise ValueError("a workspace cannot be under the home directory")
    for shared in _SHARED_BY_EVERY_SANDBOX:
        if workspace.is_relative_to(shared):
            beneath = workspace.relative_to(shared).parts
            if beneath and beneath[0].startswith("claude-"):
                raise ValueError(
                    "a workspace cannot be in the temporary directory every sandboxed shell shares"
                )


def environment_for_a_run(
    environment: Mapping[str, str], *, passed_on: Sequence[str] = ()
) -> dict[str, str]:
    """An environment made for a run: the few names it needs, the locale, and whatever
    the operator has said to pass on (a provider's API key, where one is used). A shell
    the run starts sees all of it, so a name passed on is a name the model can read."""
    return {
        name: value
        for name, value in environment.items()
        if name in _PASSED or name.startswith("LC_") or name in passed_on
    }


def effective_agent_sha256(definition: AgentDefinition, tools: Sequence[str]) -> str:
    """The hash of the definition as it is handed to the provider: what the file says
    of the agent, with the role's tools in place of whatever the file asked for."""
    handed_over = {
        "name": definition.name,
        "description": definition.description,
        "prompt": definition.prompt,
        "tools": list(tools),
    }
    return hashlib.sha256(json.dumps(handed_over, sort_keys=True).encode()).hexdigest()


def boundary_digest(bounds: Confinement, runtime: Mapping[str, str]) -> str:
    """What a confinement pass is a pass of: the tools and settings with the workspace's
    own path taken out, and the runtime they were tried on. A change to any of these is
    another boundary, with no pass. Two roles with the same bounds have the same
    boundary: it is the bounds that were seen to hold, not the name of who is in them."""
    settings = json.dumps(dict(bounds.settings), sort_keys=True)
    filesystem = dict(bounds.settings).get("sandbox", {}).get("filesystem", {})
    for workspace in filesystem.get("allowRead", []):
        settings = settings.replace(json.dumps(workspace)[1:-1], "<workspace>")
        settings = settings.replace(json.dumps(str(Path(workspace).parent))[1:-1], "<root>")
    bound = {
        "tools": list(bounds.tools),
        "settings": settings,
        "runtime": dict(sorted(runtime.items())),
    }
    return hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
