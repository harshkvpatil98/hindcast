"""The outcome-blind relevance measurement (M14).

`cascade retrieval bench --relevance` is how `retrieval.mode` gets decided, so
the things that would quietly bias it are what is tested here: a party name
matching inside another word, an unmeasurable scenario scored as a zero, a
boilerplate "party" pinning both arms at 1.0, an interval that moves between
runs, and a bench query that has drifted from what the call sites really ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from cascade.ledger.schema import Scenario
from cascade.retrieval.bench import (
    ArmPair,
    HybridNotReady,
    hybrid_readiness,
    summarise_relevance,
)
from cascade.retrieval.metrics import (
    generic_names,
    informative,
    mentions,
    mentions_any,
    score_retrieval,
)
from cascade.retrieval.queries import (
    RelevanceQuery,
    agent_evidence_query,
    baseline_evidence_query,
    compiler_evidence_query,
    relevance_queries,
)
from cascade.retrieval.schema import PartitionIndex, RetrievedChunk, SearchResult

CUTOFF = datetime(2025, 6, 1, tzinfo=UTC)


def chunk(
    chunk_id: str,
    body: str,
    *,
    days_old: float = 1.0,
    document_id: str | None = None,
    distance: float = 0.5,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=document_id or f"doc-{chunk_id}",
        ordinal=0,
        body=body,
        published_at=CUTOFF - timedelta(days=days_old),
        source="ccnews",
        url="",
        title="",
        distance=distance,
    )


def result(
    *chunks: RetrievedChunk, mode: str = "vector", terms: tuple[str, ...] = ()
) -> SearchResult:
    return SearchResult(
        as_of=CUTOFF,
        k=6,
        chunks=chunks,
        elapsed_ms=1.0,
        mode=mode,  # type: ignore[arg-type]
        terms=terms,
    )


# ---------------------------------------------------------------------------
# Party matching
# ---------------------------------------------------------------------------


class TestMentions:
    def test_a_name_inside_another_word_is_not_a_mention(self) -> None:
        """The brief's case. 'Apple' in 'pineapple' is a fruit salad, not a party."""
        assert not mentions("Pineapple exports rose sharply last quarter.", "Apple")
        assert not mentions("The applesauce recall widened.", "Apple")
        assert not mentions("Snapple and Grapple merged.", "Apple")

    def test_a_whole_word_is_a_mention_whatever_surrounds_it(self) -> None:
        assert mentions("Apple reported earnings.", "Apple")
        assert mentions("Regulators fined Apple.", "Apple")
        assert mentions("Apple's appeal was heard.", "Apple")
        assert mentions("(Apple) said", "Apple")
        assert mentions("the Apple-Epic dispute", "Apple")

    def test_matching_ignores_case(self) -> None:
        assert mentions("NATO allies met.", "nato")
        assert mentions("nato allies met.", "NATO")

    def test_multi_word_names_match_as_a_phrase(self) -> None:
        name = "Competition and Markets Authority"
        assert mentions("The Competition and Markets Authority opened a review.", name)
        assert mentions("the competition and\nmarkets   authority opened a review", name)
        assert not mentions("Competition in markets worries the authority.", name)
        assert not mentions("the Competition and Markets Authorities", name)

    def test_names_with_punctuation_at_their_edges_still_match(self) -> None:
        r"""`\b` would never match at the edge of 'AT&T' followed by a space."""
        assert mentions("AT&T raised prices.", "AT&T")
        assert not mentions("BAT&TX raised prices.", "AT&T")

    def test_curly_and_straight_apostrophes_are_one_character(self) -> None:
        curly = "Humanity" + chr(0x2019) + "s Last Exam scores rose."
        assert mentions(curly, "Humanity's Last Exam")
        assert mentions(
            "Humanity's Last Exam scores rose.", "Humanity" + chr(0x2019) + "s Last Exam"
        )

    def test_a_blank_name_mentions_nothing(self) -> None:
        """Matching it would make every chunk a hit."""
        assert not mentions("anything at all", "")
        assert not mentions("anything at all", "   ")

    def test_any_of_several_names(self) -> None:
        assert mentions_any("Kroger's bid for Albertsons", ["FTC", "Albertsons"])
        assert not mentions_any("A grocery merger", ["FTC", "Albertsons"])
        assert not mentions_any("A grocery merger", [])


# ---------------------------------------------------------------------------
# Scoring one retrieved set
# ---------------------------------------------------------------------------


