"""Inclusion rules and the screening taxonomy (spec §3.1).

The rules are enforced in code rather than prose because a prose rule is one
a tired operator can decide not to apply to "just this one" borderline
question. These tests are what make that claim true.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cascade.ledger.rules import MIN_PARTIES, Rejected, derive_cutoff, screen
from cascade.ledger.schema import MIN_HORIZON, RawQuestion, ScenarioRecord
from cascade.ledger.taxonomy import classify_domain, extract_parties, is_single_quantity

OPEN = datetime(2024, 1, 1, tzinfo=UTC)


def raw(**overrides: object) -> RawQuestion:
    base: dict[str, object] = {
        "source": "polymarket",
        "source_ref": "polymarket:test:1",
        "question": "Will the United States, Iran and Israel agree a ceasefire?",
        "resolution_criterion": "YES if all three governments announce a ceasefire.",
        "open_ts": OPEN,
        "close_ts": OPEN + timedelta(days=200),
        "resolved_at": OPEN + timedelta(days=200),
        "outcome": 1,
        "volume": 1_000_000.0,
        "event_sibling_count": 1,
    }
    base.update(overrides)
    return RawQuestion(**base)  # type: ignore[arg-type]


def run(question: RawQuestion, *, min_volume: float = 5000.0) -> ScenarioRecord | Rejected:
    return screen(
        question,
        scenario_id=question.source_ref,
        cutoff_fraction=0.5,
        min_volume=min_volume,
    )


def reason(question: RawQuestion, **kwargs: float) -> str:
    outcome = run(question, **kwargs)  # type: ignore[arg-type]
    assert isinstance(outcome, Rejected), "expected a rejection"
    return outcome.reason


# ---------------------------------------------------------------------------
# Scope: single-quantity questions are out (spec §3.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Will inflation exceed 3% in 2024?",
        "Will the Fed increase interest rates by 25+ bps after the March meeting?",
        "Will Bitcoin reach $100,000 before July?",
        "Will unemployment be above 5% in Q3?",
        "How many seats will the party win?",
        "Will the S&P 500 close above 6000?",
    ],
)
def test_single_quantity_questions_are_out_of_scope(question: str) -> None:
    """These belong to the trend-forecasting literature, not here.

    They have no parties to model, so they compile to a degenerate causal
    graph and would quietly widen the study's claimed scope.
    """
    assert is_single_quantity(question)


@pytest.mark.parametrize(
    "question",
    [
        "Will the United States, Iran and Israel agree a ceasefire?",
        "Will Microsoft complete its acquisition of Activision Blizzard?",
        "Will the UAW reach agreements with Ford, GM and Stellantis?",
    ],
)
def test_multi_party_questions_are_in_scope(question: str) -> None:
    assert not is_single_quantity(question)


def test_a_single_quantity_question_is_rejected_by_the_screen() -> None:
    assert reason(raw(question="Will inflation exceed 3% in 2024?")) == "single_quantity"


# ---------------------------------------------------------------------------
# Parties
# ---------------------------------------------------------------------------


def test_three_named_actors_satisfy_the_party_rule() -> None:
    parties = extract_parties("Will the United States, Iran and Israel agree a ceasefire?")
    assert len(parties) >= MIN_PARTIES


def test_aliases_of_one_actor_count_once() -> None:
    """ "US" and "United States" are one party. Counting both inflates the screen."""
    parties = extract_parties("Will the US and the United States and the White House act?")
    assert parties.count("United States") <= 1
    assert len(parties) < MIN_PARTIES


def test_calendar_words_are_not_parties() -> None:
    """A date must not masquerade as an actor."""
    parties = extract_parties("Will it happen by March, April or December?")
    assert len(parties) < MIN_PARTIES


def test_parties_are_returned_sorted() -> None:
    """Feeds a stored field and, through it, the manifest hash (invariant 7)."""
    parties = extract_parties("Will Ukraine, Russia and Germany sign a treaty?")
    assert list(parties) == sorted(parties)


def test_two_party_questions_are_rejected() -> None:
    """Two-party questions are what a single model already handles."""
    question = raw(
        question="Will Russia and Ukraine sign a treaty?",
        resolution_criterion="YES if a treaty is signed.",
    )
    assert reason(question) == "insufficient_parties"


def test_event_siblings_establish_parties_structurally() -> None:
    """Three mutually exclusive outcomes *are* three parties.

    This is stronger evidence than reading the title, and it is what carries
    the Polymarket half of the set.
    """
    question = raw(
        question="Will candidate Alpha win the election?",
        resolution_criterion="YES if Alpha wins.",
        event_sibling_count=7,
    )
    outcome = run(question)
    assert isinstance(outcome, ScenarioRecord)
    assert outcome.scenario.party_rule == "event_siblings"


def test_two_siblings_do_not_establish_parties() -> None:
    """A two-horse race is a two-party question."""
    question = raw(
        question="Will Alpha win?",
        resolution_criterion="YES if Alpha wins.",
        event_sibling_count=2,
    )
    assert reason(question) == "insufficient_parties"


def test_curated_parties_take_precedence() -> None:
    question = raw(
        source="curated",
        source_ref="curated:x",
        question="Will the parties settle?",
        resolution_criterion="YES if a settlement is announced.",
        curated_parties=("Union", "Employer", "Mediator", "Government"),
        explicit_cutoff=OPEN + timedelta(days=30),
    )
    outcome = run(question)
    assert isinstance(outcome, ScenarioRecord)
    assert outcome.scenario.party_rule == "curated"
    assert outcome.scenario.party_names == ("Employer", "Government", "Mediator", "Union")


# ---------------------------------------------------------------------------
# Cutoff and horizon
# ---------------------------------------------------------------------------


def test_cutoff_sits_proportionally_inside_the_question_life() -> None:
    cutoff = derive_cutoff(open_ts=OPEN, resolve_ts=OPEN + timedelta(days=100), fraction=0.5)
    assert cutoff == OPEN + timedelta(days=50)


def test_cutoff_is_none_for_a_degenerate_interval() -> None:
    """Returning a fabricated timestamp would silently invent a horizon."""
    assert derive_cutoff(open_ts=OPEN, resolve_ts=OPEN, fraction=0.5) is None


@pytest.mark.parametrize("fraction", [0.0, 1.0, -0.1, 1.5])
def test_cutoff_fraction_must_be_strictly_inside_the_unit_interval(fraction: float) -> None:
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        derive_cutoff(open_ts=OPEN, resolve_ts=OPEN + timedelta(days=10), fraction=fraction)


def test_a_short_horizon_is_rejected() -> None:
    """Below 21 days the simulation horizon is not meaningful (spec §3.1)."""
    question = raw(resolved_at=OPEN + timedelta(days=30), close_ts=OPEN + timedelta(days=30))
    # midpoint cutoff leaves 15 days
    assert reason(question) == "horizon_too_short"


def test_the_horizon_rule_matches_the_sql_check_exactly() -> None:
    """Appendix A uses strictly-greater. A row that passed here and failed
    the CHECK would abort the load halfway through."""
    # Exactly 21 days must fail: the constraint is `>`, not `>=`.
    question = raw(
        open_ts=OPEN,
        resolved_at=OPEN + timedelta(days=42),
        close_ts=OPEN + timedelta(days=42),
    )
    outcome = run(question)
    assert isinstance(outcome, Rejected)
    assert outcome.reason == "horizon_too_short"

    longer = raw(
        open_ts=OPEN,
        resolved_at=OPEN + timedelta(days=44),
        close_ts=OPEN + timedelta(days=44),
    )
    accepted = run(longer)
    assert isinstance(accepted, ScenarioRecord)
    assert accepted.scenario.horizon() > MIN_HORIZON


def test_an_explicit_cutoff_overrides_the_proportional_rule() -> None:
    """Curated entries choose their own cutoff; that is the substance of curating."""
    chosen = OPEN + timedelta(days=10)
    question = raw(
        source="curated",
        source_ref="curated:y",
        curated_parties=("A", "B", "C"),
        explicit_cutoff=chosen,
    )
    outcome = run(question)
    assert isinstance(outcome, ScenarioRecord)
    assert outcome.scenario.cutoff_ts == chosen


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------


def test_thin_markets_are_rejected() -> None:
    assert reason(raw(volume=10.0)) == "low_volume"


def test_the_volume_floor_applies_only_to_market_sources() -> None:
    """Metaculus is not a market and the curated set is authored.

    Applying a market activity floor to them would reject every scenario from
    both sources for having no volume to report.
    """
    question = raw(
        source="curated",
        source_ref="curated:z",
        volume=0.0,
        curated_parties=("A", "B", "C"),
        explicit_cutoff=OPEN + timedelta(days=30),
    )
    assert isinstance(run(question), ScenarioRecord)


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Will the ceasefire hold?", "conflict"),
        ("Will Smith win the presidential election?", "elections"),
        ("Will the union end its strike?", "labor"),
        ("Will the FTC approve the merger?", "regulation"),
        ("Will the CEO resign?", "corporate"),
        ("Will the central bank cut rates?", "macro_policy"),
        ("Will the team win the World Cup?", "sports"),
        ("Will the vaccine be approved?", "regulation"),
        ("Something entirely unclassifiable zzz", "other"),
    ],
)
def test_domain_classification(text: str, expected: str) -> None:
    assert classify_domain(text) == expected


def test_a_scenario_carries_no_outcome_field() -> None:
    """Invariant 2 at the type level, before the Postgres grant.

    The simulation reads ``Scenario``. There is no field on it an outcome
    could hide in.
    """
    outcome = run(raw(event_sibling_count=5))
    assert isinstance(outcome, ScenarioRecord)
    assert "outcome" not in outcome.scenario.model_dump()
    assert "outcome" in outcome.label.model_dump()


# ---------------------------------------------------------------------------
# Lexicon hygiene
#
# `named_parties` counts lexicon hits, so a careless entry manufactures
# parties. These guard the lexicon itself rather than the code that reads it.
# ---------------------------------------------------------------------------

# Words that are institutions in one reading and ordinary English in another.
# An entry matching one of these would fire on prose and invent a party.
_AMBIGUOUS_WITH_PROSE = frozenset(
    {
        "Apple",  # allowed: the fruit is lowercase in prose
        "Amazon",
        "Meta",
    }
)

_FORBIDDEN_IN_LEXICON = frozenset(
    {"Shell", "Visa", "Target", "Delta", "United", "House", "Senate ", "Will", "May", "March"}
)


def test_lexicon_entries_are_unique() -> None:
    """A duplicate would not break counting, but it hides an editing mistake."""
    from cascade.ledger.taxonomy import _ACTOR_LEXICON

    assert len(_ACTOR_LEXICON) == len(set(_ACTOR_LEXICON))


def test_lexicon_excludes_words_that_are_ordinary_english() -> None:
    """Bare "Shell", "Visa", "Target", "Delta" and "United" must stay out.

    Each is a company *and* a common word. Matching them would manufacture a
    party from prose -- the same class of defect as counting "YES" (ADR-0009).
    """
    from cascade.ledger.taxonomy import _ACTOR_LEXICON

    offenders = sorted(set(_ACTOR_LEXICON) & _FORBIDDEN_IN_LEXICON)
    assert offenders == [], f"ambiguous lexicon entries: {offenders}"


def test_every_alias_target_is_itself_recognised() -> None:
    """An alias pointing at a non-entry would canonicalise to a dangling name."""
    from cascade.ledger.taxonomy import _ACTOR_ALIASES, _ACTOR_LEXICON

    known = set(_ACTOR_LEXICON)
    for source, target in _ACTOR_ALIASES:
        assert source in known, f"alias source {source!r} is not in the lexicon"
        assert target in known, f"alias target {target!r} is not in the lexicon"


def test_aliases_collapse_to_a_single_party() -> None:
    """Every alias pair must count as one actor, not two."""
    from cascade.ledger.taxonomy import _ACTOR_ALIASES, extract_known_actors

    for source, target in _ACTOR_ALIASES:
        actors = extract_known_actors(f"Talks between {source} and {target} continue.")
        assert len(actors) == 1, f"{source}/{target} counted as {actors}"


def test_ordinary_prose_names_no_parties() -> None:
    """The screen must not fire on text with no institutional actors at all."""
    from cascade.ledger.taxonomy import extract_known_actors

    for prose in (
        "Will the Blaze Star go nova during Blazing Swan 2025?",
        "Will Sean Combs be alive on Jan 1st 2025?",
        "Will the review use more than five em dashes?",
        "Will it rain on Tuesday in the valley?",
    ):
        assert extract_known_actors(prose) == (), prose


def test_merger_review_questions_now_resolve_their_parties() -> None:
    """Spec §3.1 names merger reviews; the lexicon must cover their actors."""
    from cascade.ledger.taxonomy import extract_known_actors

    actors = extract_known_actors(
        "Will Microsoft complete its acquisition of Activision Blizzard "
        "after review by the CMA and the FTC?"
    )
    assert len(actors) >= MIN_PARTIES
    assert "Microsoft" in actors


# ---------------------------------------------------------------------------
# Placeholder legs (M14)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Will Placeholder V be the #1 searched song on Google this year?",
        "Will Candidate B win the 2026 Busan Mayoral Election?",
        "Will Company K be the largest company in the world by market cap on June 30?",
        "Will Person C be the 2025 Drivers Champion?",
        "Will Player BI win the 2026 TOUR Championship?",
        "Will player L record the most assists in the 2025-26 UEFA Champions League?",
        "Will Team B make the first pick of the 2025 NFL Draft?",
    ],
)
def test_a_stand_in_is_not_a_party(question: str) -> None:
    """Each of these was in the sealed set. None is a question about anything."""
    from cascade.ledger.rules import is_placeholder

    assert is_placeholder(question)
    assert reason(raw(question=question, event_sibling_count=12)) == "placeholder_leg"


@pytest.mark.parametrize(
    "question",
    [
        "Will NVIDIA be the largest company in the world by market cap on July 31?",
        "Will Team USA win the most gold medals at the Games?",
        "Will company 3M be acquired by the United States, Iran or Israel?",
        "Will another player win the HLTV Player of the Year award?",
        "Will the company in question be fined by the European Commission?",
    ],
)
def test_real_names_are_not_mistaken_for_stand_ins(question: str) -> None:
    from cascade.ledger.rules import is_placeholder

    assert not is_placeholder(question)


def test_the_placeholder_rule_reads_the_question_and_nothing_else() -> None:
    """Outcome-independent by signature: it is handed only the text."""
    import inspect

    from cascade.ledger.rules import is_placeholder

    assert list(inspect.signature(is_placeholder).parameters) == ["question"]


def test_curated_questions_are_not_screened_for_stand_ins() -> None:
    """The curated file is authored; "Party A" there would be an author's
    choice, reviewed with the rest of the entry, not an exchange's pre-listing."""
    outcome = run(
        raw(
            source="curated",
            question="Will Party A, the United States and Iran sign the accord?",
            curated_parties=("Party A", "United States", "Iran"),
        )
    )
    assert not isinstance(outcome, Rejected) or outcome.reason != "placeholder_leg"


