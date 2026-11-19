"""A VAT calculator whose baseline returns the net amount unchanged."""

import subprocess
from pathlib import Path

from relay.domain.evidence import (
    Baseline,
    Execution,
    Expected,
    Launch,
    ProcessContract,
    ProcessObservation,
)

CONTRACT = ProcessContract(
    contract_id="B1-standard-rate",
    observes="the gross amount printed for a net amount at the standard rate",
    launch=Launch(argv=("python3", "vat.py", "100")),
    expect=Expected(exit_code=0, stdout="120\n"),
    baseline=Baseline(exit_code=0, stdout="100\n"),
)
BASELINE_REVISION = "b" * 40
FAILS_AS_REGISTERED = Execution(
    BASELINE_REVISION, "container", ProcessObservation(exit_code=0, stdout=b"100\n")
)
SYNTAX_ERROR = Execution(
    BASELINE_REVISION,
    "container",
    ProcessObservation(exit_code=1, stderr=b"SyntaxError: invalid syntax"),
)

VAT_RETURNS_NET = "import sys\nprint(int(sys.argv[1]))\n"


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