class TestScoreRetrieval:
    def test_the_three_metrics_and_the_cost_on_a_hand_built_set(self) -> None:
        found = result(
            chunk("a", "Kroger defends the deal.", days_old=2, document_id="d1", distance=0.2),
            chunk(
                "b", "Albertsons shareholders vote.", days_old=10, document_id="d1", distance=0.4
            ),
            chunk("c", "Supermarket prices rise.", days_old=400, document_id="d2", distance=0.6),
            chunk("d", "A pineapple glut.", days_old=30, document_id="d3", distance=0.8),
        )
        score = score_retrieval(found, ["Kroger", "Albertsons", "Apple"])
        assert score.returned == 4
        assert score.party_mention_rate == pytest.approx(2 / 4)
        assert score.median_age_days == pytest.approx((10 + 30) / 2)
        assert score.distinct_documents == 3
        assert score.mean_distance == pytest.approx(0.5)

    def test_no_party_mentioned_scores_zero(self) -> None:
        found = result(chunk("a", "Weather was mild."), chunk("b", "Pineapple futures fell."))
        assert score_retrieval(found, ["Apple", "FTC"]).party_mention_rate == 0.0

    def test_every_chunk_mentioning_a_party_scores_one(self) -> None:
        found = result(chunk("a", "The FTC sued."), chunk("b", "Apple replied to the FTC."))
        assert score_retrieval(found, ["Apple", "FTC"]).party_mention_rate == 1.0

    def test_no_names_is_unmeasurable_not_zero(self) -> None:
        """Nothing was looked for. 0.0 would read as 'retrieved nothing relevant'."""
        found = result(chunk("a", "The FTC sued."))
        assert score_retrieval(found, []).party_mention_rate is None
        assert score_retrieval(found, ["", "  "]).party_mention_rate is None

    def test_an_empty_result_has_no_age_and_no_rate(self) -> None:
        score = score_retrieval(result(), ["FTC"])
        assert score.returned == 0
        assert score.party_mention_rate is None
        assert score.median_age_days is None
        assert score.mean_distance is None
        assert score.distinct_documents == 0

    def test_age_is_measured_against_the_cutoff_the_set_was_retrieved_under(self) -> None:
        found = result(chunk("a", "x", days_old=0.5))
        assert score_retrieval(found, []).median_age_days == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# The generic-name screen
# ---------------------------------------------------------------------------


class TestGenericNames:
    BODIES: ClassVar[dict[str, list[str]]] = {
        "s1": ["Kroger said other bidders may emerge.", "Kroger filed at 5 PM ET."],
        "s2": ["Iran warned of other steps.", "Tehran replied at 9 PM ET, Iran said."],
        "s3": ["The Ravens have other plans.", "Kickoff is 8 PM ET for the Ravens."],
        "s4": ["Other news today.", "Markets closed at 4 PM ET."],
    }
    NAMES: ClassVar[dict[str, list[str]]] = {
        "s1": ["Kroger", "Other", "PM ET"],
        "s2": ["Iran", "Other", "PM ET"],
        "s3": ["Ravens", "PM ET"],
        "s4": [],
    }

    def test_boilerplate_is_screened_and_real_parties_are_not(self) -> None:
        """'Other' and 'PM ET' appear in chunks retrieved for scenarios that do
        not list them; 'Kroger', 'Iran' and 'Ravens' appear only in their own."""
        generic = generic_names(self.BODIES, self.NAMES, max_background_rate=0.05)
        assert generic == frozenset({"other", "pm et"})

    def test_a_name_only_its_own_scenarios_mention_survives(self) -> None:
        generic = generic_names(self.BODIES, self.NAMES, max_background_rate=0.05)
        assert informative(["Kroger", "Other", "PM ET"], generic) == ("Kroger",)

    def test_the_threshold_is_a_strict_upper_bound_on_the_background_rate(self) -> None:
        bodies = {"mine": ["Acme"], "others": ["Acme once"] + ["nothing"] * 9}
        names = {"mine": ["Acme"], "others": []}
        assert generic_names(bodies, names, max_background_rate=0.10) == frozenset()
        assert generic_names(bodies, names, max_background_rate=0.09) == frozenset({"acme"})

    def test_a_name_every_scenario_lists_has_no_background_and_is_generic(self) -> None:
        bodies = {"a": ["Reuters reports."], "b": ["Reuters again."]}
        names = {"a": ["Reuters"], "b": ["Reuters"]}
        assert generic_names(bodies, names, max_background_rate=0.05) == frozenset({"reuters"})

    def test_names_are_one_name_whatever_their_case(self) -> None:
        bodies = {"a": ["x"], "b": ["other other"], "c": ["another other thing"]}
        names = {"a": ["Other", "OTHER"], "b": [], "c": []}
        assert generic_names(bodies, names, max_background_rate=0.05) == frozenset({"other"})

    def test_the_rate_must_be_a_rate(self) -> None:
        with pytest.raises(ValueError):
            generic_names({}, {}, max_background_rate=0.0)


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------


