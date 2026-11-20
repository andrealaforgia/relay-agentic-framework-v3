"""Where a Builder's candidate is published. The name is built by Relay from a project, a
behaviour and a whole number, so a caller cannot steer it to another ref."""

import pytest

from relay.git.checkout import CheckoutFailed, attempt_ref


def test_an_attempt_has_its_own_ref() -> None:
    assert attempt_ref("vat", "B1", 2) == "refs/relay/attempts/vat/B1/2"


@pytest.mark.parametrize("attempt", [0, -1, True, 1.0, "1", "refs/heads/main", "../../heads/main"])
def test_only_a_positive_whole_number_names_an_attempt(attempt: object) -> None:
    with pytest.raises(CheckoutFailed, match="positive whole number"):
        attempt_ref("vat", "B1", attempt)  # type: ignore[arg-type]


@pytest.mark.parametrize("behaviour_id", ["", "-B1", "B1/../../heads", "B 1", "B1\n"])
def test_a_behaviour_id_that_could_reshape_the_ref_is_refused(behaviour_id: str) -> None:
    with pytest.raises(CheckoutFailed, match="ref name"):
        attempt_ref("vat", behaviour_id, 1)


@pytest.mark.parametrize("project_id", ["", "vat/../..", "a/b", "-vat"])
def test_a_project_id_that_could_reshape_the_ref_is_refused(project_id: str) -> None:
    with pytest.raises(CheckoutFailed, match="ref name"):
        attempt_ref(project_id, "B1", 1)
