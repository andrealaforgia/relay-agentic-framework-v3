"""The oracle launches a real subject from a pristine checkout and records what it saw.
The subject's own claims, and any file it writes, carry no weight."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from relay.domain.evidence import Ineligibility, Launch
from relay.git.checkout import CheckoutFailed, pristine_checkout, trunk_head
from relay.testing.oracle import NativeIsolation, ProcessOracle
from tests.integration.fixtures import VAT_RETURNS_NET, commit


async def observe(tmp_path: Path, files: dict[str, str], launch: Launch):  # type: ignore[no-untyped-def]
    revision = commit(tmp_path / "subject", files)
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
    return await ProcessOracle(NativeIsolation()).observe(workdir, launch)


async def test_it_records_exit_code_and_complete_streams(tmp_path: Path) -> None:
    observation = await observe(
        tmp_path, {"vat.py": VAT_RETURNS_NET}, Launch(argv=("python3", "vat.py", "100"))
    )

    assert observation.is_eligible
    assert observation.exit_code == 0
    assert observation.stdout == b"100\n"
    assert observation.stderr == b""


async def test_standard_input_reaches_the_subject(tmp_path: Path) -> None:
    echo = "import sys\nprint(sys.stdin.read().upper(), end='')\n"

    observation = await observe(
        tmp_path, {"echo.py": echo}, Launch(argv=("python3", "echo.py"), stdin="net 100\n")
    )

    assert observation.stdout == b"NET 100\n"


async def test_a_program_that_does_not_exist_was_not_executed(tmp_path: Path) -> None:
    observation = await observe(
        tmp_path, {"vat.py": VAT_RETURNS_NET}, Launch(argv=("no-such-interpreter", "vat.py"))
    )

    assert observation.ineligibility is Ineligibility.NOT_EXECUTED


@pytest.mark.parametrize("ambiguous_status", [126, 127])
async def test_a_launcher_status_is_treated_as_not_executed(
    tmp_path: Path, ambiguous_status: int
) -> None:
    exits = f"import sys\nsys.exit({ambiguous_status})\n"

    observation = await observe(tmp_path, {"x.py": exits}, Launch(argv=("python3", "x.py")))

    assert observation.ineligibility is Ineligibility.NOT_EXECUTED


async def test_a_subject_that_hangs_is_stopped_and_marked(tmp_path: Path) -> None:
    hangs = "import time\nprint('100', flush=True)\ntime.sleep(60)\n"

    observation = await observe(
        tmp_path, {"vat.py": hangs}, Launch(argv=("python3", "vat.py"), timeout_seconds=0.5)
    )

    assert observation.ineligibility is Ineligibility.TIMED_OUT


async def test_a_subject_killed_by_a_signal_is_marked(tmp_path: Path) -> None:
    dies = "import os, signal\nprint('100', flush=True)\nos.kill(os.getpid(), signal.SIGKILL)\n"

    observation = await observe(tmp_path, {"vat.py": dies}, Launch(argv=("python3", "vat.py")))

    assert observation.ineligibility is Ineligibility.KILLED_BY_SIGNAL


async def test_output_beyond_the_capture_limit_is_not_silently_truncated(tmp_path: Path) -> None:
    floods = "print('120')\nprint('x' * 5_000_000)\n"
    revision = commit(tmp_path / "subject", {"vat.py": floods})
    workdir = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    observation = await ProcessOracle(NativeIsolation(), capture_limit_bytes=1_000_000).observe(
        workdir, Launch(argv=("python3", "vat.py"))
    )

    assert observation.ineligibility is Ineligibility.OUTPUT_OVERFLOW


async def test_the_subject_does_not_inherit_the_supervisor_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELAY_WORKER_TOKEN", "canary-credential")
    monkeypatch.setenv("VAT_RATE", "20")
    leaks = (
        "import os\n"
        "print(os.environ.get('RELAY_WORKER_TOKEN', 'absent'))\n"
        "print(os.environ.get('VAT_RATE', 'absent'))\n"
    )

    observation = await observe(tmp_path, {"leak.py": leaks}, Launch(argv=("python3", "leak.py")))

    assert observation.stdout == b"absent\nabsent\n"


async def test_the_checkout_is_exactly_the_pinned_revision(tmp_path: Path) -> None:
    repository = tmp_path / "subject"
    baseline = commit(repository, {"vat.py": VAT_RETURNS_NET})
    commit(repository, {"vat.py": "import sys\nprint(int(sys.argv[1]) * 120 // 100)\n"})

    workdir = await pristine_checkout(str(repository), baseline, tmp_path / "checkout")
    observation = await ProcessOracle(NativeIsolation()).observe(
        workdir, Launch(argv=("python3", "vat.py", "100"))
    )

    assert observation.stdout == b"100\n"


@pytest.mark.parametrize("revision", ["main", "HEAD", "--upload-pack=touch pwned", "abc123"])
async def test_only_a_full_commit_hash_can_be_checked_out(tmp_path: Path, revision: str) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})

    with pytest.raises(CheckoutFailed, match="40-character"):
        await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")


async def test_an_unknown_revision_fails_loudly(tmp_path: Path) -> None:
    commit(tmp_path / "subject", {"vat.py": VAT_RETURNS_NET})

    with pytest.raises(CheckoutFailed):
        await pristine_checkout(str(tmp_path / "subject"), "0" * 40, tmp_path / "checkout")


def branch(repository: Path, name: str, revision: str) -> None:
    subprocess.run(["git", "-C", str(repository), "branch", "-f", name, revision], check=True)


async def test_the_trunk_is_the_exactly_named_branch_not_one_that_ends_the_same_way(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "subject"
    impostor = commit(repository, {"vat.py": VAT_RETURNS_NET})
    trunk = commit(repository, {"vat.py": "print(120)\n"})
    branch(repository, "a/refs/heads/main", impostor)

    assert await trunk_head(str(repository), "main") == trunk


async def test_a_look_alike_branch_does_not_stand_in_for_a_missing_trunk(tmp_path: Path) -> None:
    repository = tmp_path / "subject"
    impostor = commit(repository, {"vat.py": VAT_RETURNS_NET})
    branch(repository, "a/refs/heads/release", impostor)

    with pytest.raises(CheckoutFailed, match="no branch 'release'"):
        await trunk_head(str(repository), "release")


def tracked_as_executable(repository: Path, name: str) -> str:
    subprocess.run(["git", "-C", str(repository), "update-index", "--chmod=+x", name], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "executable"], check=True)
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize("umask", [0o022, 0o077])
async def test_permissions_in_a_checkout_come_from_git_not_from_the_host(
    tmp_path: Path, umask: int
) -> None:
    repository = tmp_path / "subject"
    commit(repository, {"helper.sh": "#!/bin/sh\n", "data.txt": "x"})
    (repository / "sub").mkdir()
    commit(repository, {"sub/nested.txt": "y"})
    revision = tracked_as_executable(repository, "helper.sh")
    previous = os.umask(umask)
    try:
        checkout = await pristine_checkout(str(repository), revision, tmp_path / "checkout")
    finally:
        os.umask(previous)

    def mode(name: str) -> int:
        return (checkout / name).stat().st_mode & 0o777

    assert (mode("helper.sh"), mode("data.txt"), mode("sub"), mode("sub/nested.txt")) == (
        0o755,
        0o644,
        0o755,
        0o644,
    )
    assert mode(".") == 0o755


async def test_an_executable_whose_bit_the_filesystem_dropped_is_still_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "subject"
    commit(repository, {"helper.sh": "#!/bin/sh\n"})
    revision = tracked_as_executable(repository, "helper.sh")
    remove_tree = shutil.rmtree

    def on_a_filesystem_without_execute_bits(path: Path) -> None:
        (path.parent / "helper.sh").chmod(0o600)
        remove_tree(path)

    monkeypatch.setattr("relay.git.checkout.shutil.rmtree", on_a_filesystem_without_execute_bits)

    checkout = await pristine_checkout(str(repository), revision, tmp_path / "checkout")

    assert (checkout / "helper.sh").stat().st_mode & 0o777 == 0o755


async def test_a_tracked_file_that_turned_into_a_link_fails_the_checkout_and_touches_nothing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "host-file"
    outside.write_text("not the subject's")
    outside.chmod(0o600)
    repository = tmp_path / "subject"
    revision = commit(repository, {"vat.py": VAT_RETURNS_NET, "tool.sh": "#!/bin/sh\n"})
    remove_tree = shutil.rmtree

    def as_after_a_name_collision(path: Path) -> None:
        (path.parent / "tool.sh").unlink()
        os.symlink(outside, path.parent / "tool.sh")
        remove_tree(path)

    monkeypatch.setattr("relay.git.checkout.shutil.rmtree", as_after_a_name_collision)

    with pytest.raises(CheckoutFailed, match="not exactly the tracked tree"):
        await pristine_checkout(str(repository), revision, tmp_path / "checkout")

    assert outside.stat().st_mode & 0o777 == 0o600


async def test_a_tracked_file_reached_through_a_linked_directory_fails_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "host-dir"
    outside.mkdir()
    (outside / "tool.sh").write_text("host's own")
    (outside / "tool.sh").chmod(0o600)
    repository = tmp_path / "subject"
    (repository / "bin").mkdir(parents=True)
    revision = commit(repository, {"bin/tool.sh": "#!/bin/sh\n"})
    remove_tree = shutil.rmtree

    def with_the_directory_replaced_by_a_link(path: Path) -> None:
        remove_tree(path.parent / "bin")
        os.symlink(outside, path.parent / "bin")
        remove_tree(path)

    monkeypatch.setattr("relay.git.checkout.shutil.rmtree", with_the_directory_replaced_by_a_link)

    with pytest.raises(CheckoutFailed, match="not exactly the tracked tree"):
        await pristine_checkout(str(repository), revision, tmp_path / "checkout")

    assert (outside / "tool.sh").stat().st_mode & 0o777 == 0o600


async def test_two_tracked_names_folded_into_one_file_fail_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "subject"
    revision = commit(repository, {"a.txt": "one", "b.txt": "two"})
    remove_tree = shutil.rmtree

    def as_if_one_name_overwrote_the_other(path: Path) -> None:
        (path.parent / "b.txt").unlink()
        remove_tree(path)

    monkeypatch.setattr("relay.git.checkout.shutil.rmtree", as_if_one_name_overwrote_the_other)

    with pytest.raises(CheckoutFailed, match="not exactly the tracked tree"):
        await pristine_checkout(str(repository), revision, tmp_path / "checkout")


def repository_with_a_tracked_link(repository: Path, target: Path) -> str:
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "v@relay.test"], check=True
    )
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Verifier"], check=True)
    os.symlink(target, repository / "link")
    return commit(repository, {"vat.py": VAT_RETURNS_NET})


def commit_a_file_whose_name_is_not_valid_text(repository: Path) -> str:
    commit(repository, {"vat.py": VAT_RETURNS_NET})
    blob = subprocess.run(
        ["git", "-C", str(repository), "hash-object", "-w", "--stdin"],
        input=b"x",
        check=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run(
        [b"git", b"-C", os.fsencode(repository), b"update-index", b"--add", b"--cacheinfo",
         b"100644," + blob + b",caf\xe9.txt"],
        check=True,
    )  # fmt: skip
    subprocess.run(["git", "-C", str(repository), "commit", "-q", "-m", "odd name"], check=True)
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


async def test_a_tracked_link_is_left_as_a_link_and_its_target_untouched(tmp_path: Path) -> None:
    outside = tmp_path / "host-file"
    outside.write_text("x")
    outside.chmod(0o600)
    revision = repository_with_a_tracked_link(tmp_path / "subject", outside)

    checkout = await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")

    assert (checkout / "link").is_symlink()
    assert outside.stat().st_mode & 0o777 == 0o600


async def test_a_file_name_that_is_not_valid_text_is_checked_out_or_refused_explicitly(
    tmp_path: Path,
) -> None:
    revision = commit_a_file_whose_name_is_not_valid_text(tmp_path / "subject")

    try:
        checkout = await pristine_checkout(
            str(tmp_path / "subject"), revision, tmp_path / "checkout"
        )
    except CheckoutFailed:
        return  # a filesystem that cannot hold the name: an explicit, recorded failure
    assert (checkout / "vat.py").stat().st_mode & 0o777 == 0o644
    assert content_of(os.path.join(os.fsencode(checkout), b"caf\xe9.txt")) == b"x"


def content_of(path: bytes) -> bytes:
    with open(path, "rb") as file:
        return file.read()


def add_to_the_index(repository: Path, name: str, content: bytes) -> None:
    """Track a file without creating it in the worktree, as a commit made on another
    kind of filesystem could."""
    blob = (
        subprocess.run(
            ["git", "-C", str(repository), "hash-object", "-w", "--stdin"],
            input=content,
            check=True,
            capture_output=True,
        )
        .stdout.strip()
        .decode()
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},{name}",
        ],
        check=True,
    )


def commit_the_index(repository: Path) -> str:
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-q", "-m", "from another host"], check=True
    )
    return subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def two_directories_differing_only_by_case(repository: Path) -> str:
    (repository / "rates").mkdir(parents=True)
    commit(repository, {"rates/standard": "20"})
    add_to_the_index(repository, "Rates/reduced", b"5")
    return commit_the_index(repository)


async def test_two_tracked_directories_folded_into_one_fail_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "subject"
    revision = two_directories_differing_only_by_case(repository)
    remove_tree = shutil.rmtree

    def as_on_a_filesystem_that_ignores_case(path: Path) -> None:
        checkout = path.parent
        names = {entry.name for entry in checkout.iterdir()}
        if {"rates", "Rates"} <= names:  # this host kept them apart; fold them
            (checkout / "Rates" / "reduced").rename(checkout / "rates" / "reduced")
            (checkout / "Rates").rmdir()
        remove_tree(path)

    monkeypatch.setattr("relay.git.checkout.shutil.rmtree", as_on_a_filesystem_that_ignores_case)

    with pytest.raises(CheckoutFailed, match="not exactly the tracked tree"):
        await pristine_checkout(str(repository), revision, tmp_path / "checkout")


async def test_a_file_that_is_not_tracked_fails_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "subject"
    revision = commit(repository, {"vat.py": VAT_RETURNS_NET})
    remove_tree = shutil.rmtree

    def with_a_stray_file(path: Path) -> None:
        (path.parent / "stray.py").write_text("not in the commit")
        remove_tree(path)

    monkeypatch.setattr("relay.git.checkout.shutil.rmtree", with_a_stray_file)

    with pytest.raises(CheckoutFailed, match="not exactly the tracked tree"):
        await pristine_checkout(str(repository), revision, tmp_path / "checkout")


async def test_a_tracked_link_that_became_a_file_fails_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "host-file"
    outside.write_text("x")
    revision = repository_with_a_tracked_link(tmp_path / "subject", outside)
    remove_tree = shutil.rmtree

    def with_the_link_replaced_by_a_file(path: Path) -> None:
        (path.parent / "link").unlink()
        (path.parent / "link").write_text("a regular file now")
        remove_tree(path)

    monkeypatch.setattr("relay.git.checkout.shutil.rmtree", with_the_link_replaced_by_a_file)

    with pytest.raises(CheckoutFailed, match="not exactly the tracked tree"):
        await pristine_checkout(str(tmp_path / "subject"), revision, tmp_path / "checkout")