def relevance_query(scenario_id: str, kind: str = "baseline") -> RelevanceQuery:
    return RelevanceQuery(
        scenario_id=scenario_id,
        kind=kind,  # type: ignore[arg-type]
        query=compiler_evidence_query("Will it happen?"),
        as_of=CUTOFF,
        k=6,
    )


def pairs_where_hybrid_names_the_party() -> tuple[list[ArmPair], dict[str, tuple[str, ...]]]:
    """Twelve scenarios. Vector finds one on-party chunk in three; hybrid finds
    two, fresher and from more documents, a little further from the query."""
    pairs = []
    names: dict[str, tuple[str, ...]] = {}
    for index in range(12):
        party = f"Party{index:02d}"
        sid = f"s{index:02d}"
        names[sid] = (party, "Other")
        vector = result(
            chunk(
                f"{sid}-v0", f"{party} spoke.", days_old=300, document_id=f"{sid}-d0", distance=0.30
            ),
            chunk(
                f"{sid}-v1",
                "Some other thing.",
                days_old=400,
                document_id=f"{sid}-d0",
                distance=0.32,
            ),
            chunk(
                f"{sid}-v2",
                "Another other item.",
                days_old=500,
                document_id=f"{sid}-d1",
                distance=0.34,
            ),
        )
        hybrid = result(
            chunk(
                f"{sid}-v0", f"{party} spoke.", days_old=300, document_id=f"{sid}-d0", distance=0.30
            ),
            chunk(
                f"{sid}-h1", f"{party} replied.", days_old=3, document_id=f"{sid}-d2", distance=0.40
            ),
            chunk(
                f"{sid}-h2", "An other matter.", days_old=5, document_id=f"{sid}-d3", distance=0.45
            ),
            mode="hybrid",
            terms=(party.lower(),),
        )
        pairs.append(ArmPair(query=relevance_query(sid), vector=vector, hybrid=hybrid))
    return pairs, names


def summarise(pairs: list[ArmPair], names: dict[str, tuple[str, ...]], **overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "party_names": names,
        "salt": "unit-test-salt",
        "b_resamples": 2000,
        "max_background_rate": 0.05,
        "graphs": 0,
        "elapsed_s": 0.0,
    }
    arguments.update(overrides)
    return summarise_relevance(pairs, **arguments)  # type: ignore[arg-type]


