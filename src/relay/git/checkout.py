"""Pristine checkouts of a pinned revision. Never a branch name: a name can move."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

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


class RemoteUnavailable(CheckoutFailed):
    """The repository could not be asked. Nothing is known; the same request may succeed
    later. Never grounds for a permanent decision."""


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
    revision = await resolve_ref(repository, wanted)
    if revision is None:
        raise CheckoutFailed(f"the repository has no {description}")
    return revision


async def resolve_ref(repository: str, wanted: str) -> str | None:
    """The object the exactly named ref points at now, or None if there is no such ref."""
    # ls-remote matches its pattern against the tail of every ref name, so another ref
    # ending in the same words would be listed too. Only the exact ref counts.
    try:
        listing = await git("ls-remote", "--", repository, wanted, redact=repository)
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


async def refs_under(repository: str, prefix: str) -> dict[str, str]:
    """Every ref whose full name starts with `prefix`, with the object it points at."""
    try:
        listing = await git("ls-remote", "--", repository, f"{prefix}*", redact=repository)
    except CheckoutFailed as failure:
        raise RemoteUnavailable(str(failure)) from None
    found = {}
    for line in listing.splitlines():
        revision, _, ref = line.partition("\t")
        if ref.startswith(prefix) and FULL_COMMIT_HASH.fullmatch(revision):
            found[ref] = revision
    return found


async def pristine_checkout(repository: str, revision: str, into: Path) -> Path:
    """The files of one revision and nothing else: no Git metadata, so neither the remote
    address nor any credential embedded in it is exposed to whatever runs there."""
    if not FULL_COMMIT_HASH.fullmatch(revision):
        raise CheckoutFailed(f"a revision must be a 40-character commit hash, not {revision!r}")
    await git("clone", "--quiet", "--no-checkout", "--", repository, str(into), redact=repository)
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


async def git_succeeds(*arguments: str) -> bool:
    try:
        await git(*arguments)
    except CheckoutFailed:
        return False
    return True


async def git(*arguments: str, redact: str | None = None) -> str:
    return (await _git_output(*arguments, redact=redact)).decode(errors="replace").strip()


async def _git_output(*arguments: str, redact: str | None = None) -> bytes:
    """Raw output: Git path names are bytes and need not be valid text."""
    status, stdout, detail = await git_result(*arguments, redact=redact)
    if status != 0:
        raise CheckoutFailed(f"git {arguments[0]} failed: {detail}")
    return stdout


async def git_result(*arguments: str, redact: str | None = None) -> tuple[int, bytes, str]:
    """Run trusted Git and return its exit status, its output and its diagnostics, with
    the repository address and any URL credentials removed from the diagnostics."""
    process = await asyncio.create_subprocess_exec(
        *_TRUSTED_GIT,
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_GIT_ENVIRONMENT,
    )
    stdout, stderr = await process.communicate()
    detail = stderr.decode(errors="replace").strip()
    if redact:
        # Git echoes the address it was given; a failure must not carry it onwards.
        detail = detail.replace(redact, "<repository>")
    assert process.returncode is not None
    return process.returncode, stdout, _URL_USERINFO.sub("//", detail)
