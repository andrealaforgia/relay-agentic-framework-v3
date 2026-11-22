"""The control plane's own copy of every commit it has judged.

A recorded commit hash names an object but does not keep it: a Builder can move the ref
that made it reachable, and Git may then prune it. So before a commit is used as
evidence it is fetched into a bare repository on the control plane's disk and pinned
there under a private ref. Publication, review checkouts and the trunk push all take the
commit from this mirror by its recorded hash, never by resolving a name again.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from relay.git.checkout import (
    FULL_COMMIT_HASH,
    CheckoutFailed,
    RemoteRejected,
    RemoteUnavailable,
    git,
    git_answers_yes,
    git_result,
    leave_only_the_tracked_files,
)

_PINS = "refs/relay/pins"
_INCOMING = "refs/relay/incoming"


class Mirror:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def pin_from(self, repository: str, ref: str, expected: str) -> None:
        """Fetch the commit `ref` points at, require it to be `expected`, and pin it.
        Only this one source ref is read, into a scratch ref that cannot touch a pin."""
        _require_hash(expected)
        await self._ensure_exists()
        scratch = f"{_INCOMING}/{uuid.uuid4().hex}"
        try:
            await self._git(
                "fetch", "--quiet", "--no-tags", "--", repository, f"+{ref}:{scratch}",
                redact=repository,
            )  # fmt: skip
            if await self._git("rev-parse", scratch) != expected:
                raise CheckoutFailed("the candidate moved while it was being fetched")
            # A ref may point at a tag object; only a commit is a candidate.
            if await self._git("cat-file", "-t", expected) != "commit":
                raise CheckoutFailed("the candidate is not a commit")
            await self._git("update-ref", f"{_PINS}/{expected}", expected)
        finally:
            await self._discard(scratch)

    async def recover(
        self, repository: str, commit: str, refs: Sequence[str], timeout: float | None = None
    ) -> bool:
        """Make sure this exact commit is pinned, fetching it if need be from any of the
        given refs whose history still contains it.

        True: it is pinned. False: every ref was fetched and none contains it, so it is
        gone. If a fetch could not be completed, or gave no answer within `timeout`, the
        search is unfinished and RemoteUnavailable is raised: "could not look" is never
        reported as "not there"."""
        _require_hash(commit)
        if await self.holds(commit):
            return True
        await self._ensure_exists()
        unfinished: CheckoutFailed | None = None
        for ref in refs:
            scratch = f"{_INCOMING}/{uuid.uuid4().hex}"
            try:
                status, _, detail = await git_result(
                    "-C", str(self._path), "fetch", "--quiet", "--no-tags", "--", repository,
                    f"+{ref}:{scratch}",
                    redact=repository, timeout=timeout,
                )  # fmt: skip
                if status != 0:
                    raise CheckoutFailed(f"git fetch failed: {detail}")
            except CheckoutFailed as failure:
                unfinished = failure
                continue
            try:
                if await self._is_a_commit(commit) and await self.descends(scratch, commit):
                    await self._git("update-ref", f"{_PINS}/{commit}", commit)
                    return True
            finally:
                await self._discard(scratch)
        if unfinished is not None:
            raise RemoteUnavailable(str(unfinished))
        return False

    async def holds(self, commit: str) -> bool:
        """Whether this commit is pinned here, and so safe from pruning. An object that
        merely happens to be present is not held."""
        _require_hash(commit)
        # A directory can exist without being a repository yet, if creating the mirror
        # was interrupted. That is a mirror holding nothing, not an error.
        return self._is_initialised() and await git_answers_yes(
            "-C",
            str(self._path),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{_PINS}/{commit}^{{commit}}",
        )

    async def descends(self, commit: str, ancestor: str) -> bool:
        """Whether `ancestor` is in the history of `commit`. A commit this mirror does not
        have at all cannot be in the history of one it has fetched in full."""
        if not await self._is_a_commit(ancestor):
            return False
        return await git_answers_yes(
            "-C", str(self._path), "merge-base", "--is-ancestor", ancestor, commit
        )

    async def _is_a_commit(self, revision: str) -> bool:
        # `cat-file -e` on a bare hash exits 1 when the object is absent, which is a real
        # no. Asking for "<hash>^{commit}" would exit with an error for the same case.
        if not await git_answers_yes("-C", str(self._path), "cat-file", "-e", revision):
            return False
        return await self._git("cat-file", "-t", revision) == "commit"

    async def _discard(self, scratch: str) -> None:
        """Remove a scratch ref. Failing to tidy up is not a reason to fail the operation."""
        await git_result("-C", str(self._path), "update-ref", "-d", scratch)

    async def export(self, commit: str, into: Path) -> Path:
        """Exactly the tracked tree of a pinned commit, as the oracle and reviewers see it."""
        _require_hash(commit)
        await git("clone", "--quiet", "--no-checkout", "--", str(self._path), str(into))
        await git("-C", str(into), "fetch", "--quiet", "origin", f"{_PINS}/{commit}")
        await git("-C", str(into), "checkout", "--quiet", "--detach", commit)
        if await git("-C", str(into), "rev-parse", "HEAD") != commit:
            raise CheckoutFailed("the checked-out revision is not the pinned commit")
        await leave_only_the_tracked_files(into)
        return into

    async def retain(self, workspace: Path, base: str, ref: str) -> str:
        """Keep what is in a workspace as a commit on top of `base`, under `ref`, and pin
        it. The supervisor makes this commit: whoever worked in the workspace was given
        no Git metadata and is trusted with none. A workspace that has grown some is
        refused, since it would be read as a repository of its own.

        Everything in the workspace becomes the tree: what was added, changed and
        removed since `base`. A link is kept as a link and never followed."""
        _require_hash(base)
        for found in workspace.rglob(".git"):
            raise CheckoutFailed(
                f"the workspace contains Git metadata at {found.relative_to(workspace)}"
            )
        if not await self.holds(base):
            raise CheckoutFailed("the commit the workspace was made from is not held")
        with tempfile.TemporaryDirectory(prefix="relay-retain-") as scratch:
            clone = str(Path(scratch) / "clone")
            await git("clone", "--quiet", "--no-checkout", "--", str(self._path), clone)
            await git("-C", clone, "fetch", "--quiet", "origin", f"{_PINS}/{base}")
            # The index starts empty, so the tree is exactly what is in the workspace:
            # what the base had and the workspace lacks is not in it.
            await git("-C", clone, f"--work-tree={workspace}", "add", "--all", "--", ".")
            tree = await git("-C", clone, "write-tree")
            commit = await git(
                "-C", clone, "-c", "user.name=Relay", "-c", "user.email=relay@localhost",
                "commit-tree", tree, "-p", base, "-m", "candidate built by a worker",
            )  # fmt: skip
            await self._git("fetch", "--quiet", "--no-tags", "--", clone, f"+{commit}:{ref}")
        await self._git("update-ref", f"{_PINS}/{commit}", commit)
        return commit

    async def publish(
        self, repository: str, commit: str, ref: str, timeout: float | None = None
    ) -> None:
        """Create `ref` at a pinned commit on the remote, only if the ref does not exist.
        Exactly one destination is named; nothing else on the remote can be touched.

        The outcome is read from Git's per-ref push status, not guessed from its messages:
        RemoteRejected when the remote itself refused this ref, CheckoutFailed when the ref
        already exists, RemoteUnavailable when the push never got an answer."""
        _require_hash(commit)
        refusal = await self._push(repository, commit, ref, expected="", timeout=timeout)
        if refusal is None:
            return
        if refusal.by_the_remote:
            raise RemoteRejected(refusal.detail)
        # Refused on this side: the lease found the ref already present.
        raise CheckoutFailed(f"the push was not accepted: {refusal.detail}")

    async def advance(
        self,
        repository: str,
        commit: str,
        ref: str,
        *,
        expected: str,
        timeout: float,
        about_to_send: Callable[[], Awaitable[bool]] | None = None,
    ) -> bool:
        """Move `ref` on the remote from exactly `expected` to a pinned commit that is a
        fast-forward of it. True once the remote has reported the update.

        Everything that can be established locally is established first. Only then is
        `about_to_send` called, immediately before the one operation that can reach the
        remote; if it answers no, nothing is sent and the result is False.

        Before sending: NotFastForward, or CheckoutFailed if this mirror cannot say.
        After sending: RemoteRejected when the update did not happen and this send
        cannot make it happen; RemoteUnavailable when no answer came, in which case the
        update may have happened, or may yet."""
        _require_hash(commit)
        _require_hash(expected)
        if not await self.holds(commit):
            raise CheckoutFailed("the commit to send is not pinned in this mirror")
        if not await self._is_a_commit(expected):
            raise CheckoutFailed("the expected head is not available in this mirror")
        if not await self.descends(commit, expected):
            raise NotFastForward("the commit is not a fast-forward of the expected head")
        if about_to_send is not None and not await about_to_send():
            return False
        refusal = await self._push(repository, commit, ref, expected=expected, timeout=timeout)
        if refusal is not None:
            raise RemoteRejected(refusal.detail)
        return True

    async def read_ref(self, repository: str, ref: str, *, base: str, timeout: float) -> RefReading:
        """What `ref` points at on the remote now, and whether that same commit strictly
        descends from `base`. One fetch answers both, so they cannot describe two
        different moments. A remote that does not answer in time is RemoteUnavailable."""
        _require_hash(base)
        await self._ensure_exists()
        scratch = f"{_INCOMING}/{uuid.uuid4().hex}"
        try:
            status, _, detail = await git_result(
                "-C", str(self._path), "fetch", "--quiet", "--no-tags", "--", repository,
                f"+{ref}:{scratch}",
                redact=repository, timeout=timeout,
            )  # fmt: skip
            if status != 0:
                raise RemoteUnavailable(f"git fetch failed: {detail}")
            head = await self._git("rev-parse", scratch)
            return RefReading(head, head != base and await self.descends(head, base))
        finally:
            await self._discard(scratch)

    async def _push(
        self,
        repository: str,
        commit: str,
        ref: str,
        *,
        expected: str,
        timeout: float | None = None,
    ) -> _Refusal | None:
        """Push one commit to one ref, on condition that the ref is at `expected` (empty:
        absent). None when the remote accepted it; a refusal when this ref was definitely
        not updated. RemoteUnavailable when the outcome is not known.

        The outcome is read from Git's per-ref status, never guessed from its messages."""
        status, output, detail = await git_result(
            "-C", str(self._path), "push", "--porcelain",
            f"--force-with-lease={ref}:{expected}", "--", repository, f"{commit}:{ref}",
            redact=repository, timeout=timeout,
        )  # fmt: skip
        if status == 0:
            return None
        for line in output.decode(errors="replace").splitlines():
            flag, _, rest = line.partition("\t")
            refs, _, summary = rest.partition("\t")
            if flag == "!" and refs.endswith(f":{ref}"):
                if summary.startswith("[remote rejected]"):
                    return _Refusal(f"{summary.strip()} {detail}".strip(), by_the_remote=True)
                if summary.startswith("[rejected]"):
                    return _Refusal(summary.strip(), by_the_remote=False)
                # Anything else, "[remote failure]" above all, means the remote did not
                # report what happened. The push may or may not have taken effect.
                raise RemoteUnavailable(f"git push: {summary.strip()} {detail}".strip())
        raise RemoteUnavailable(f"git push failed: {detail}")

    def _is_initialised(self) -> bool:
        return (self._path / "HEAD").exists()

    async def _ensure_exists(self) -> None:
        if not self._is_initialised():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            await git("init", "--quiet", "--bare", str(self._path))

    async def _git(self, *arguments: str, redact: str | None = None) -> str:
        return await git("-C", str(self._path), *arguments, redact=redact)


class NotFastForward(CheckoutFailed):
    """Established locally, before anything was sent."""


@dataclass(frozen=True)
class RefReading:
    head: str
    beyond_base: bool


@dataclass(frozen=True)
class _Refusal:
    detail: str
    by_the_remote: bool


def _require_hash(commit: str) -> None:
    if not FULL_COMMIT_HASH.fullmatch(commit):
        raise CheckoutFailed("a revision must be a 40-character commit hash")