class TestSummary:
    def test_both_readings_of_the_mention_rate_are_reported(self) -> None:
        """'Other' is in every chunk, so over all registry names both arms score
        1.0 and the difference vanishes. Over the informative names it shows."""
        pairs, names = pairs_where_hybrid_names_the_party()
        report = summarise(pairs, names)
        (kind,) = report.kinds
        by_reading = {
            item.names: item for item in kind.comparisons if item.metric == "party_mention_rate"
        }
        literal, screened = by_reading["all registry names"], by_reading["informative names"]

        assert report.generic == ("other",)
        assert literal.vector_mean == literal.hybrid_mean == 1.0
        assert literal.interval is not None and literal.interval.point == 0.0

        assert screened.vector_mean == pytest.approx(1 / 3)
        assert screened.hybrid_mean == pytest.approx(2 / 3)
        assert screened.interval is not None
        assert screened.interval.point == pytest.approx(1 / 3)
        assert screened.n_paired == 12

    def test_the_other_metrics_and_the_cost_are_beside_it(self) -> None:
        pairs, names = pairs_where_hybrid_names_the_party()
        (kind,) = summarise(pairs, names).kinds
        by_metric = {item.metric: item for item in kind.comparisons if item.names == "-"}
        assert by_metric["median_age_days"].vector_mean == pytest.approx(400)
        assert by_metric["median_age_days"].hybrid_mean == pytest.approx(5)
        assert by_metric["distinct_documents"].vector_mean == pytest.approx(2)
        assert by_metric["distinct_documents"].hybrid_mean == pytest.approx(3)
        cost = by_metric["mean_distance"]
        assert cost.interval is not None and cost.interval.point > 0, (
            "the fixture's hybrid arm sits further from the query; a report that "
            "did not show that would show only the benefits"
        )
        assert kind.mean_overlap == pytest.approx(1 / 5)
        assert kind.queries_without_terms == 0

    def test_the_interval_is_a_function_of_the_salt(self) -> None:
        """Same salt, same interval, to the last bit -- a report that moved between
        runs could not show a regression. A different salt resamples differently."""
        pairs, names = pairs_where_hybrid_names_the_party()
        # Twelve different age gaps, so the resampled mean is not confined to a
        # handful of values every seed would land on alike.
        varied = [
            ArmPair(
                query=pair.query,
                vector=pair.vector,
                hybrid=result(
                    chunk(f"h{index}", "x", days_old=1.0 + index * index * 1.7, distance=0.4),
                    mode="hybrid",
                ),
            )
            for index, pair in enumerate(pairs)
        ]
        first = summarise(varied, names).kinds[0].comparisons
        again = summarise(varied, names).kinds[0].comparisons
        other = summarise(varied, names, salt="a-different-salt").kinds[0].comparisons
        assert first == again
        assert [item.interval for item in first] != [item.interval for item in other]

    def test_a_scenario_with_no_usable_name_is_counted_not_scored(self) -> None:
        pairs, names = pairs_where_hybrid_names_the_party()
        names["s00"] = ("Other",)
        report = summarise(pairs, names)
        screened = next(
            item
            for item in report.kinds[0].comparisons
            if item.metric == "party_mention_rate" and item.names == "informative names"
        )
        assert screened.n_paired == 11
        assert screened.unmeasurable == 1
        assert report.scenarios_without_informative_names == 1

    def test_queries_of_one_scenario_are_averaged_before_pairing(self) -> None:
        """A dozen agent queries share a question; resampling them as independent
        units would shrink every interval. The scenario is the unit."""
        pairs, names = pairs_where_hybrid_names_the_party()
        tripled = [
            ArmPair(
                query=relevance_query(pair.query.scenario_id, "agent"),
                vector=pair.vector,
                hybrid=pair.hybrid,
            )
            for pair in pairs
            for _ in range(3)
        ]
        (kind,) = summarise(tripled, names).kinds
        assert kind.queries == 36
        assert kind.scenarios == 12
        assert all(item.n_paired + item.unmeasurable == 12 for item in kind.comparisons)

    def test_kinds_are_reported_separately(self) -> None:
        pairs, names = pairs_where_hybrid_names_the_party()
        mixed = pairs + [
            ArmPair(
                query=relevance_query(pair.query.scenario_id, "compiler"),
                vector=pair.vector,
                hybrid=pair.hybrid,
            )
            for pair in pairs[:4]
        ]
        report = summarise(mixed, names)
        assert [kind.kind for kind in report.kinds] == ["compiler", "baseline"]
        assert [kind.scenarios for kind in report.kinds] == [4, 12]

    def test_hybrid_queries_that_found_no_term_are_counted(self) -> None:
        pairs, names = pairs_where_hybrid_names_the_party()
        pairs[0] = ArmPair(
            query=pairs[0].query, vector=pairs[0].vector, hybrid=result(mode="hybrid", terms=())
        )
        assert summarise(pairs, names).kinds[0].queries_without_terms == 1


# ---------------------------------------------------------------------------
# Readiness: exit 3 rather than a bench that runs for days
# ---------------------------------------------------------------------------


def partition(name: str, rows: int, fts: bool) -> PartitionIndex:
    return PartitionIndex(
        partition=name,
        rows=rows,
        index_name=f"{name}_embedding_hnsw_idx",
        m=16,
        ef_construction=64,
        fts_index_name=f"{name}_body_fts_idx" if fts else None,
    )


