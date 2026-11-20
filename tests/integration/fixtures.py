"""A VAT calculator whose baseline returns the net amount unchanged, in a real Git repository."""

import subprocess
from pathlib import Path

from relay.domain.evidence import Baseline, Expected, Launch, ProcessContract

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


def publish_attempt(
    repository: Path, behaviour_id: str, attempt: int, files: dict[str, str], *, on: str
) -> str:
    """What a Builder does: commit a change on top of `on` and publish it under the attempt's
    ref, leaving trunk where it was. Returns the candidate's commit hash."""

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("checkout", "-q", "--detach", on)
    for name, content in files.items():
        (repository / name).write_text(content)
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", f"attempt {attempt}")
    candidate = git("rev-parse", "HEAD")
    git("update-ref", f"refs/relay/attempts/{behaviour_id}/{attempt}", candidate)
    git("checkout", "-q", "main")
    return candidate
