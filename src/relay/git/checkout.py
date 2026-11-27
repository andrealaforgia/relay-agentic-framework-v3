"""Pristine checkouts of a pinned revision. Never a branch name: a name can move."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import IO
from urllib.parse import unquote, urlsplit

from relay.domain.identifiers import require_identifier

FULL_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")
_BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_URL_USERINFO = re.compile(r"//[^/@\s]+@")
# No hooks, no user or system configuration, no credential prompts: this runs with
# the supervisor's authority on content the supervisor does not trust.
_TRUSTED_GIT = ("git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never")
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    # A replacement object would let a recorded hash name one tree and export another.
    "GIT_NO_REPLACE_OBJECTS": "1",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


class CheckoutFailed(Exception):
    pass


class CandidateMoved(CheckoutFailed):
    """The ref an attempt was published under names another commit than the one that
    was to be observed, or the repository answered that there is no such ref. Nothing
    was observed. A repository that did not answer is not this: it may yet."""


class RemoteUnavailable(CheckoutFailed):
    """The repository could not be asked. Nothing is known; the same request may succeed
    later. Never grounds for a permanent decision."""


class NotStarted(CheckoutFailed):
    """Git could not be started at all. Nothing ran, so nothing reached anyone."""


class RemoteRejected(CheckoutFailed):
    """The repository itself answered no to an update of a named ref."""


def require_credential_free(repository: str) -> str:
    """A repository address is stored and shown; a secret inside it would be too.
    Credentials are supplied to Git separately, never as part of the address. The
    refusal never repeats the address, since the address is what may hold the secret."""
    if "://" not in repository:
        return repository  # a local path or an scp-style address: no place for a secret
    try:
        address = urlsplit(repository)
        address.port  # noqa: B018 - parsing the authority is what raises on a malformed one
    except ValueError:
        raise ValueError("the repository address is malformed") from None
    if address.scheme != "file" and not address.hostname:
        # Without a host, whatever was meant as userinfo has been read as part of the path.
        raise ValueError("the repository address is malformed")
    if address.password is not None or address.query or address.fragment:
        raise ValueError("a repository address must not contain credentials or a query")
    if address.scheme in {"http", "https"} and address.username is not None:
        raise ValueError("a repository address must not contain credentials or a query")
    return repository


def canonical_repository(repository: str) -> str:
    """The one address a project's repository is known by. A local path is made absolute
    here, once: Git run from another directory would otherwise read it as another place."""
    require_credential_free(repository)
    head = repository.split("/", 1)[0]
    if repository.startswith("file://"):
        # Git reads a file URL from this machine whatever host it names, so it is the
        # same place as its path: one trunk is managed by one project, however the
        # address was written when the project was opened.
        return _local_identity(unquote(urlsplit(repository).path))
    if "://" in repository or ":" in head:
        return repository  # a URL, or an scp-style host:path address
    return _local_identity(repository)


def _local_identity(written: str) -> str:
    """A repository on this machine is known by the directory that holds its refs: a
    work tree, the `.git` inside it and a linked work tree are one set of branches, and
    so is a name Git completes with `.git`. Where there is no repository there is
    nothing to know it by, and it is refused: a path enrolled before its repository
    existed would be known by another name afterwards."""
    if not written:
        # No path at all is not the directory Relay happens to be running in.
        raise ValueError("the repository address names no path")
    resolved = Path(written).resolve()
    # As Git itself looks: the path, and the path with `.git` after it if there is
    # nothing at the path. Something there that cannot be read as a repository is
    # refused: it is not a reason to enrol its neighbour instead.
    candidate = resolved if resolved.exists() else resolved.with_name(resolved.name + ".git")
    found = subprocess.run(
        ["git", "-C", os.fspath(candidate), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        capture_output=True,
        check=False,
        # Only this directory is asked, not whatever repository it may happen to be in.
        env={**_GIT_ENVIRONMENT, "GIT_CEILING_DIRECTORIES": os.fspath(candidate.parent)},
    )  # fmt: skip
    if found.returncode != 0:
        raise ValueError("there is no Git repository that can be read at that path")
    # Exactly Git's own terminator is removed: a name may end in a space.
    return os.fspath(Path(os.fsdecode(found.stdout.removesuffix(b"\n"))).resolve())


async def trunk_head(repository: str, trunk: str, *, timeout: float | None = None) -> str:
    """The commit the project's trunk points at now, read from the project's own
    repository. With a timeout, a repository that does not answer is given up on."""
    if not _BRANCH_NAME.fullmatch(trunk):
        raise CheckoutFailed(f"not a branch name: {trunk!r}")
    return await _resolve(repository, f"refs/heads/{trunk}", f"branch {trunk!r}", timeout)


# The installation whose attempts these are, as part of every attempt ref: set once by
# the process when it has read which installation it serves. Empty in a process that has
# not said, where there is one installation and nothing to tell apart.
_ATTEMPTS_OF = ""


def name_attempts_under(installation: str) -> None:
    """Two installations may manage branches of one repository, each with a project, a
    behaviour and an attempt of the same names. Their candidates are kept apart."""
    global _ATTEMPTS_OF
    _ATTEMPTS_OF = f"{require_identifier('installation', installation)}/" if installation else ""


def attempt_ref(project_id: str, behaviour_id: str, attempt: int) -> str:
    """Where a Builder publishes the candidate for one attempt at one behaviour of one
    project. The project is part of the name: several projects may share a repository."""
    try:
        require_identifier("project", project_id)
        require_identifier("behaviour", behaviour_id)
    except ValueError as invalid:
        raise CheckoutFailed(f"not usable in a ref name: {invalid}") from None
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise CheckoutFailed("an attempt is a positive whole number")
    return f"refs/relay/{_ATTEMPTS_OF}attempts/{project_id}/{behaviour_id}/{attempt}"


async def attempt_head(repository: str, project_id: str, behaviour_id: str, attempt: int) -> str:
    ref = attempt_ref(project_id, behaviour_id, attempt)
    revision = await resolve_ref(repository, ref)
    if revision is None:
        raise CandidateMoved(
            f"the repository has no candidate for attempt {attempt} of {behaviour_id}"
        )
    return revision


async def _resolve(
    repository: str, wanted: str, description: str, timeout: float | None = None
) -> str:
    revision = await resolve_ref(repository, wanted, timeout=timeout)
    if revision is None:
        raise CheckoutFailed(f"the repository has no {description}")
    return revision


async def resolve_ref(repository: str, wanted: str, *, timeout: float | None = None) -> str | None:
    """The object the exactly named ref points at now, or None if there is no such ref."""
    # ls-remote matches its pattern against the tail of every ref name, so another ref
    # ending in the same words would be listed too. Only the exact ref counts.
    try:
        listing = await git(
            "ls-remote", "--", repository, wanted, redact=repository, timeout=timeout
        )
    except CheckoutFailed as failure:
        raise RemoteUnavailable(str(failure)) from None
    revisions = [
        revision
        for revision, _, ref in (line.partition("\t") for line in listing.splitlines())
        if ref == wanted
    ]
    if len(revisions) != 1 or not FULL_COMMIT_HASH.fullmatch(revisions[0]):
        return None
    return revisions[0]


async def refs_under(
    repository: str, prefix: str, *, timeout: float | None = None
) -> dict[str, str]:
    """Every ref whose full name starts with `prefix`, with the object it points at."""
    try:
        listing = await git(
            "ls-remote", "--", repository, f"{prefix}*", redact=repository, timeout=timeout
        )
    except CheckoutFailed as failure:
        raise RemoteUnavailable(str(failure)) from None
    found = {}
    for line in listing.splitlines():
        revision, _, ref = line.partition("\t")
        if ref.startswith(prefix) and FULL_COMMIT_HASH.fullmatch(revision):
            found[ref] = revision
    return found


async def pristine_checkout(
    repository: str, revision: str, into: Path, *, timeout: float | None = None
) -> Path:
    """The files of one revision and nothing else: no Git metadata, so neither the remote
    address nor any credential embedded in it is exposed to whatever runs there. With a
    timeout, a repository that stops answering part-way is given up on."""
    if not FULL_COMMIT_HASH.fullmatch(revision):
        raise CheckoutFailed(f"a revision must be a 40-character commit hash, not {revision!r}")
    await git(
        "clone", "--quiet", "--no-checkout", "--", repository, str(into),
        redact=repository, timeout=timeout,
    )  # fmt: skip
    await git("-C", str(into), "checkout", "--quiet", "--detach", revision)
    head = await git("-C", str(into), "rev-parse", "HEAD")
    if head != revision:
        raise CheckoutFailed(f"checked out {head}, expected {revision}")
    await leave_only_the_tracked_files(into)
    return into


async def leave_only_the_tracked_files(checkout: Path) -> None:
    """Remove the Git metadata, confirm the tree is exactly the tracked tree, and set
    permissions from what Git recorded: a file is executable by everyone if it is tracked
    as executable, readable by everyone either way. The host's umask, its filesystem and
    the account that made the checkout leave no trace, so every runner shows the subject
    the same tree.

    This runs with the supervisor's authority over a tree an untrusted repository shaped.
    A filesystem can fold two tracked names into one (by letter case, for instance),
    merging files or whole directories, and a tracked file can end up replaced by a link.
    So nothing is changed until the entries on disk match the tracked entries exactly, by
    name and by kind, and no link is ever followed."""
    listing = await _git_output("-C", str(checkout), "ls-files", "--stage", "-z")
    shutil.rmtree(checkout / ".git")
    root = os.fsencode(checkout)
    tracked: dict[bytes, str] = {}
    permissions: dict[bytes, int] = {}
    for entry in filter(None, listing.split(b"\0")):
        details, _, name = entry.partition(b"\t")
        mode = details.split()[0]
        tracked[name] = _KIND_OF_GIT_MODE.get(mode, "other")
        if tracked[name] == "file":
            permissions[name] = 0o755 if mode == b"100755" else 0o644
    directories: list[bytes] = []
    if _entries_on_disk(root, b"", directories) != tracked:
        raise CheckoutFailed("the checkout on this host is not exactly the tracked tree")
    files = [os.lstat(os.path.join(root, name)) for name in permissions]
    if len({(file.st_dev, file.st_ino) for file in files}) != len(files):
        # Two names for one file: setting the mode of one would set it for the other.
        raise CheckoutFailed("the checkout on this host is not exactly the tracked tree")
    for directory in directories:
        os.chmod(directory, 0o755)
    for name, mode_bits in permissions.items():
        # Verified above to be a regular file reached through real directories only.
        os.chmod(os.path.join(root, name), mode_bits)


_KIND_OF_GIT_MODE = {b"100644": "file", b"100755": "file", b"120000": "link", b"160000": "module"}


def _entries_on_disk(root: bytes, prefix: bytes, directories: list[bytes]) -> dict[bytes, str]:
    """Every file and link under the checkout by its exact name, without following links.
    Directories are collected for the caller; an empty directory stands for a submodule."""
    here = os.path.join(root, prefix) if prefix else root
    directories.append(here)
    found: dict[bytes, str] = {}
    with os.scandir(here) as scan:
        children = sorted(scan, key=lambda child: child.name)
    for child in children:
        name = prefix + b"/" + child.name if prefix else child.name
        if child.is_symlink():
            found[name] = "link"
        elif child.is_file(follow_symlinks=False):
            found[name] = "file"
        elif child.is_dir(follow_symlinks=False):
            below = _entries_on_disk(root, name, directories)
            found |= below or {name: "module"}
        else:
            found[name] = "other"
    return found


async def git_answers_yes(*arguments: str) -> bool:
    """Ask Git a yes-or-no question. True when it exits 0, False when it exits 1, which
    is how Git says no. Any other exit is Git failing to answer and is raised: "could not
    find out" must never be mistaken for "no"."""
    status, _, detail = await git_result(*arguments)
    if status not in (0, 1):
        raise CheckoutFailed(f"git {_subcommand(arguments)} failed: {detail}")
    return status == 0