class TestReadiness:
    def test_ready_when_deployed_and_every_non_empty_partition_is_indexed(self) -> None:
        partitions = [partition("chunks_2024q1", 10, True), partition("chunks_2027q4", 0, False)]
        assert hybrid_readiness(deployed=True, partitions=partitions) == ()

    def test_a_missing_index_is_named_with_the_command_that_builds_it(self) -> None:
        partitions = [partition("chunks_2024q1", 10, True), partition("chunks_2024q2", 99, False)]
        (reason,) = hybrid_readiness(deployed=True, partitions=partitions)
        assert "chunks_2024q2" in reason
        assert "cascade retrieval index --fts" in reason

    def test_an_absent_function_is_named_with_its_migration(self) -> None:
        (reason,) = hybrid_readiness(deployed=False, partitions=[])
        assert "018" in reason and "cascade db migrate" in reason

    def test_the_exception_carries_every_reason(self) -> None:
        error = HybridNotReady(["no function", "no index"])
        assert error.reasons == ("no function", "no index")
        assert "no function; no index" in str(error)


# ---------------------------------------------------------------------------
# The bench measures the study's own queries
# ---------------------------------------------------------------------------


def scenario(scenario_id: str, question: str = "Will the FTC block Kroger?") -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        question=question,
        resolution_criterion="YES if a court enjoins the deal by 12:00 PM ET. Source: Associated Press.",
        cutoff_ts=CUTOFF,
        resolve_ts=CUTOFF + timedelta(days=90),
        domain="regulation",
        source="curated",
        source_ref=scenario_id,
        party_rule="curated",
        party_names=("FTC", "Kroger"),
        event_group=None,
    )


@dataclass(frozen=True)
class FakeActor:
    id: str
    name: str
    objective: str


@dataclass(frozen=True)
class FakeGraph:
    scenario_id: str
    actors: tuple[FakeActor, ...]


class TestQueries:
    def test_the_embedded_text_is_what_each_call_site_always_embedded(self) -> None:
        """Byte for byte. `retrieval.mode: vector` must retrieve exactly what it
        did before this module existed, or recorded runs stop replaying."""
        question, criterion = "Will the FTC block Kroger?", "YES if enjoined."
        name, objective = "Kroger", "Close the merger before financing lapses."
        assert compiler_evidence_query(question).text == question
        assert (
            agent_evidence_query(question, name, objective).text == f"{question} {name} {objective}"
        )
        assert baseline_evidence_query(question, criterion).text == f"{question} {criterion}"

    def test_resolution_boilerplate_never_reaches_the_keyword_terms(self) -> None:
        """It names the resolver and the deadline, not the parties."""
        query = baseline_evidence_query("Will the FTC block Kroger?", "Source: Associated Press.")
        assert "Associated Press" in query.text
        assert "Associated Press" not in query.keyword_text

    def test_the_actor_name_is_an_explicit_entity(self) -> None:
        query = agent_evidence_query("Will the FTC block Kroger?", "Labour Party", "Keep jobs.")
        assert query.entities == ("Labour Party",)
        assert "\n" in query.keyword_text, "a run must not span the question and the objective"

    def test_the_query_set_is_ordered_and_covers_every_kind(self) -> None:
        graph = FakeGraph(
            "s-b",
            (FakeActor("z_union", "UFCW", "Protect jobs."), FakeActor("a_ftc", "FTC", "Block it.")),
        )
        queries = relevance_queries(
            [scenario("s-b"), scenario("s-a")], [graph], k_agent=6, k_compiler=60
        )
        assert [(item.scenario_id, item.kind, item.k) for item in queries] == [
            ("s-a", "compiler", 60),
            ("s-a", "baseline", 6),
            ("s-b", "compiler", 60),
            ("s-b", "baseline", 6),
            ("s-b", "agent", 6),
            ("s-b", "agent", 6),
        ]
        assert [item.query.entities for item in queries[4:]] == [("FTC",), ("UFCW",)]
        assert all(item.as_of == CUTOFF for item in queries)

    def test_a_scenario_without_a_graph_gets_no_invented_agent_queries(self) -> None:
        queries = relevance_queries([scenario("s-a")], [], k_agent=6, k_compiler=60)
        assert [item.kind for item in queries] == ["compiler", "baseline"]

    def test_two_graphs_for_one_scenario_are_refused(self) -> None:
        graph = FakeGraph("s-a", ())
        with pytest.raises(ValueError, match="two graphs"):
            relevance_queries([scenario("s-a")], [graph, graph], k_agent=6, k_compiler=60)

    def test_a_naive_cutoff_cannot_be_benchmarked(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            RelevanceQuery(
                scenario_id="s",
                kind="compiler",
                query=compiler_evidence_query("q"),
                as_of=datetime(2025, 6, 1),
                k=6,
            )
