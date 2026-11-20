"""Pristine checkouts of a pinned revision. Never a branch name: a name can move."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
from pathlib import Path
from urllib.parse import urlsplit

from relay.domain.identifiers import require_identifier

_FULL_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")
_BRANCH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_URL_USERINFO = re.compile(r"//[^/@\s]+@")
# No hooks, no user or system configuration, no credential prompts: this runs with
# the supervisor's authority on content the supervisor does not trust.
_TRUSTED_GIT = ("git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never")
_GIT_ENVIRONMENT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
}


class CheckoutFailed(Exception):
    pass


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


async def trunk_head(repository: str, trunk: str) -> str:
    """The commit the project's trunk points at now, read from the project's own repository."""
    if not _BRANCH_NAME.fullmatch(trunk):
        raise CheckoutFailed(f"not a branch name: {trunk!r}")
    return await _resolve(repository, f"refs/heads/{trunk}", f"branch {trunk!r}")


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
    return f"refs/relay/attempts/{project_id}/{behaviour_id}/{attempt}"


async def attempt_head(repository: str, project_id: str, behaviour_id: str, attempt: int) -> str:
    ref = attempt_ref(project_id, behaviour_id, attempt)
    return await _resolve(repository, ref, f"candidate for attempt {attempt} of {behaviour_id}")


async def _resolve(repository: str, wanted: str, description: str) -> str:
    # ls-remote matches its pattern against the tail of every ref name, so another ref
    # ending in the same words would be listed too. Only the exact ref counts.
    listing = await _git("ls-remote", "--", repository, wanted, redact=repository)
    revisions = [
        revision
        for revision, _, ref in (line.partition("\t") for line in listing.splitlines())
        if ref == wanted
    ]
    if len(revisions) != 1 or not _FULL_COMMIT_HASH.fullmatch(revisions[0]):
        raise CheckoutFailed(f"the repository has no {description}")
    return revisions[0]


async def candidate_checkout(
    repository: str, ref: str, revision: str, built_on: str, into: Path
) -> tuple[Path, bool]:
    """The files of a candidate published under `ref`, and whether its history contains
    the revision it claims to be built on."""
    for pinned in (revision, built_on):
        if not _FULL_COMMIT_HASH.fullmatch(pinned):
            raise CheckoutFailed("a revision must be a 40-character commit hash")
    await _git("clone", "--quiet", "--no-checkout", "--", repository, str(into), redact=repository)
    await _git("-C", str(into), "fetch", "--quiet", "origin", ref, redact=repository)
    if await _git("-C", str(into), "rev-parse", "FETCH_HEAD") != revision:
        raise CheckoutFailed("the candidate moved while it was being fetched")
    # A ref may point at a tag object; checking that out would run the tagged commit
    # while the evidence named the tag. Only a commit is a candidate.
    if await _git("-C", str(into), "cat-file", "-t", revision) != "commit":
        raise CheckoutFailed("the candidate is not a commit")
    descends = await _succeeds("-C", str(into), "merge-base", "--is-ancestor", built_on, revision)
    await _git("-C", str(into), "checkout", "--quiet", "--detach", revision)
    if await _git("-C", str(into), "rev-parse", "HEAD") != revision:
        raise CheckoutFailed("the checked-out revision is not the candidate")
    await _leave_only_the_tracked_files(into)
    return into, descends


async def pristine_checkout(repository: str, revision: str, into: Path) -> Path:
    """The files of one revision and nothing else: no Git metadata, so neither the remote
    address nor any credential embedded in it is exposed to whatever runs there."""
    if not _FULL_COMMIT_HASH.fullmatch(revision):
        raise CheckoutFailed(f"a revision must be a 40-character commit hash, not {revision!r}")
    await _git("clone", "--quiet", "--no-checkout", "--", repository, str(into), redact=repository)
    await _git("-C", str(into), "checkout", "--quiet", "--detach", revision)
    head = await _git("-C", str(into), "rev-parse", "HEAD")
    if head != revision:
        raise CheckoutFailed(f"checked out {head}, expected {revision}")
    await _leave_only_the_tracked_files(into)
    return into


async def _leave_only_the_tracked_files(checkout: Path) -> None:
    """Remove the Git metadata and set permissions from what Git recorded: a file is
    executable by everyone if it is tracked as executable, readable by everyone either
    way. The host's umask, its filesystem and the account that made the checkout leave no
    trace, so every runner shows the subject the same tree.

    This runs with the supervisor's authority over a tree an untrusted repository shaped,
    so it never follows a symbolic link and refuses a tree that does not hold every
    tracked file as its own regular file (as happens when two names collide on a
    case-insensitive filesystem)."""
    tracked = await _git_output("-C", str(checkout), "ls-files", "--stage", "-z")
    shutil.rmtree(checkout / ".git")
    root = os.fsencode(checkout)
    modes: dict[bytes, int] = {}
    for entry in filter(None, tracked.split(b"\0")):
        details, _, name = entry.partition(b"\t")
        mode = details.split()[0]
        if mode in (b"100755", b"100644"):
            modes[os.path.join(root, name)] = 0o755 if mode == b"100755" else 0o644
    seen: set[tuple[int, int]] = set()
    for path in modes:
        found = _regular_file_inside(root, path)
        if found is None or found in seen:
            raise CheckoutFailed("the checkout does not hold every tracked file as its own file")
        seen.add(found)
    for directory, _, _ in os.walk(root):
        os.chmod(directory, 0o755)
    for path, permissions in modes.items():
        # Each path was just verified to be a regular file reached through no link.
        os.chmod(path, permissions)


def _regular_file_inside(root: bytes, path: bytes) -> tuple[int, int] | None:
    """The identity of a regular file reached without passing through any symbolic link,
    or None if the path is anything else."""
    parent = os.path.dirname(path)
    expected = os.path.normpath(os.path.join(os.path.realpath(root), os.path.relpath(parent, root)))
    if os.path.realpath(parent) != expected:
        return None  # some directory on the way is a symbolic link
    try:
        status = os.lstat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino) if stat.S_ISREG(status.st_mode) else None


async def _succeeds(*arguments: str) -> bool:
    try:
        await _git(*arguments)
    except CheckoutFailed:
        return False
    return True


async def _git(*arguments: str, redact: str | None = None) -> str:
    return (await _git_output(*arguments, redact=redact)).decode(errors="replace").strip()


async def _git_output(*arguments: str, redact: str | None = None) -> bytes:
    """Raw output: Git path names are bytes and need not be valid text."""
    process = await asyncio.create_subprocess_exec(
        *_TRUSTED_GIT,
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_GIT_ENVIRONMENT,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        if redact:
            # Git echoes the address it was given; a failure must not carry it onwards.
            detail = detail.replace(redact, "<repository>")
        raise CheckoutFailed(f"git {arguments[0]} failed: {_URL_USERINFO.sub('//', detail)}")
    return stdout
