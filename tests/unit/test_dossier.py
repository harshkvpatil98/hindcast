"""The scenario dossier (ADR-0037).

The dossier lets a model summarise far more evidence than any prompt carried
before, and in doing so opens a channel for that model's memory of how things
turned out to reach the agents dressed as retrieved fact. Most of this file is
about the check that closes it: what :func:`verify` refuses, and that it
refuses without a model. No real model is involved anywhere here, so nothing
below says whether dossiers are *good* -- only that an unsupported claim cannot
get into a prompt.
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cascade.config import Settings
from cascade.decompose import dossier as dossier_module
from cascade.decompose.dossier import (
    DOSSIER_TOOL,
    DOSSIER_TOOL_NAME,
    SECTIONS,
    Claim,
    Dossier,
    DossierDraft,
    DossierWriter,
    Excerpt,
    Hit,
    dossier_hash,
    pool_evidence,
    queries_for,
    render,
    user_prompt,
    verify,
)
from cascade.decompose.prompts import draft_user_prompt
from cascade.eval.baselines import baseline_prompt
from cascade.ledger.schema import Scenario
from cascade.sim.prompts import ActorBrief, persona_block

CUTOFF = datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


def scenario(**overrides: Any) -> Scenario:
    fields: dict[str, Any] = {
        "scenario_id": "s1",
        "question": "Will the FTC block the Acme and Globex merger before July?",
        "resolution_criterion": "Resolves YES if the FTC formally blocks the merger.",
        "cutoff_ts": CUTOFF,
        "resolve_ts": datetime(2026, 7, 1, tzinfo=UTC),
        "domain": "corporate",
        "source": "polymarket",
        "source_ref": "ref",
        "party_rule": "named_parties",
        "party_names": ("Acme", "FTC", "Globex"),
        "event_group": None,
    }
    fields.update(overrides)
    return Scenario(**fields)


def excerpt(
    index: int, body: str, *, days_before: int = 10, document: str | None = None
) -> Excerpt:
    return Excerpt(
        index=index,
        chunk_id=f"c{index}",
        document_id=document or f"d{index}",
        published_at=CUTOFF - timedelta(days=days_before),
        source="ccnews",
        body=body,
    )


POOL = (
    excerpt(
        1,
        "The Federal Trade Commission opened an in-depth review of Acme's proposed "
        "$4.2 billion acquisition of Globex, a spokesperson said on Tuesday.",
        days_before=40,
    ),
    excerpt(
        2,
        "Acme chief executive Dana Reyes said the company would litigate if regulators "
        "sued to stop the deal. A hearing is scheduled for April 20.",
        days_before=9,
    ),
)


def check(
    draft: DossierDraft, pool: tuple[Excerpt, ...] = POOL, **overrides: Any
) -> tuple[Dossier, tuple[Any, ...]]:
    options: dict[str, Any] = {"min_support_ratio": 0.6, "max_claims_per_section": 12}
    options.update(overrides)
    return verify(draft, scenario=scenario(), excerpts=pool, **options)


def reasons(dropped: tuple[Any, ...]) -> list[str]:
    return [item.reason.split(":")[0] for item in dropped]


# ---------------------------------------------------------------------------
# verify: what gets through
# ---------------------------------------------------------------------------


def test_a_claim_the_cited_excerpt_states_is_kept() -> None:
    draft = DossierDraft(
        positions=[
            Claim(text="Dana Reyes said Acme would litigate if regulators sued.", cites=(2,))
        ]
    )
    dossier, dropped = check(draft)
    assert [claim.text for claim in dossier.positions] == [draft.positions[0].text]
    assert dropped == ()


def test_a_scheduled_future_event_is_reportable_as_status_when_the_excerpt_announces_it() -> None:
    """Knowing on the cutoff that a hearing is set for April is pre-cutoff
    knowledge. Refusing every future date would discard exactly the facts a
    forecaster most needs."""
    draft = DossierDraft(status=[Claim(text="A hearing is scheduled for April 20.", cites=(2,))])
    dossier, dropped = check(draft)
    assert len(dossier.status) == 1
    assert dropped == ()


def test_the_documents_own_date_can_carry_a_claims_date() -> None:
    """The excerpt says "on Tuesday"; its publication date says which one."""
    published = POOL[0].published_at
    draft = DossierDraft(
        timeline=[
            Claim(
                text="The Federal Trade Commission opened an in-depth review of the acquisition.",
                cites=(1,),
                on=published.strftime("%Y-%m-%d"),
            )
        ]
    )
    dossier, dropped = check(draft)
    assert len(dossier.timeline) == 1, dropped


# ---------------------------------------------------------------------------
# verify: what is refused -- the laundering channel
# ---------------------------------------------------------------------------


def test_a_name_the_cited_excerpt_does_not_contain_is_refused() -> None:
    """The writer "knows" who chaired the agency. The excerpts never say."""
    draft = DossierDraft(
        positions=[
            Claim(text="FTC chair Lina Khan said the agency would review the deal.", cites=(1,))
        ]
    )
    dossier, dropped = check(draft)
    assert dossier.positions == ()
    assert [item.reason for item in dropped] == ["unsupported:lina"]


def test_a_number_the_cited_excerpt_does_not_contain_is_refused() -> None:
    draft = DossierDraft(
        status=[Claim(text="The acquisition is valued at $5.1 billion.", cites=(1,))]
    )
    _, dropped = check(draft)
    assert [item.reason for item in dropped] == ["unsupported:5.1"]


def test_support_comes_from_the_cited_excerpts_only() -> None:
    """Excerpt 2 names Dana Reyes; a claim citing only excerpt 1 cannot lean on it.
    Otherwise one relevant excerpt would license any claim about anything."""
    draft = DossierDraft(
        positions=[Claim(text="Dana Reyes welcomed the in-depth review of the deal.", cites=(1,))]
    )
    _, dropped = check(draft)
    assert reasons(dropped) == ["unsupported"]


def test_hindsight_wording_over_supported_names_is_refused_by_the_support_ratio() -> None:
    """Every name here is in the record; almost none of the *content* is."""
    draft = DossierDraft(
        status=[
            Claim(
                text="Acme ultimately abandoned the Globex takeover after courts sided "
                "with opponents.",
                cites=(1,),
            )
        ]
    )
    _, dropped = check(draft)
    assert reasons(dropped) == ["weak_support"]


def test_a_citation_to_an_excerpt_that_was_never_offered_is_refused() -> None:
    draft = DossierDraft(status=[Claim(text="The FTC opened an in-depth review.", cites=(1, 7))])
    _, dropped = check(draft)
    assert [item.reason for item in dropped] == ["bad_citation:7"]


@pytest.mark.parametrize(
    ("on", "kept"),
    [
        ("2026-03-14", True),
        ("2026-03-15", False),  # the cutoff's own day: cannot be shown to precede 12:00
        ("2026-03-16", False),
        ("2026-03", True),  # the cutoff's month, carried by the support check
        ("2026-04", False),
    ],
)
def test_a_timeline_entry_must_be_dated_before_the_cutoff(on: str, kept: bool) -> None:
    body = "The FTC opened an in-depth review in March 2026, a spokesperson said."
    pool = (excerpt(1, body, days_before=1),)
    draft = DossierDraft(
        timeline=[Claim(text="The FTC opened an in-depth review.", cites=(1,), on=on)]
    )
    dossier, dropped = check(draft, pool)
    assert (len(dossier.timeline) == 1) is kept, dropped
    if not kept:
        assert reasons(dropped) == ["dated_after_cutoff"]


def test_a_timeline_entry_needs_a_date() -> None:
    draft = DossierDraft(timeline=[Claim(text="The FTC opened an in-depth review.", cites=(1,))])
    _, dropped = check(draft)
    assert reasons(dropped) == ["undated_timeline_entry"]


def test_the_year_of_a_claimed_date_must_be_in_the_record() -> None:
    draft = DossierDraft(
        timeline=[Claim(text="The FTC opened an in-depth review.", cites=(1,), on="2024-11-02")]
    )
    _, dropped = check(draft)
    assert [item.reason for item in dropped] == ["unsupported:2024"]


def test_an_excerpt_dated_at_the_cutoff_is_a_defect_not_a_filter() -> None:
    late = Excerpt(
        index=1,
        chunk_id="c1",
        document_id="d1",
        published_at=CUTOFF,
        source="ccnews",
        body="The FTC blocked the merger.",
    )
    with pytest.raises(ValueError, match="not before the cutoff"):
        check(DossierDraft(), (late,))


def test_refused_and_duplicate_claims_are_reported_not_lost() -> None:
    claim = Claim(text="The FTC opened an in-depth review of the acquisition.", cites=(1,))
    draft = DossierDraft(status=[claim, claim, claim])
    dossier, dropped = check(draft, max_claims_per_section=12)
    assert len(dossier.status) == 1
    assert reasons(dropped) == ["duplicate", "duplicate"]


def test_a_section_over_its_limit_reports_what_it_cut() -> None:
    claims = [
        Claim(text=f"The FTC opened an in-depth review, item {4.2}, {word}.", cites=(1,))
        for word in ("acquisition", "review", "spokesperson")
    ]
    dossier, dropped = check(DossierDraft(status=claims), max_claims_per_section=2)
    assert len(dossier.status) == 2
    assert reasons(dropped) == ["over_section_limit"]


def test_verify_is_pure() -> None:
    """No model, no clock, no I/O: the check must be reproducible by anyone
    from the stored dossier and the corpus alone."""
    tree = ast.parse((REPO / "cascade/decompose/dossier.py").read_text())
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    forbidden = {"time", "random", "os", "pathlib", "httpx", "psycopg", "anthropic", "subprocess"}
    assert not (imported & forbidden)
    assert "cascade.llm.client" not in imported


# ---------------------------------------------------------------------------
# Pooling
# ---------------------------------------------------------------------------


def hit(chunk: str, *, document: str | None = None, days_before: int = 5) -> Hit:
    return Hit(
        chunk_id=chunk,
        document_id=document or f"doc-{chunk}",
        published_at=CUTOFF - timedelta(days=days_before),
        source="ccnews",
        body=f"body of {chunk}",
    )


def pool(lists: list[list[Hit]], **overrides: Any) -> tuple[Excerpt, ...]:
    options: dict[str, Any] = {
        "as_of": CUTOFF,
        "limit": 10,
        "max_per_document": 3,
        "excerpt_chars": 100,
    }
    options.update(overrides)
    return pool_evidence(lists, **options)


def test_pooling_interleaves_by_rank_so_every_query_is_heard() -> None:
    first = [hit("a1", days_before=1), hit("a2", days_before=2), hit("a3", days_before=3)]
    second = [hit("b1", days_before=4), hit("b2", days_before=5)]
    chosen = {item.chunk_id for item in pool([first, second], limit=3)}
    assert chosen == {"a1", "b1", "a2"}


def test_pooling_dedupes_and_caps_chunks_per_document() -> None:
    shared = [hit(f"x{i}", document="long-article", days_before=i + 1) for i in range(5)]
    chosen = pool([shared, [shared[0], hit("y")]], max_per_document=2)
    assert sorted(item.chunk_id for item in chosen) == ["x0", "x1", "y"]


def test_the_pool_is_numbered_in_publication_order() -> None:
    chosen = pool([[hit("new", days_before=1), hit("old", days_before=30)]])
    assert [(item.index, item.chunk_id) for item in chosen] == [(1, "old"), (2, "new")]


def test_pooling_raises_on_a_hit_at_or_after_the_cutoff() -> None:
    with pytest.raises(ValueError, match="time lock failed upstream"):
        pool([[hit("late", days_before=0)]])


@given(st.permutations(["a", "b", "c", "d"]), st.integers(min_value=1, max_value=4))
def test_pooling_is_a_function_of_its_inputs(order: list[str], limit: int) -> None:
    lists = [[hit(name, days_before=ord(name) - 90) for name in order]]
    assert pool(lists, limit=limit) == pool(lists, limit=limit)
    assert len(pool(lists, limit=limit)) == limit


def test_queries_use_only_the_scenarios_own_text() -> None:
    queries = queries_for(scenario(), max_party_queries=2)
    assert queries[0] == scenario().question
    assert sum(1 for query in queries if query.startswith(("Acme ", "FTC "))) == 2
    assert not any(query.startswith("Globex ") for query in queries)
    assert len(set(queries)) == len(queries)


# ---------------------------------------------------------------------------
# Rendering and hashing
# ---------------------------------------------------------------------------


def built() -> Dossier:
    draft = DossierDraft(
        timeline=[
            Claim(
                text="The Federal Trade Commission opened an in-depth review.",
                cites=(1,),
                on=POOL[0].published_at.strftime("%Y-%m-%d"),
            )
        ],
        positions=[
            Claim(text="Dana Reyes said Acme would litigate if regulators sued.", cites=(2,))
        ],
        status=[Claim(text="A hearing is scheduled for April 20.", cites=(2,))],
    )
    dossier, dropped = check(draft)
    assert dropped == ()
    return dossier


def test_render_is_stable_and_carries_dates_but_not_citation_numbers() -> None:
    text = render(built(), max_chars=4000)
    assert text == render(built(), max_chars=4000)
    assert POOL[0].published_at.strftime("%Y-%m-%d") in text
    assert not re.search(r"\[\d+\]", text)


def test_render_over_budget_drops_the_oldest_history_first() -> None:
    """What happened last and where things stand is what a forecaster cannot
    do without; the distant past is what goes."""
    older = Claim(text="The FTC opened an in-depth review.", cites=(1,), on="2026-02-03")
    newer = Claim(text="Dana Reyes said Acme would litigate.", cites=(2,), on="2026-03-06")
    dossier = built().model_copy(update={"timeline": (older, newer)})
    full = render(dossier, max_chars=4000)
    tight = render(dossier, max_chars=len(full) - 10)
    assert len(tight) <= len(full) - 10
    assert "A hearing is scheduled" in tight
    assert newer.text in tight
    assert older.text not in tight


def test_an_empty_dossier_renders_as_nothing() -> None:
    dossier, _ = check(DossierDraft())
    assert render(dossier, max_chars=4000) == ""


def test_the_hash_covers_claims_and_the_evidence_pool() -> None:
    base = built()
    assert dossier_hash(base) == dossier_hash(built())
    edited = base.model_copy(update={"status": ()})
    repooled = base.model_copy(update={"evidence": base.evidence[:1]})
    assert len({dossier_hash(base), dossier_hash(edited), dossier_hash(repooled)}) == 3


# ---------------------------------------------------------------------------
# The prompts: off means byte-identical
# ---------------------------------------------------------------------------


def _draft(situation: str = "") -> str:
    kwargs: dict[str, Any] = {"situation": situation} if situation else {}
    return draft_user_prompt(
        question="Q?",
        resolution_criterion="R.",
        cutoff_iso="2026-03-15T12:00:00Z",
        party_names=("Acme",),
        chunks=[("2026-03-01", "ccnews", "body")],
        **kwargs,
    )


def _brief(situation: str = "") -> ActorBrief:
    return ActorBrief(
        actor_id="a1",
        name="Acme",
        objective="Close the deal.",
        risk_posture="neutral",
        constraints=(),
        utility=(("f1", 1.0),),
        levers=(("f1", 0.5),),
        counterparties=("a2",),
        horizon=24,
        question_context="Q?",
        evidence=(("2026-03-01", "ccnews", "body"),),
        situation=situation,
    )


def test_with_the_dossier_off_every_prompt_is_byte_identical_to_before() -> None:
    """Recordings made before the dossier existed must keep resolving, and an
    "off" arm that differed from the old prompt by one heading would make the
    on/off comparison a three-way one."""
    assert "Situation report" not in _draft()
    assert "Situation report" not in persona_block(_brief(), evidence_chars=900)
    assert "Situation report" not in baseline_prompt(scenario(), (), evidence_chars=900)
    # The section, when present, is an insertion and nothing else.
    report = "## Where things stood at the cutoff\n- A hearing is scheduled."
    for off, on in (
        (_draft(), _draft(report)),
        (
            persona_block(_brief(), evidence_chars=900),
            persona_block(_brief(report), evidence_chars=900),
        ),
        (
            baseline_prompt(scenario(), (), evidence_chars=900),
            baseline_prompt(scenario(), (), evidence_chars=900, situation=report),
        ),
    ):
        start = on.index("# Situation report")
        end = on.index(report) + len(report)
        assert on[:start] + on[end:].lstrip("\n") == off


def test_the_writer_is_told_the_cutoff_and_sees_numbered_dated_excerpts() -> None:
    prompt = user_prompt(scenario(), POOL)
    assert "2026-03-15T12:00:00" in prompt
    assert f"[1] {POOL[0].published_at.date().isoformat()}" in prompt
    assert prompt.index("[1] ") < prompt.index("[2] ")


def test_the_tool_schema_is_generated_from_the_model() -> None:
    assert DOSSIER_TOOL["input_schema"] == DossierDraft.model_json_schema()
    assert set(SECTIONS) == set(DossierDraft.model_fields)


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------


class Result:
    def __init__(self, payload: Any) -> None:
        self.tool_calls = [{"type": "tool_use", "name": DOSSIER_TOOL_NAME, "input": payload}]
        self.text = ""
        self.stop_reason = "tool_use"


class Client:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.requests: list[Any] = []

    def complete(self, request: Any, *, trace_name: str = "", **_: Any) -> Any:
        self.requests.append(request)
        return Result(self.payload)


def test_the_writer_searches_inside_the_time_lock_and_verifies_the_answer(
    settings: Settings,
) -> None:
    asked: list[tuple[str, datetime, int]] = []

    def search(query: str, as_of: datetime, k: int) -> list[Hit]:
        asked.append((query, as_of, k))
        return [
            Hit(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                published_at=item.published_at,
                source=item.source,
                body=item.body,
            )
            for item in POOL
        ]

    payload = {
        "status": [
            {"text": "A hearing is scheduled for April 20.", "cites": [2]},
            {"text": "The FTC blocked the merger on June 3.", "cites": [2]},
            {"text": "No citation at all here, which is malformed.", "cites": []},
        ]
    }
    client = Client(payload)
    outcome = DossierWriter(settings=settings, client=client, search=search).write(scenario())

    assert {as_of for _, as_of, _ in asked} == {CUTOFF}
    assert {k for _, _, k in asked} == {settings.dossier.per_query_k}
    assert [claim.text for claim in outcome.dossier.status] == [
        "A hearing is scheduled for April 20."
    ]
    assert sorted(reasons(outcome.dropped)) == ["malformed", "unsupported"]
    assert outcome.llm_calls == 1
    assert outcome.dossier_sha256 == dossier_hash(outcome.dossier)
    request = client.requests[0]
    assert request.model == settings.models.compiler
    assert request.tool_choice == {"type": "tool", "name": DOSSIER_TOOL_NAME}


def test_an_empty_pool_costs_no_model_call(settings: Settings) -> None:
    client = Client({})
    outcome = DossierWriter(
        settings=settings, client=client, search=lambda query, as_of, k: []
    ).write(scenario())
    assert client.requests == []
    assert outcome.llm_calls == 0
    assert outcome.dossier.n_claims == 0


def test_search_has_no_default_as_of() -> None:
    """Invariant 1 reaches the new call path: the writer's search is positional."""
    source = (REPO / "cascade/decompose/dossier.py").read_text()
    assert "as_of=None" not in source and "as_of: datetime =" not in source
    assert dossier_module.Search is not None