def _subcommand(arguments: tuple[str, ...]) -> str:
    return arguments[2] if arguments[:1] == ("-C",) and len(arguments) > 2 else arguments[0]


async def git(*arguments: str, redact: str | None = None, timeout: float | None = None) -> str:
    output = await _git_output(*arguments, redact=redact, timeout=timeout)
    return output.decode(errors="replace").strip()


async def _git_output(
    *arguments: str, redact: str | None = None, timeout: float | None = None
) -> bytes:
    """Raw output: Git path names are bytes and need not be valid text."""
    status, stdout, detail = await git_result(*arguments, redact=redact, timeout=timeout)
    if status != 0:
        raise CheckoutFailed(f"git {arguments[0]} failed: {detail}")
    return stdout


async def git_result(
    *arguments: str, redact: str | None = None, timeout: float | None = None
) -> tuple[int, bytes, str]:
    """Run trusted Git and return its exit status, its output and its diagnostics, with
    the repository address and any URL credentials removed from the diagnostics.

    Before there is an exit status, two failures are kept apart. NotStarted is raised
    only for what is established before the launch is attempted: Git cannot be found, or
    the files for its output cannot be created. From the moment the launch is attempted,
    every failure is RemoteUnavailable: Git may have run, and whatever it had already
    sent to the other side may still take effect there. That covers an error from the
    launch itself, a Git stopped for not answering within `timeout`, an exit status that
    could not be collected, and output that could not be read back.

    Output goes to files, not pipes. A stopped Git can leave children behind that hold
    its output open (a remote hook, say); with files there is nothing to keep reading,
    so nothing here waits on them or is kept alive by them."""
    subcommand = _subcommand(arguments)
    program = shutil.which(_TRUSTED_GIT[0], path=_GIT_ENVIRONMENT["PATH"])
    if program is None:
        raise NotStarted(f"git {subcommand} could not be started: git was not found")
    if signal.getsignal(signal.SIGCHLD) is signal.SIG_IGN:
        # A finished child would be discarded by the system at once, and its process id
        # could then belong to something else. Relay must be the one that collects it.
        raise NotStarted(
            f"git {subcommand} was not started: this process discards its children's "
            "exit statuses, so the outcome of Git could not be known"
        )
    # Not `with`: a context manager that fails to close replaces whatever was already
    # being raised, and here that can be a cancellation or a proven "never started".
    try:
        output = tempfile.TemporaryFile()  # noqa: SIM115
    except OSError as failure:
        raise NotStarted(f"git {subcommand} could not be started: {failure}") from None
    try:
        try:
            diagnostics = tempfile.TemporaryFile()  # noqa: SIM115
        except OSError as failure:
            raise NotStarted(f"git {subcommand} could not be started: {failure}") from None
        try:
            status, stdout, detail = await _run(program, arguments, output, diagnostics, timeout)
        except BaseException as outcome:
            _close_without_replacing(outcome, diagnostics)
            raise
    except BaseException as outcome:
        # Whatever is already on its way out, a cancellation and a proven "never
        # started" above all, is what the caller must receive. A failure to tidy up is
        # attached to it, never substituted for it.
        _close_without_replacing(outcome, output)
        raise
    unclosed = []
    for file in (diagnostics, output):
        try:
            file.close()
        except OSError as failure:
            unclosed.append(str(failure))
    if unclosed:
        # Git ran and was heard, but this side could not finish cleanly: not an answer.
        raise RemoteUnavailable(
            f"git {subcommand} ran but its output files could not be closed: {unclosed[0]}"
        )
    if redact:
        # Git echoes the address it was given; a failure must not carry it onwards.
        detail = detail.replace(redact, "<repository>")
    return status, stdout, _URL_USERINFO.sub("//", detail)