def test_the_domain_comes_from_the_question_not_the_criterions_boilerplate() -> None:
    """Classifying question + criterion put an NFL draft pick under `health`,
    because the criterion said "the player who is selected" -- and would still
    put it under `regulation` on "ruling", which is checked before `sports`."""
    outcome = run(
        raw(
            question="Will Jeremiyah Love be the third pick in the 2026 NFL draft?",
            resolution_criterion=(
                "Resolves YES if the player who is selected third is Love, per the "
                "league's official ruling on the draft order."
            ),
            event_sibling_count=12,
        )
    )
    assert not isinstance(outcome, Rejected)
    assert outcome.scenario.domain == "sports"


def test_the_criterion_still_classifies_a_question_that_says_nothing() -> None:
    outcome = run(
        raw(
            question="Nothing Ever Happens: Iran, Israel and the United States edition",
            resolution_criterion="Resolves YES if no ceasefire is announced.",
            event_sibling_count=12,
        )
    )
    assert not isinstance(outcome, Rejected)
    assert outcome.scenario.domain == "conflict"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Will the WHO declare a pandemic?", "health"),
        ("Will the candidate who wins form a government?", "other"),
    ],
)
def test_who_is_the_organisation_only_in_capitals(text: str, expected: str) -> None:
    assert classify_domain(text) == expected


def test_a_player_prop_is_a_single_quantity() -> None:
    assert is_single_quantity("Max Christie: Points O/U 16.5")
    assert is_single_quantity("Will the over/under on total goals be hit?")