# ---------------------------------------------------------------------------
# Which prompts get it: all or nothing, and never an ungrounded cell
# ---------------------------------------------------------------------------


def enabled(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "dossier": settings.dossier.model_copy(update={"enabled": True}),
            "llm": settings.llm.model_copy(update={"prompt_rev": "r3"}),
        }
    )


def explode(*_: Any, **__: Any) -> Any:
    raise AssertionError("the dossier table must not be read here")


def test_with_the_dossier_off_the_table_is_never_read(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cascade.decompose import dossier_store

    monkeypatch.setattr(dossier_store, "load_dossiers", explode)
    assert dossier_store.situations_for(settings, ("s2", "s1")) == {
        "s1": ("", None),
        "s2": ("", None),
    }


def test_enabled_with_a_scenario_missing_is_an_error_not_an_empty_report(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cascade.decompose import dossier_store

    stored = {"s1": (built(), dossier_hash(built()))}
    monkeypatch.setattr(dossier_store, "load_dossiers", lambda *_, **__: stored)
    found = dossier_store.situations_for(enabled(settings), ("s1",))
    assert found["s1"] == (
        render(built(), max_chars=settings.dossier.prompt_chars),
        dossier_hash(built()),
    )
    with pytest.raises(dossier_store.MissingDossiers, match="s2"):
        dossier_store.situations_for(enabled(settings), ("s1", "s2"))


def test_an_ungrounded_cell_gets_no_dossier_and_a_missing_one_exits_3(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dossier is evidence, so it follows factor C. Were it handed to a
    `parametric_only` cell, the grounding ablation would compare "evidence"
    with "a summary of the evidence"."""
    import typer

    from cascade import cli
    from cascade.decompose import dossier_store

    monkeypatch.setattr(dossier_store, "load_dossiers", explode)
    assert cli._situations(enabled(settings), ["s1"], role="sim", grounded=False) == {
        "s1": ("", None)
    }
    monkeypatch.setattr(dossier_store, "load_dossiers", lambda *_, **__: {})
    with pytest.raises(typer.Exit) as raised:
        cli._situations(enabled(settings), ["s1"], role="sim")
    assert raised.value.exit_code == cli.EXIT_PRECONDITION


def test_the_compiler_reads_the_dossier_and_records_which_one(settings: Settings) -> None:
    from cascade.decompose.compiler import Lathe
    from cascade.decompose.prompts import CRITIQUE_TOOL, DRAFT_TOOL
    from tests.conftest import fake_embed
    from tests.unit.test_decompose_compiler import ScriptedClient, ScriptedResult, graph_payload

    def compile_with(situations: dict[str, tuple[str, str | None]]) -> tuple[Any, Any]:
        client = ScriptedClient(
            [
                ScriptedResult(DRAFT_TOOL["name"], graph_payload()),
                ScriptedResult(CRITIQUE_TOOL["name"], {"defects": []}),
                ScriptedResult(DRAFT_TOOL["name"], graph_payload()),
            ]
        )
        compiler = Lathe(
            settings=settings,
            client=client,
            embed=fake_embed(),
            retrieve=lambda question, as_of, k: [],
            situations=situations,
        )
        return compiler.compile_scenario(scenario()), client.requests[0][1]

    outcome, draft = compile_with({"s1": ("- A hearing is scheduled.", "abc123")})
    assert "A hearing is scheduled." in draft.messages[0]["content"]
    assert outcome.ok and outcome.dossier_sha256 == "abc123"

    plain, plain_draft = compile_with({})
    assert "Situation report" not in plain_draft.messages[0]["content"]
    assert plain.ok and plain.dossier_sha256 is None


# ---------------------------------------------------------------------------
# Configuration and the migration
# ---------------------------------------------------------------------------


def test_the_dossier_ships_off_and_needs_its_own_prompt_revision() -> None:
    base = Settings()
    assert base.dossier.enabled is False
    assert "dossier" in base.budget.phase_ceiling_usd

    payload = base.model_dump()
    payload["dossier"]["enabled"] = True
    payload["llm"]["prompt_rev"] = "r2"  # before the dossier existed
    with pytest.raises(ValueError, match=r"requires llm\.prompt_rev >= r3"):
        Settings.model_validate(payload)

    for revision in ("r3", base.llm.prompt_rev):
        payload["llm"]["prompt_rev"] = revision
        assert Settings.model_validate(payload).dossier.enabled is True


def test_migration_019_records_the_revision_and_grants_read_only() -> None:
    sql = (REPO / "migrations/019_scenario_dossiers.sql").read_text()
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert body.count("'r3'") == 2 and "'compiler'" in body and "'agent'" in body
    grants = re.findall(r"GRANT\s+(.+?)\s+ON\s+(\S+)\s+TO\s+(.+?);", body, flags=re.S)
    assert grants == [("SELECT", "scenario_dossiers", "cascade_sim, cascade_eval")]
    assert "scenario_labels" not in body
