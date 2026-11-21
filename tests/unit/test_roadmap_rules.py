"""What a roadmap revision may contain, and which story of a feature may start, once
stories can be rejected and corrected. Rules over recorded facts: no model is asked."""

from relay.domain.story import (
    Candidate,
    Entry,
    Member,
    StoryState,
    is_resolved,
    next_story_to_release,
    why_not_a_revision,
)

APPROVED, PLANNED, REJECTED, RELEASED = (
    StoryState.APPROVED,
    StoryState.PLANNED,
    StoryState.REJECTED,
    StoryState.RELEASED,
)


def kept(*stories: str) -> list[Entry]:
    return [Entry(story, "kept") for story in stories]


def test_a_first_roadmap_is_any_list_of_new_stories() -> None:
    assert why_not_a_revision([], [Entry("S1", "new"), Entry("S2", "new")]) == ""
    assert "at least one story" in why_not_a_revision([], [])


def test_a_revision_accounts_for_every_story_of_the_one_it_revises() -> None:
    effective = [Member("S1", APPROVED), Member("S2", PLANNED)]

    assert why_not_a_revision(effective, kept("S1", "S2")) == ""
    assert "S2" in why_not_a_revision(effective, kept("S1"))
    assert "twice" in why_not_a_revision(effective, kept("S1", "S2", "S2"))
    assert "S9" in why_not_a_revision(effective, [*kept("S1", "S2"), Entry("S9", "kept")])
    assert "already" in why_not_a_revision(effective, [*kept("S1"), Entry("S2", "new")])


def test_only_a_planned_story_may_be_reworded_moved_or_dropped() -> None:
    effective = [Member("S1", APPROVED), Member("S2", RELEASED), Member("S3", PLANNED)]

    for change in ("reworded", "dropped"):
        assert why_not_a_revision(effective, [*kept("S1", "S2"), Entry("S3", change)]) == ""
        assert "S2" in why_not_a_revision(
            effective, [Entry("S1", "kept"), Entry("S2", change), Entry("S3", "kept")]
        )
        assert "S1" in why_not_a_revision(
            effective, [Entry("S1", change), Entry("S2", "kept"), Entry("S3", "kept")]
        )
    assert why_not_a_revision(effective, [Entry("S3", "kept"), *kept("S1", "S2")]) == ""
    assert "order" in why_not_a_revision(effective, kept("S2", "S1", "S3"))


def test_a_rejection_has_exactly_one_correction_until_it_is_resolved_and_never_two() -> None:
    effective = [Member("R", REJECTED), Member("N", PLANNED)]
    correcting = Entry("C", "new", corrects="R")

    assert "R" in why_not_a_revision(effective, kept("R", "N"))
    assert why_not_a_revision(effective, [*kept("R", "N"), correcting]) == ""
    assert "two" in why_not_a_revision(
        effective, [*kept("R", "N"), correcting, Entry("D", "new", corrects="R")]
    )
    assert "rejected" in why_not_a_revision(
        effective, [*kept("R", "N"), correcting, Entry("E", "new", corrects="N")]
    )
    with_c = [Member("R", REJECTED), Member("N", PLANNED), Member("C", PLANNED, corrects="R")]
    assert "R" in why_not_a_revision(with_c, [*kept("R", "N"), Entry("C", "dropped")])
    resolved = [Member("R", REJECTED), Member("C", APPROVED, corrects="R")]
    assert "two" in why_not_a_revision(resolved, [*kept("R", "C"), Entry("D", "new", corrects="R")])
    assert why_not_a_revision(resolved, kept("R", "C")) == ""


def test_a_rejection_is_resolved_when_its_chain_of_corrections_ends_in_an_approval() -> None:
    chain = [
        Member("R", REJECTED),
        Member("C", REJECTED, corrects="R"),
        Member("D", APPROVED, corrects="C"),
    ]

    assert all(is_resolved(story, chain) for story in chain)
    waiting = [*chain[:2], Member("D", PLANNED, corrects="C")]
    assert not any(is_resolved(story, waiting) for story in waiting)
    assert not is_resolved(Member("R", REJECTED), [Member("R", REJECTED)])


def feature(*stories: tuple[str, StoryState, str | None]) -> list[Candidate]:
    return [Candidate("F1", story, state, corrects=corrects) for story, state, corrects in stories]


def test_the_correction_of_a_rejection_starts_and_no_ordinary_story_does_wherever_it_is_put() -> (
    None
):
    for order in (
        [("R", REJECTED, None), ("N", PLANNED, None), ("C", PLANNED, "R")],
        [("N", PLANNED, None), ("R", REJECTED, None), ("C", PLANNED, "R")],
        [("C", PLANNED, "R"), ("R", REJECTED, None), ("N", PLANNED, None)],
    ):
        chosen = next_story_to_release([feature(*order)])
        assert chosen is not None and chosen.story_id == "C", order


def test_ordinary_work_waits_until_the_chain_of_corrections_ends_in_an_approval() -> None:
    def chosen(*stories: tuple[str, StoryState, str | None]) -> str | None:
        story = next_story_to_release([feature(*stories)])
        return story.story_id if story else None

    r, n = ("R", REJECTED, None), ("N", PLANNED, None)
    assert chosen(r, n) is None
    assert chosen(r, ("C", REJECTED, "R"), n) is None
    assert chosen(r, ("C", REJECTED, "R"), ("D", PLANNED, "C"), n) == "D"
    assert chosen(r, ("C", REJECTED, "R"), ("D", APPROVED, "C"), n) == "N"
    assert chosen(r, ("C", APPROVED, "R"), n) == "N"


def test_a_stopped_correction_does_not_start_and_nor_does_anything_ordinary() -> None:
    stuck = [
        Candidate("F1", "R", REJECTED),
        Candidate("F1", "C", PLANNED, stopped=True, corrects="R"),
        Candidate("F1", "N", PLANNED),
    ]
    other = [Candidate("F2", "T", PLANNED)]

    assert next_story_to_release([stuck]) is None
    assert next_story_to_release([stuck, other]) == other[0]