async def _run(
    program: str,
    arguments: tuple[str, ...],
    output: IO[bytes],
    diagnostics: IO[bytes],
    timeout: float | None,
) -> tuple[int, bytes, str]:
    subcommand = _subcommand(arguments)
    try:
        # Blocking on purpose and briefly. No error from here on says whether the
        # child got as far as running Git, so none is read as "it did not".
        process = subprocess.Popen(  # noqa: ASYNC220
            [program, *_TRUSTED_GIT[1:], *arguments],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=diagnostics,
            env=_GIT_ENVIRONMENT,
        )
    except OSError as failure:
        raise RemoteUnavailable(f"git {subcommand} may or may not have run: {failure}") from None
    status = await _exit_status(process, timeout, subcommand)
    try:
        output.seek(0)
        diagnostics.seek(0)
        return status, output.read(), diagnostics.read().decode(errors="replace").strip()
    except OSError as failure:
        raise RemoteUnavailable(
            f"git {subcommand} ran but its output could not be read: {failure}"
        ) from None


def _close_without_replacing(outcome: BaseException, file: IO[bytes]) -> None:
    try:
        file.close()
    except OSError as failure:
        outcome.add_note(f"closing a file of Git's output also failed: {failure}")


async def _exit_status(
    process: subprocess.Popen[bytes], timeout: float | None, subcommand: str
) -> int:
    """Wait for Git and collect its exit status ourselves. Popen reports an exit status
    of zero when the status was lost to someone else, and a lost status must never be
    read as success: it is RemoteUnavailable, like any other outcome nobody saw."""
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    pause = 0.001
    collected = False
    try:
        while True:
            try:
                # Never blocks: it only asks whether the child has finished.
                pid, status = os.waitpid(process.pid, os.WNOHANG)  # noqa: ASYNC222
            except ChildProcessError:
                collected = True
                raise RemoteUnavailable(
                    f"git {subcommand} ran but its exit status could not be collected"
                ) from None
            if pid:
                collected = True
                return os.waitstatus_to_exitcode(status)
            if deadline is not None and loop.time() >= deadline:
                raise RemoteUnavailable(f"git {subcommand} gave no answer in time")
            await asyncio.sleep(pause)
            pause = min(pause * 2, 0.05)
    finally:
        if not collected:
            _stop(process.pid)
        # The status has been dealt with here; Popen must not look for it again.
        process.returncode = 0


def _stop(pid: int) -> None:
    """Stop a Git that was given up on (timed out, or its caller was cancelled).

    A signal goes only to a process this one still owns. An unreaped child keeps its
    process id, so asking first whether it is still ours and still running is what makes
    the signal safe: once it has been reaped, by anyone, that id may be someone else's.

    This rests on one rule for the whole process: nothing else here may reap a child it
    did not start. Between the question and the signal there is no way, short of a
    process handle macOS does not offer, to hold the id against such a reaper."""
    try:
        # Never blocks: it only asks whether the child has finished.
        finished, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return  # not ours to signal any more
    if finished:
        return
    os.kill(pid, signal.SIGKILL)
    with suppress(ChildProcessError):
        # Returns at once: the child has just been killed.
        os.waitpid(pid, 0)
