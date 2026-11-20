"""Identifiers end up in Git ref names and stream names."""

import pytest

from relay.domain.identifiers import require_identifier


@pytest.mark.parametrize("value", ["B1", "vat-calculator", "story_2.b3", "a" * 64])
def test_plain_identifiers_are_accepted(value: str) -> None:
    assert require_identifier("behaviour", value) == value


@pytest.mark.parametrize(
    "value",
    ["", "-B1", ".B1", "B 1", "a/b", "B..1", "P.lock", "B1.", "a" * 65, "B1\n", "caf\u00e9"],
)
def test_anything_git_would_refuse_in_a_ref_is_refused_here_first(value: str) -> None:
    with pytest.raises(ValueError, match="project id"):
        require_identifier("project", value)
