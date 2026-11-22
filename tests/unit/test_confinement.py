"""What bounds a model's process, by role. The settings here are the ones that were seen
to hold against the real CLI (`docs/consensus/slice-3/3b-confinement-probes.md`); these
tests fix what Relay asks for, and the paid suite checks that asking is enough."""

from pathlib import Path

import pytest

from relay.providers.confinement import (
    ROLE_TOOLS,
    boundary_digest,
    confinement_for,
    effective_agent_sha256,
    environment_for_a_run,
)
from relay.providers.definitions import AgentDefinition

ROOT = Path("/srv/relay/workspaces")
DEFINITION = AgentDefinition(
    name="alf-test-design-reviewer",
    path=Path("/agents/alf-test-design-reviewer.md"),
    sha256="0" * 64,
    revision=None,
    dirty=False,
    description="Evaluates test code quality.",
    prompt="You review tests.\n",
)


@pytest.mark.parametrize("role", ["analyst", "verifier", "qa", "sentinel"])
def test_a_role_that_only_reads_has_no_shell_and_cannot_change_a_file(role: str) -> None:
    bounds = confinement_for(role, ROOT / "inv-1" / "tree")

    assert bounds.tools == ("Read", "Grep", "Glob")
    assert bounds.settings == {
        "permissions": {"blockReadsOutsideWorkingDirectories": True, "deny": ["Agent", "Skill"]}
    }


def test_the_builder_has_a_shell_that_is_sandboxed_with_no_way_out_and_no_network() -> None:
    workspace = ROOT / "inv-1" / "tree"

    bounds = confinement_for("builder", workspace, withheld=[Path("/srv/relay/mirrors")])

    assert bounds.tools == ("Read", "Grep", "Glob", "Edit", "Write", "Bash")
    assert bounds.settings["permissions"] == {
        "blockReadsOutsideWorkingDirectories": True,
        "deny": ["Agent", "Skill"],
        "defaultMode": "acceptEdits",
    }
    sandbox = bounds.settings["sandbox"]
    assert (sandbox["enabled"], sandbox["failIfUnavailable"]) == (True, True)
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"] == {"allowedDomains": []}
    # Deny wins over allow for writes, so nothing is said about writes: outside the
    # workspace they are refused by the sandbox itself. For reads, the home directory and
    # everything beside the workspace are denied, and the workspace alone given back.
    assert sandbox["filesystem"] == {
        "denyRead": ["~/", str(workspace.parent), "/srv/relay/mirrors"],
        "allowRead": [str(workspace)],
    }


def test_a_role_relay_does_not_know_has_no_bounds_to_run_within() -> None:
    with pytest.raises(LookupError, match="curator"):
        confinement_for("curator", ROOT / "inv-1" / "tree")


@pytest.mark.parametrize(
    "workspace",
    [Path.home() / "relay" / "inv-1" / "tree", Path("/tmp/claude-501/inv-1/tree"), Path("tree")],
)
def test_a_workspace_where_the_bounds_would_not_hold_is_refused(workspace: Path) -> None:
    """Under the home directory the rule that hides the home directory would hide it;
    under the sandbox's shared temporary directory every sandboxed shell can write."""
    with pytest.raises(ValueError, match="workspace"):
        confinement_for("builder", workspace)


def test_the_run_is_given_an_environment_made_for_it_and_nothing_else() -> None:
    """The sandbox does not withhold the environment from a shell: what is passed is all
    that stands between a model and a secret in it."""
    given = environment_for_a_run(
        {
            "PATH": "/usr/bin",
            "HOME": "/Users/po",
            "USER": "po",
            "LANG": "en_GB.UTF-8",
            "LC_ALL": "en_GB.UTF-8",
            "TMPDIR": "/var/tmp",
            "RELAY_DATABASE_URL": "postgresql://relay:secret@db/relay",
            "AWS_SECRET_ACCESS_KEY": "s",
            "ANTHROPIC_API_KEY": "k",
            "GITHUB_TOKEN": "t",
            "SOMETHING_NOBODY_THOUGHT_OF": "x",
        }
    )

    assert given == {
        "PATH": "/usr/bin",
        "HOME": "/Users/po",
        "USER": "po",
        "LANG": "en_GB.UTF-8",
        "LC_ALL": "en_GB.UTF-8",
        "TMPDIR": "/var/tmp",
    }


def test_a_credential_the_provider_needs_is_passed_only_if_the_operator_says_so() -> None:
    kept = environment_for_a_run(
        {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "k", "GH_TOKEN": "t"},
        passed_on=["ANTHROPIC_API_KEY"],
    )

    assert kept == {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "k"}


def test_the_definition_as_handed_over_is_hashed_with_the_tools_it_was_given() -> None:
    reading = effective_agent_sha256(DEFINITION, ("Read", "Grep", "Glob"))

    assert reading == effective_agent_sha256(DEFINITION, ("Read", "Grep", "Glob"))
    assert reading != effective_agent_sha256(DEFINITION, ("Read", "Grep", "Glob", "Bash"))
    assert reading != DEFINITION.sha256 and len(reading) == 64


def test_what_a_pass_is_a_pass_of_does_not_depend_on_which_workspace_it_was() -> None:
    """S3-19.7: bound to the bounds and the runtime, not to one directory."""
    one = confinement_for("builder", ROOT / "inv-1" / "tree")
    other = confinement_for("builder", ROOT / "inv-2" / "another-tree")
    runtime = {"cli": "2.1.330", "os": "Darwin 25.5.0"}

    assert boundary_digest(one, runtime) == boundary_digest(other, runtime)
    assert boundary_digest(one, runtime) != boundary_digest(one, {**runtime, "cli": "2.1.331"})
    assert boundary_digest(one, runtime) != boundary_digest(
        confinement_for("qa", ROOT / "inv-1" / "tree"), runtime
    )
    withheld = confinement_for("builder", ROOT / "inv-1" / "tree", withheld=[Path("/srv/x")])
    assert boundary_digest(one, runtime) != boundary_digest(withheld, runtime)


def test_roles_with_the_same_bounds_are_within_the_same_boundary() -> None:
    """It is the bounds that were seen to hold, whoever is in them."""
    runtime = {"cli": "2.1.330"}

    assert boundary_digest(confinement_for("qa", ROOT / "a" / "tree"), runtime) == boundary_digest(
        confinement_for("analyst", ROOT / "b" / "tree"), runtime
    )


def test_every_role_with_a_definition_has_bounds() -> None:
    from relay.providers.definitions import DEFAULT_DEFINITIONS

    assert set(ROLE_TOOLS) == set(DEFAULT_DEFINITIONS)
