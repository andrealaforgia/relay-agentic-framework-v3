"""A repository that stops answering part-way through a checkout is given up on."""

import asyncio
import time
from pathlib import Path

import pytest

from relay.git.checkout import CheckoutFailed, pristine_checkout


async def test_a_clone_that_stalls_is_given_up_on_at_its_deadline(tmp_path: Path) -> None:
    """3AII-S4-3.4. A server that accepts the connection and then says nothing."""
    held: list[asyncio.StreamWriter] = []

    async def says_nothing(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)

    server = await asyncio.start_server(says_nothing, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    started = time.monotonic()
    try:
        with pytest.raises(CheckoutFailed):
            await pristine_checkout(
                f"git://127.0.0.1:{port}/vat.git", "a" * 40, tmp_path / "subject", timeout=2
            )
    finally:
        for writer in held:
            writer.close()
        server.close()

    assert time.monotonic() - started < 30
