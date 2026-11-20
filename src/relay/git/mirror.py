"""The control plane's own copy of every commit it has judged.

A recorded commit hash names an object but does not keep it: a Builder can move the ref
that made it reachable, and Git may then prune it. So before a commit is used as
evidence it is fetched into a bare repository on the control plane's disk and pinned
there under a private ref. Publication, review checkouts and the trunk push all take the
commit from this mirror by its recorded hash, never by resolving a name again.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
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

    async def recover(self, repository: str, commit: str, refs: Sequence[str]) -> bool:
        """Make sure this exact commit is pinned, fetching it if need be from any of the
        given refs whose history still contains it.

        True: it is pinned. False: every ref was fetched and none contains it, so it is
        gone. If a fetch could not be completed the search is unfinished and
        RemoteUnavailable is raised: "could not look" is never reported as "not there"."""
        _require_hash(commit)
        if await self.holds(commit):
            return True
        await self._ensure_exists()
        unfinished: CheckoutFailed | None = None
        for ref in refs:
            scratch = f"{_INCOMING}/{uuid.uuid4().hex}"
            try:
                await self._git(
                    "fetch", "--quiet", "--no-tags", "--", repository, f"+{ref}:{scratch}",
                    redact=repository,
                )  # fmt: skip
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
        return self._path.exists() and await git_answers_yes(
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

    async def publish(self, repository: str, commit: str, ref: str) -> None:
        """Create `ref` at a pinned commit on the remote, only if the ref does not exist.
        Exactly one destination is named; nothing else on the remote can be touched.

        The outcome is read from Git's per-ref push status, not guessed from its messages:
        RemoteRejected when the remote itself refused this ref, CheckoutFailed when the ref
        already exists, RemoteUnavailable when the push never got an answer."""
        _require_hash(commit)
        status, output, detail = await git_result(
            "-C", str(self._path), "push", "--porcelain", f"--force-with-lease={ref}:", "--",
            repository, f"{commit}:{ref}",
            redact=repository,
        )  # fmt: skip
        if status == 0:
            return
        for line in output.decode(errors="replace").splitlines():
            flag, _, rest = line.partition("\t")
            refs, _, summary = rest.partition("\t")
            if flag == "!" and refs.endswith(f":{ref}"):
                if summary.startswith("[remote rejected]"):
                    raise RemoteRejected(f"{summary.strip()} {detail}".strip())
                if summary.startswith("[rejected]"):
                    # Refused on this side: the lease found the ref already present.
                    raise CheckoutFailed(f"the push was not accepted: {summary.strip()}")
                # Anything else, "[remote failure]" above all, means the remote did not
                # report what happened. The push may or may not have taken effect.
                raise RemoteUnavailable(f"git push: {summary.strip()} {detail}".strip())
        raise RemoteUnavailable(f"git push failed: {detail}")

    async def _ensure_exists(self) -> None:
        if not (self._path / "HEAD").exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            await git("init", "--quiet", "--bare", str(self._path))

    async def _git(self, *arguments: str, redact: str | None = None) -> str:
        return await git("-C", str(self._path), *arguments, redact=redact)


def _require_hash(commit: str) -> None:
    if not FULL_COMMIT_HASH.fullmatch(commit):
        raise CheckoutFailed("a revision must be a 40-character commit hash")
