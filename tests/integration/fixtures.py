"""A VAT calculator whose baseline returns the net amount unchanged, in a real Git repository."""

import subprocess
from pathlib import Path

from relay.domain.evidence import Baseline, Expected, Launch, ProcessContract
from relay.testing.oracle import Isolation, Run

CONTRACT = ProcessContract(
    contract_id="B1-standard-rate",
    observes="the gross amount printed for a net amount at the standard rate",
    launch=Launch(argv=("python3", "vat.py", "100"), timeout_seconds=30),
    expect=Expected(exit_code=0, stdout="120\n"),
    baseline=Baseline(exit_code=0, stdout="100\n"),
)
VAT_RETURNS_NET = "import sys\nprint(int(sys.argv[1]))\n"
VAT_APPLIES_STANDARD_RATE = "import sys\nprint(int(sys.argv[1]) * 120 // 100)\n"


def commit(repository: Path, files: dict[str, str]) -> str:
    """Commit files to a real Git repository, creating it on first use; return the commit hash."""

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()

    if not (repository / ".git").exists():
        repository.mkdir(parents=True, exist_ok=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "verifier@relay.test")
        git("config", "user.name", "Verifier")
    for name, content in files.items():
        (repository / name).write_text(content)
    git("add", "-A")
    git("commit", "-q", "-m", "change")
    return git("rev-parse", "HEAD")


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


def publish_attempt(
    repository: Path,
    behaviour_id: str,
    attempt: int,
    files: dict[str, str],
    *,
    on: str,
    project: str = "vat-calculator",
) -> str:
    """What a Builder does: commit a change on top of `on` and publish it under the attempt's
    ref, leaving trunk where it was. Returns the candidate's commit hash."""
    _git(repository, "checkout", "-q", "--detach", on)
    for name, content in files.items():
        (repository / name).write_text(content)
    _git(repository, "add", "-A")
    _git(repository, "commit", "-q", "--allow-empty", "-m", f"attempt {attempt}")
    candidate = _git(repository, "rev-parse", "HEAD")
    _git(
        repository,
        "update-ref",
        f"refs/relay/attempts/{project}/{behaviour_id}/{attempt}",
        candidate,
    )
    _git(repository, "checkout", "-q", "main")
    return candidate


def publish_tag_as_attempt(
    repository: Path, behaviour_id: str, attempt: int, *, pointing_at: str
) -> str:
    """Publish an annotated tag object, not a commit, under the attempt's ref."""
    _git(repository, "tag", "-a", "-m", "not a commit", f"decoy-{attempt}", pointing_at)
    tag_object = _git(repository, "rev-parse", f"refs/tags/decoy-{attempt}")
    _git(
        repository,
        "update-ref",
        f"refs/relay/attempts/vat-calculator/{behaviour_id}/{attempt}",
        tag_object,
    )
    return tag_object


class CountingLaunches:
    """Wraps an isolation and counts how often a subject was actually launched."""

    def __init__(self, isolation: Isolation) -> None:
        self._isolation = isolation
        self.name = isolation.name
        self.launches = 0

    async def run(self, workdir: Path, launch: Launch, capture_limit: int) -> Run:
        self.launches += 1
        return await self._isolation.run(workdir, launch, capture_limit)
