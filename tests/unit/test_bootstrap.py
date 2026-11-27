"""What a process is told from outside the database, and from where."""

from __future__ import annotations

from pathlib import Path

import pytest

from relay.runtime import NotConfigured, bootstrap, profile_of


def test_the_environment_says_where_the_database_and_redis_are(tmp_path: Path) -> None:
    told = bootstrap(
        {
            "RELAY_DATABASE_URL": "postgresql://d",
            "RELAY_REDIS_URL": "redis://r",
            "HOME": str(tmp_path),
        }
    )

    assert (told.database, told.redis, told.schema) == ("postgresql://d", "redis://r", None)


def test_a_named_file_says_it_and_the_environment_is_heard_first(tmp_path: Path) -> None:
    named = tmp_path / "relay.toml"
    named.write_text('[relay]\ndatabase = "postgresql://file"\nredis = "redis://file"\n')

    told = bootstrap(
        {"RELAY_CONFIG": str(named), "RELAY_REDIS_URL": "redis://said", "HOME": str(tmp_path)}
    )

    assert (told.database, told.redis) == ("postgresql://file", "redis://said")


def test_a_file_in_the_working_directory_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3C-1.5: what a process serves is not decided by where it was started."""
    (tmp_path / "relay.toml").write_text(
        '[relay]\ndatabase = "postgresql://here"\nredis = "redis://here"\n'
    )
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    home.mkdir()

    with pytest.raises(NotConfigured, match="RELAY_DATABASE_URL"):
        bootstrap({"HOME": str(home)})


def test_a_named_file_that_is_not_there_is_said_and_not_passed_over(tmp_path: Path) -> None:
    with pytest.raises(NotConfigured, match="not there"):
        bootstrap(
            {
                "RELAY_CONFIG": str(tmp_path / "missing.toml"),
                "RELAY_DATABASE_URL": "postgresql://d",
                "RELAY_REDIS_URL": "redis://r",
            }
        )


def test_a_roles_profile_is_the_default_with_what_is_said_of_the_role_over_it() -> None:
    settings = {
        "profiles": {
            "default": {"model": "claude-haiku-5-5", "timeout": 60, "budget_usd": "0.5"},
            "builder": {"model": "claude-opus-5-5", "effort": "high"},
        }
    }

    builder, qa = profile_of(settings, "builder"), profile_of(settings, "qa")

    assert (builder.model, builder.effort, builder.timeout) == ("claude-opus-5-5", "high", 60.0)
    assert str(builder.budget_usd) == "0.5"
    assert (qa.model, qa.effort) == ("claude-haiku-5-5", None)
    assert profile_of({}, "qa").budget_usd is None
