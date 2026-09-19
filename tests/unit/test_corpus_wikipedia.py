"""Wikipedia snapshots: the time lock, as-of selection and additive unit keys (M14).

Every exchange in ``tests/fixtures/wikipedia/`` was recorded from
en.wikipedia.org on 2026-09-19 by driving this adapter, at the registry's own
cutoffs; long revision text is cut after the lead and the cut is marked. A
cassette answers only the requests it holds -- anything else fails the test --
so what the adapter *asks for* is under test as much as what it does with the
answer.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from cascade.config import Settings
from cascade.corpus import pipeline
from cascade.corpus.fetch import Fetcher, RateLimited
from cascade.corpus.sources import wikipedia
from cascade.ledger.schema import Scenario

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "wikipedia"

# Parameters every request carries and no exchange is keyed by.
_CONSTANT = {"format", "formatversion", "maxlag"}


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# Registry cutoffs, verbatim.
NATANZ = ts("2025-05-11T03:53:19.001000Z")
TRUMP_TARIFF = ts("2025-03-04T15:37:48.653021Z")
JCPOA = ts("2018-01-12T00:00:00Z")
ANTHROPIC_EARLY = ts("2026-01-31T10:04:07.982085Z")
ANTHROPIC_LATE = ts("2026-02-19T08:38:19.117000Z")
ROCKETS = ts("2025-02-17T03:42:31.013500Z")
BANK_OF_RUSSIA = ts("2026-05-04T18:20:18.366628Z")
TESLA = ts("2025-11-25T07:33:03.625277Z")
META = ts("2025-10-06T10:10:43.984017Z")
GOLD = ts("2026-03-30T03:58:25.278761Z")
TWITTER: dict[str, Any] = {
    "question": "Will Elon Musk complete the acquisition of Twitter before the end of 2022?",
    "party_names": [
        "Delaware Court of Chancery",
        "Elon Musk",
        "Financing banks",
        "Twitter board",
        "Twitter shareholders",
    ],
    "cutoff": ts("2022-07-08T00:00:00Z"),
}


class Cassette:
    """Recorded exchanges, served by parameter match; every request is logged."""

    def __init__(self, *names: str) -> None:
        self.exchanges: list[dict[str, Any]] = []
        for name in names:
            recorded = json.loads((FIXTURES / f"{name}.json").read_text())
            self.exchanges.extend(recorded["exchanges"])
        self.requests: list[dict[str, str]] = []

    @staticmethod
    def key(params: dict[str, str]) -> dict[str, str]:
        return {key: value for key, value in params.items() if key not in _CONSTANT}

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        assert params.get("maxlag") == "5", "every request must carry maxlag"
        wanted = self.key(params)
        for exchange in self.exchanges:
            if self.key(exchange["params"]) == wanted:
                return httpx.Response(exchange["status"], json=exchange["body"])
        raise AssertionError(f"request not in cassette: {wanted}")

    def fetcher(self, handler: Callable[[httpx.Request], httpx.Response] | None = None) -> Fetcher:
        return Fetcher(
            requests_per_second=1000.0,
            classify_body=wikipedia.classify_body,
            client=httpx.Client(transport=httpx.MockTransport(handler or self.handler)),
        )

    def sent(self, key: str) -> list[str]:
        return [params[key] for params in self.requests if key in params]


# ---------------------------------------------------------------------------
# Strictly before the cutoff
# ---------------------------------------------------------------------------


def test_the_revision_is_strictly_before_the_cutoff() -> None:
    """A revision saved at the cutoff instant is not before it.

    Recorded: Anthropic has a revision saved at exactly 2026-02-17T17:10:13Z,
    and ``rvstart`` is inclusive -- asked for that instant, the API returns it.
    """
    cassette = Cassette("anthropic_at_the_cutoff")
    cutoff = datetime(2026, 2, 17, 17, 10, 13, tzinfo=UTC)
    revision = wikipedia.revision_before(cassette.fetcher(), title="Anthropic", as_of=cutoff)
    assert revision is not None
    assert revision.timestamp < cutoff
    assert revision.revision_id == 1338826479, "the revision *before* the one at the cutoff"
    assert cassette.sent("rvstart") == ["2026-02-17T17:10:12Z"], "anchored a second early"


def test_a_revision_at_the_cutoff_is_refused_even_when_the_server_sends_it() -> None:
    """The anchor is a request; the check is the guarantee."""
    cassette = Cassette("anthropic_at_the_cutoff")
    at_cutoff = next(
        exchange
        for exchange in cassette.exchanges
        if exchange["params"].get("rvstart") == "2026-02-17T17:10:13Z"
    )
    fetcher = cassette.fetcher(lambda request: httpx.Response(200, json=at_cutoff["body"]))
    cutoff = datetime(2026, 2, 17, 17, 10, 13, tzinfo=UTC)
    assert wikipedia.revision_before(fetcher, title="Anthropic", as_of=cutoff) is None


def test_a_naive_cutoff_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        wikipedia.revision_before(
            Cassette().fetcher(), title="Anthropic", as_of=datetime(2026, 2, 17)
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        wikipedia.SnapshotUnit(as_of=datetime(2026, 2, 17), titles=("Anthropic",))


# ---------------------------------------------------------------------------
# Pages that did not exist yet, and moves after the cutoff
# ---------------------------------------------------------------------------

WAR = ("Twelve-Day War", "June 2025 Israeli strikes on Iran", "Iran\u2013Israel war")


def test_a_page_created_after_the_cutoff_yields_nothing() -> None:
    """Not its first revision, not its current one: nothing.

    Recorded at the Natanz scenario's cutoff (2025-05-11): the war began a
    month later. "Twelve-Day War" was created 2025-06-14; the cassette holds
    that first revision too, so an adapter that fell back to it would get an
    answer rather than an unrecorded request.
    """
    cassette = Cassette("created_after_the_cutoff")
    fetcher = cassette.fetcher()
    assert wikipedia.resolve(fetcher, title="Twelve-Day War", as_of=NATANZ) is None
    assert "newer" not in cassette.sent("rvdir"), "never asked for the first revision"


def test_titles_moved_after_the_cutoff_cannot_bring_post_cutoff_content_in() -> None:
    """Today "June 2025 Israeli strikes on Iran" and "Iran-Israel war" redirect to
    articles about the war; the move log names the pages that left them after
    the cutoff. Found by identity, those pages still have no revision before
    the cutoff -- so a move is a way to find old text, never to fetch new text.
    """
    cassette = Cassette("created_after_the_cutoff")
    unit = wikipedia.SnapshotUnit(as_of=NATANZ, titles=WAR)
    assert list(wikipedia.load_snapshots(cassette.fetcher(), unit)) == []
    assert sorted(cassette.sent("letitle")) == sorted(WAR), "each title's moves were checked"
    assert len(cassette.sent("pageids")) == 3, "and each moved page was asked for as of the cutoff"
    assert not cassette.sent("redirects"), "server-side redirect resolution would be today's"


def test_a_title_means_what_it_meant_at_the_cutoff_not_what_it_points_to_today() -> None:
    """Recorded at the tariff scenario's cutoff (2025-03-04T15:37Z).

    That day "Trump" was a disambiguation page declaring Donald Trump and
    Trump (card games) its primary topics. It was moved away in May 2025; the
    page filed under "Trump" today held only a self-redirect at the cutoff, so
    the move log finds the page that was there. Its declaration is read as of
    then, the scenario's own words ("United States") pick Donald Trump over the
    card game, and his article is taken as it read 55 minutes before the cutoff.
    The server is never asked to resolve today's redirect.
    """
    cassette = Cassette("trump_redirect_after_the_cutoff")
    context = wikipedia.stems(
        "Will Trump impose a blanket tariff of 20-30% on the EU by June 30? "
        "Any European Union European Unions Only PM ET Trump United States"
    )
    unit = wikipedia.SnapshotUnit(as_of=TRUMP_TARIFF, titles=("Trump",), context=context)
    [document] = list(wikipedia.load_snapshots(cassette.fetcher(), unit))
    assert document.title == "Donald Trump"
    assert document.published_at == ts("2025-03-04T14:42:14Z") < TRUMP_TARIFF
    assert cassette.sent("letitle") == ["Trump"], "the self-redirect sent it to the move log"
    assert "Trump (card games)" not in cassette.sent("titles"), "the confirmed entry comes first"
    assert not cassette.sent("redirects")


TESLA_QUESTION = "Will Tesla deliver between 450000 and 475000 vehicles in Q4 2025"


def test_a_declared_primary_topic_is_chosen_by_the_scenarios_own_words() -> None:
    """Recorded at its cutoff (2025-11-25): "Tesla" was a disambiguation page
    declaring Nikola Tesla, Tesla, Inc. and the unit primary; the question's
    "vehicles" meets Tesla, Inc.'s entry line ("electric vehicle")."""
    cassette = Cassette("tesla_deliveries_depth_1")
    [tesla] = [
        exchange
        for exchange in cassette.exchanges
        if exchange["params"].get("titles") == "Tesla"
        and exchange["params"].get("prop") == "revisions"
    ]
    page = tesla["body"]["query"]["pages"][0]["revisions"][0]["slots"]["main"]["content"]
    entries = wikipedia.primary_topics(page)
    assert [title for title, _ in entries] == ["Nikola Tesla", "Tesla, Inc.", "Tesla (unit)"]
    context = wikipedia.stems(f"{TESLA_QUESTION} PM ET Tesla")
    ranked = wikipedia.rank_primary(entries, context=context, title="Tesla")
    assert ranked[0] == ("Tesla, Inc.", True)
    assert [confirmed for _, confirmed in ranked[1:]] == [False, False]

    unit = wikipedia.plan_unit(
        question=TESLA_QUESTION, party_names=["PM ET", "Tesla"], cutoff=TESLA, depth=1
    )
    titles = [document.title for document in wikipedia.load_snapshots(cassette.fetcher(), unit)]
    assert "Tesla, Inc." in titles
    # Tesla, Inc.'s own lead links him (the hop reads it), and the gate drops
    # him: nothing in his article is about deliveries.
    assert "Nikola Tesla" not in titles


def test_a_primary_topic_nothing_confirms_is_not_taken() -> None:
    """Recorded: "Will Meta have the second best AI model on October 31?". "Meta"
    declared Meta (prefix) and Meta Platforms; neither entry line nor either
    as-of lead shares a word with the question or its parties. Taking the
    editors' first entry would file the prefix under an AI-model question."""
    cassette = Cassette("meta_second_best_depth_1")
    unit = wikipedia.plan_unit(
        question="Will Meta have the second best AI model on October 31?",
        party_names=["Arena Score", "Google", "Leaderboard", "Meta", "PM ET", "Results"],
        cutoff=META,
        depth=1,
    )
    titles = [document.title for document in wikipedia.load_snapshots(cassette.fetcher(), unit)]
    assert titles == ["Google"]
    sent = cassette.sent("titles")
    assert "Meta (prefix)" in sent and "Meta Platforms" in sent, "both were read and declined"


def test_a_menu_declares_no_primary_topic() -> None:
    menu = "'''Solana''' may refer to:\n* [[La Solana]], a municipality\n{{disambiguation}}\n"
    assert wikipedia.primary_topics(menu) == []
    inline = "'''X''' most commonly refers to [[Y (thing)]], a thing.\n* [[Z]]\n"
    assert [title for title, _ in wikipedia.primary_topics(inline)] == ["Y (thing)"]


def test_a_name_too_short_to_count_does_not_veto_the_article_it_names() -> None:
    """Recorded: "Will Gold (GC) settle at $3,800-$4,200 in June?". "Gold" is gated
    -- a capitalised word the registry does not list -- and the only other
    name is "GC", which no whole-word count can use. It must not veto Gold."""
    cassette = Cassette("gold_settle_depth_1")
    unit = wikipedia.plan_unit(
        question="Will Gold (GC) settle at $3,800-$4,200 in June?",
        party_names=[
            "Active Month",
            "CME",
            "Days",
            "First Position Date",
            "Intraday",
            "Only",
            "Settlement",
        ],
        cutoff=GOLD,
        depth=1,
    )
    assert "Gold" in unit.gated
    titles = [document.title for document in wikipedia.load_snapshots(cassette.fetcher(), unit)]
    assert titles == ["Gold"]


def test_a_page_moved_after_the_cutoff_is_found_by_identity_and_filed_under_its_old_title() -> None:
    """Recorded: on 2018-01-12 the article was at "Joint Comprehensive Plan of
    Action"; in 2026 it was moved to "Iran nuclear deal" and its old title
    given to a former redirect. Asked by title, the API returns that page's
    2015 self-redirect. The move log locates the article; the revision is
    strictly before the cutoff and the document never carries its new name.
    """
    cassette = Cassette("jcpoa_moved_after_the_cutoff")
    unit = wikipedia.SnapshotUnit(as_of=JCPOA, titles=("JCPOA",))
    [document] = list(wikipedia.load_snapshots(cassette.fetcher(), unit))
    assert document.title == "Joint Comprehensive Plan of Action"
    assert document.published_at == ts("2018-01-06T11:51:43Z") < JCPOA
    # Its 2026 title stays out of the document's own name and address; the
    # 2018 text is entitled to mention the alias, and does.
    assert "Iran nuclear deal" not in document.title + document.url
    assert "Vienna" in document.body
    assert cassette.sent("lestart") == ["2018-01-12T00:00:00Z"], "moves from the cutoff on"


def test_a_self_redirect_that_was_never_moved_yields_nothing() -> None:
    """The walk terminates on a title whose as-of text points to itself."""
    cassette = Cassette("jcpoa_moved_after_the_cutoff")

    def no_moves(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("list") == "logevents":
            return httpx.Response(200, json={"batchcomplete": True, "query": {"logevents": []}})
        return cassette.handler(request)

    found = wikipedia.resolve(
        cassette.fetcher(no_moves), title="Joint Comprehensive Plan of Action", as_of=JCPOA
    )
    assert found is None


# ---------------------------------------------------------------------------
# One article, several cutoffs
# ---------------------------------------------------------------------------


def test_two_scenarios_sharing_an_article_each_get_a_revision_before_their_own_cutoff() -> None:
    """Recorded: Anthropic as of the two registry cutoffs that name it.

    Each snapshot is its own document with its own ``published_at``, so the
    ``published_at < as_of`` filter Chronofence applies hands the earlier
    scenario only the earlier revision. Storing one document per article --
    the latest -- would either starve the earlier scenario or, with a lazier
    filter, leak the later text into it.
    """
    cassette = Cassette("anthropic_two_cutoffs")
    fetcher = cassette.fetcher()
    [first] = list(
        wikipedia.load_snapshots(
            fetcher, wikipedia.SnapshotUnit(as_of=ANTHROPIC_EARLY, titles=("Anthropic",))
        )
    )
    [second] = list(
        wikipedia.load_snapshots(
            fetcher, wikipedia.SnapshotUnit(as_of=ANTHROPIC_LATE, titles=("Anthropic",))
        )
    )
    assert first.published_at < ANTHROPIC_EARLY
    assert second.published_at < ANTHROPIC_LATE
    assert first.source_ref != second.source_ref, "two revisions, two documents"
    assert second.published_at >= ANTHROPIC_EARLY, "the later snapshot postdates the early cutoff"
    admissible_for_early = [d for d in (first, second) if d.published_at < ANTHROPIC_EARLY]
    assert admissible_for_early == [first], "so the time lock serves the early scenario its own"


# ---------------------------------------------------------------------------
# The body
# ---------------------------------------------------------------------------


def test_the_body_is_the_revisions_own_wikitext_and_nothing_is_rendered() -> None:
    """Recorded: the 2024-25 Houston Rockets season as of 2025-02-14, three days
    before the #1-seed scenario's cutoff, when the team stood at 34-21. Its
    render (in the cassette, via ``action=parse``) expands today's standings
    template: "Updated: August 26, 2026" and the final 52-30.
    """
    cassette = Cassette("rockets_season_as_of_2025-02-17")
    unit = wikipedia.SnapshotUnit(as_of=ROCKETS, titles=("2024\u201325 Houston Rockets season",))
    [document] = list(wikipedia.load_snapshots(cassette.fetcher(), unit))
    assert "wins: 34" in document.body and "losses: 21" in document.body
    assert "Houston Rockets | 52 | 30" not in document.body
    assert "2026" not in document.body.split("Game log")[0]
    assert "parse" not in cassette.sent("action"), "an old revision rendered today is today's"
    assert document.source_ref == "rev:1275706002"
    assert document.url.endswith("oldid=1275706002")


def test_wikitext_conversion_drops_transclusions_and_keeps_the_articles_own_words() -> None:
    text = wikipedia.wikitext_to_text(
        "{{Short description|A team}}\n"
        "{{Infobox team|name=Example FC|manager=[[Jane Doe]]|image=x.png|wins=3}}\n"
        "'''Example FC''' is a [[Football|football]] club<ref>cite</ref> in [[Town]].\n"
        "== Standings ==\n{{2025 standings}}\n"
        "== History ==\n"
        "Founded in {{convert|1900|AD}}.<!-- note -->\n"
        "== References ==\n{{reflist}}\n"
    )
    assert text.splitlines() == [
        "name: Example FC",
        "manager: Jane Doe",
        "wins: 3",
        "Example FC is a football club in Town.",
        "History",
        "Founded in 1900 AD.",
    ]


def test_redirects_and_disambiguations_are_read_from_wikitext() -> None:
    assert (
        wikipedia.redirect_target("#REDIRECT [[Donald Trump]]\n{{R from move}}") == "Donald Trump"
    )
    assert wikipedia.redirect_target("#redirect [[foo_bar#Section]]") == "Foo bar"
    assert wikipedia.redirect_target("'''Trump''' may refer to:") is None
    assert wikipedia.is_disambiguation(
        "'''Any''' may refer to:\n* [[Any (song)]]\n{{disambiguation}}"
    )
    assert not wikipedia.is_disambiguation("'''Anthropic''' is a company.")


def test_lead_links_come_from_the_lead_prose_only() -> None:
    wikitext = (
        "{{Infobox x|country=[[France]]}}\n{{Hatnote|see [[Other thing]]}}\n"
        "'''X''' is in [[Paris]], the capital of [[France|the country]].<ref>[[Cited]]</ref>\n"
        "[[File:Photo.jpg|thumb|[[Caption link]]]]\n"
        "== Section ==\nLater [[Section link]].\n"
    )
    assert wikipedia.lead_links(wikitext) == ["Paris", "France"]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selection_reads_the_question_the_party_names_and_the_cutoff_only() -> None:
    selection = wikipedia.select(
        question="Will the Denver Broncos win the AFC West?",
        party_names=["AFC West", "Denver Broncos", "NFL", "PM ET"],
        cutoff=ts("2025-08-29T22:29:52.731500Z"),
    )
    firsts = [slot[0] for slot in selection.slots]
    assert firsts == ["Denver Broncos", "AFC West", "NFL"], "PM ET is nobody"
    assert "2025 Denver Broncos season" in selection.slots[0]
    assert "2025\u201326 Denver Broncos season" in selection.slots[0]
    assert selection.names == ("Denver Broncos", "AFC West")
    assert "NFL" in selection.gated, "a party the question does not name must earn its place"


def test_selection_uses_party_names() -> None:
    """The registry knows the Delaware Court of Chancery matters to the Twitter
    deal; the question does not say so."""
    selection = wikipedia.select(**TWITTER)
    assert ("Delaware Court of Chancery",) in selection.slots
    assert ("Twitter board",) in selection.slots
    without = wikipedia.select(**{**TWITTER, "party_names": []})
    assert len(selection.slots) == len(without.slots) + 4
    assert ("Delaware Court of Chancery",) not in without.slots


def test_selection_finds_the_questions_dated_event_and_breaks_names_at_punctuation() -> None:
    selection = wikipedia.select(
        question="Will Erin Doherty (Adolescence) win at the 2026 Slovenian parliamentary election?",
        party_names=[],
        cutoff=ts("2026-02-10T00:00:00Z"),
    )
    firsts = [slot[0] for slot in selection.slots]
    assert firsts[:4] == [
        "2026 Slovenian parliamentary election",
        "2026 Slovenian parliamentary",
        "2026 Slovenian",
        "Erin Doherty",
    ]
    assert "Erin Doherty Adolescence" not in firsts
    assert "Adolescence" in firsts


def test_placeholder_names_and_possessives() -> None:
    selection = wikipedia.select(
        question="Will Party I win the most seats? Will Taylor Swift's album top the chart?",
        party_names=["Company A", "Artist B"],
        cutoff=ts("2026-01-01T00:00:00Z"),
    )
    firsts = [slot[0] for slot in selection.slots]
    for placeholder in ("Party I", "Company A", "Artist B"):
        assert placeholder not in firsts
    assert selection.slots[0][:2] == ("Taylor Swift's", "Taylor Swift")


def test_selection_is_a_function_of_the_scenario_alone() -> None:
    """No clock, no network, no outcome: the same scenario selects the same titles."""
    assert wikipedia.select(**TWITTER) == wikipedia.select(**TWITTER)


def test_mentions_counts_whole_words_only() -> None:
    names = ["Denver Broncos", "AFC West"]
    assert wikipedia.mentions("The Denver Broncos play in the AFC West.", names) == (2, 0)
    assert wikipedia.mentions("Searched high and low.", ["Search"]) == (0, 0)
    assert wikipedia.mentions("LCK is a league; Freecs play in it.", ["LCK", "Freecs", "EU"]) == (
        0,
        2,
    )


def test_hop_titles_prefer_links_shared_by_several_leads() -> None:
    ordered = wikipedia.hop_titles([["A", "B", "C"], ["D", "B"], ["C", "E"]], exclude=["E"])
    assert ordered == ["B", "C", "A", "D"]


def test_a_unit_fetches_named_parties_and_gates_the_rest_on_the_as_of_lead() -> None:
    """Recorded: the Twitter-acquisition scenario at depth 1.

    The Court of Chancery is a registered party the question never names, and
    it is fetched. The article on the deal itself is reached through the
    Elon Musk article's as-of lead. Links from that lead that name nothing the
    question names -- "Business magnate", "Investor" -- are read and dropped.
    """
    cassette = Cassette("twitter_acquisition_depth_1")
    unit = wikipedia.plan_unit(**TWITTER, depth=1)
    documents = list(wikipedia.load_snapshots(cassette.fetcher(), unit))
    titles = [document.title for document in documents]
    for wanted in ("Delaware Court of Chancery", "Elon Musk", "Twitter"):
        assert wanted in titles
    assert "Acquisition of Twitter by Elon Musk" in titles
    for generic in ("Business magnate", "Investor", "Microblogging"):
        assert generic in cassette.sent("titles"), "the link was followed"
        assert generic not in titles, "and dropped, because it is not about the question"
    for document in documents:
        assert document.published_at < TWITTER["cutoff"]
    stamps = [document.published_at for document in documents]
    assert stamps == sorted(stamps), "earliest first, so near-duplicate collapse keeps the earliest"


def test_a_capitalised_word_the_question_does_not_vouch_for_is_dropped() -> None:
    """Recorded: "...after the June Meeting?" (cutoff 2026-05-04). "Meeting" is a
    real article and a capitalised word in the question; its as-of lead names
    nothing else the question does, so it is neither kept nor used as a hop
    source. "Bank of Russia" is followed, as of the cutoff, to its article.
    """
    cassette = Cassette("bank_of_russia_depth_1")
    unit = wikipedia.plan_unit(
        question="Will the Bank of Russia make no change to the key rate after the June Meeting?",
        party_names=["Russia"],
        cutoff=BANK_OF_RUSSIA,
        depth=1,
    )
    assert "Meeting" in unit.gated
    documents = list(wikipedia.load_snapshots(cassette.fetcher(), unit))
    assert [document.title for document in documents] == ["Central Bank of Russia"]
    assert "Meeting" in cassette.sent("titles"), "read, then dropped"


class _Pages:
    """A synthetic wiki for testing windowing logic, not API behaviour."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if params.get("list") == "logevents":
            return httpx.Response(200, json={"query": {"logevents": []}})
        titles = params["titles"].split("|")
        if params.get("prop") == "info":
            return httpx.Response(200, json={"query": {"pages": [{"title": t} for t in titles]}})
        self.asked.append(params["titles"])
        revision = {
            "revid": 1000 + sorted(self.pages).index(params["titles"]),
            "timestamp": "2025-01-01T00:00:00Z",
            "slots": {"main": {"content": self.pages[params["titles"]]}},
        }
        return httpx.Response(200, json={"query": {"pages": [{"revisions": [revision]}]}})


def test_gates_read_the_leads_prose_not_the_infobox_before_it() -> None:
    """A large article's first few thousand characters are infobox fields. A
    gate that read "the start of the text" would never reach the sentence
    saying what the article is about, and would refuse it."""
    infobox = "{{Infobox thing|" + "|".join(f"field{n}=value {n}" for n in range(300)) + "}}"
    prose = "'''Subject''' is closely tied to Other Name.\n== History ==\n"
    unit = wikipedia.SnapshotUnit(
        as_of=ts("2025-06-01T00:00:00Z"),
        titles=("Subject",),
        names=("Subject", "Other Name"),
        gated=frozenset({"Subject"}),
    )
    wiki = _Pages({"Subject": f"{infobox}\n{prose}"})
    fetcher = Fetcher(
        requests_per_second=1000.0,
        client=httpx.Client(transport=httpx.MockTransport(wiki.handler)),
    )
    [document] = list(wikipedia.load_snapshots(fetcher, unit))
    assert len(document.body.split("closely tied")[0]) > 2500, "the infobox fills the window"


def test_hop_windows_are_the_same_list_at_every_depth() -> None:
    """Depth 1 and depth 2 window into one list of links, even when a link is
    also one of depth 1's own titles. Excluding a unit's own titles would make
    the list depend on the depth asking, and the windows would overlap."""
    about = "Source and Party are both named here."
    pages = {
        "Source": "'''Source''' links [[A]], [[Party]], [[B]] and [[C]].\n== Body ==\n",
        "Party": about,
        "A": about,
        "B": about,
        "C": about,
    }
    as_of = ts("2025-06-01T00:00:00Z")
    common: dict[str, Any] = {"as_of": as_of, "hop_sources": ("Source",), "names": ("Source",)}
    first = wikipedia.SnapshotUnit(titles=("Source", "Party"), hop_skip=0, hop_take=2, **common)
    second = wikipedia.SnapshotUnit(titles=(), hop_skip=2, hop_take=12, **common)
    followed = []
    for unit in (first, second):
        wiki = _Pages(pages)
        fetcher = Fetcher(
            requests_per_second=1000.0,
            client=httpx.Client(transport=httpx.MockTransport(wiki.handler)),
        )
        list(wikipedia.load_snapshots(fetcher, unit))
        followed.append([title for title in wiki.asked if title not in {"Source", *unit.titles}])
    assert followed == [["A"], ["B", "C"]]


# ---------------------------------------------------------------------------
# Unit keys and depth
# ---------------------------------------------------------------------------


def test_unit_keys_carry_depth_and_cutoff_and_legacy_keys_are_recognised() -> None:
    key = wikipedia.unit_key("polymarket:afc-west-winner-1:540281", TWITTER["cutoff"], 2)
    assert key == "d02.20220708.polymarket:afc-west-winner-1:540281"
    assert wikipedia.split_unit(key) == ("polymarket:afc-west-winner-1:540281", 2)
    assert wikipedia.split_unit("polymarket:afc-west-winner-1:540281") == (
        "polymarket:afc-west-winner-1:540281",
        wikipedia.LEGACY_DEPTH,
    )
    with pytest.raises(ValueError):
        wikipedia.unit_key("x", TWITTER["cutoff"], 0)
    with pytest.raises(ValueError):
        wikipedia.plan_unit(**TWITTER, depth=0)


def test_depth_windows_are_disjoint_and_together_cover_the_stream() -> None:
    question = "Will the Denver Broncos win the AFC West?"
    parties = ["AFC West", "Denver Broncos", "NFL"]
    cutoff = ts("2025-08-29T22:29:52.731500Z")
    first = wikipedia.plan_unit(question=question, party_names=parties, cutoff=cutoff, depth=1)
    second = wikipedia.plan_unit(question=question, party_names=parties, cutoff=cutoff, depth=2)
    assert first.titles and not second.titles, "three slots fit in the first window"
    assert first.hop_take == wikipedia.PASS_TITLES - 3 and first.hop_skip == 0
    assert second.hop_skip == first.hop_take and second.hop_take == wikipedia.PASS_TITLES
    assert first.hop_sources == second.hop_sources
    assert first.gated == second.gated


def test_unit_keys_sort_depth_major_then_earliest_cutoff() -> None:
    """The queue orders Wikipedia units by key alone (they carry no month)."""
    early, late = ts("2019-01-05T00:00:00Z"), ts("2026-03-01T00:00:00Z")
    keys = sorted(
        [
            wikipedia.unit_key("b", late, 2),
            wikipedia.unit_key("a", late, 1),
            wikipedia.unit_key("z", early, 1),
            wikipedia.unit_key("a", late, 2),
        ]
    )
    assert keys == ["d01.20190105.z", "d01.20260301.a", "d02.20260301.a", "d02.20260301.b"]


def _scenario(scenario_id: str, question: str, parties: list[str], cutoff: datetime) -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        question=question,
        resolution_criterion="",
        cutoff_ts=cutoff,
        resolve_ts=cutoff + timedelta(days=90),
        domain="corporate",
        source="curated",
        source_ref=scenario_id,
        party_rule="curated",
        party_names=tuple(parties),
        event_group=None,
    )


TWITTER_SCENARIO = _scenario(
    "curated:twitter-musk-acquisition-2022",
    TWITTER["question"],
    TWITTER["party_names"],
    TWITTER["cutoff"],
)
BRONCOS_SCENARIO = _scenario(
    "polymarket:afc-west-winner-1:540281",
    "Will the Denver Broncos win the AFC West?",
    ["AFC West", "Denver Broncos", "NFL"],
    ts("2025-08-29T22:29:52.731500Z"),
)


def with_corpus(settings: Settings, **update: Any) -> Settings:
    return settings.model_copy(update={"corpus": settings.corpus.model_copy(update=update)})


def test_a_deeper_pass_is_additive_and_legacy_done_keys_are_honoured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cascade.ledger.store as ledger_store

    registry = (TWITTER_SCENARIO, BRONCOS_SCENARIO)
    monkeypatch.setattr(ledger_store, "load_scenarios", lambda settings, role: registry)
    shallow = pipeline._units_for(with_corpus(settings, wikipedia_depth=1), "wikipedia")
    deep = pipeline._units_for(with_corpus(settings, wikipedia_depth=2), "wikipedia")
    assert shallow == [
        "d01.20220708.curated:twitter-musk-acquisition-2022",
        "d01.20250829.polymarket:afc-west-winner-1:540281",
    ]
    assert set(shallow) < set(deep), "raising the depth adds keys and keeps every old one"
    assert all(wikipedia.split_unit(key)[1] == 2 for key in set(deep) - set(shallow))
    # The previous adapter's rows are bare scenario ids. No planned key equals
    # one, so they are neither re-run nor in the way.
    assert not {scenario.scenario_id for scenario in registry} & set(deep)


def test_run_ingest_fetches_only_the_pending_depth(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real loop, the store and the network replaced: the
    legacy row and depth 1 are `done`, so depth 2 alone is fetched and marked."""
    import cascade.ledger.store as ledger_store

    cassette = Cassette("twitter_acquisition_depth_2")
    scenario = TWITTER_SCENARIO
    first = wikipedia.unit_key(scenario.scenario_id, scenario.cutoff_ts, 1)
    second = wikipedia.unit_key(scenario.scenario_id, scenario.cutoff_ts, 2)
    marked: list[dict[str, Any]] = []
    written: list[str] = []
    monkeypatch.setattr(ledger_store, "load_scenarios", lambda settings, role: (scenario,))
    monkeypatch.setattr(pipeline, "seen_simhashes", lambda settings: [])
    monkeypatch.setattr(pipeline, "existing_document_ids", lambda settings: set())
    monkeypatch.setattr(
        pipeline, "completed_units", lambda settings, source: {scenario.scenario_id, first}
    )
    monkeypatch.setattr(pipeline, "stored_chunk_count", lambda settings: 0)
    monkeypatch.setattr(pipeline, "_scenario_cutoffs", lambda settings: [])
    monkeypatch.setattr(pipeline, "mark_unit", lambda settings, **kw: marked.append(kw))

    def write(settings: Any, batch: Any, chunks: Any, vectors: Any) -> tuple[int, int]:
        written.extend(document.document_id for document in batch)
        return len(batch), len(chunks)

    monkeypatch.setattr(pipeline, "write_batch", write)
    real_fetcher = Fetcher
    built: list[dict[str, Any]] = []

    def fetcher(**kwargs: Any) -> Fetcher:
        built.append(dict(kwargs))
        kwargs["requests_per_second"] = 1000.0
        return real_fetcher(
            client=httpx.Client(transport=httpx.MockTransport(cassette.handler)), **kwargs
        )

    monkeypatch.setattr(pipeline, "Fetcher", fetcher)

    class Embedder:
        def load(self) -> None:
            return None

        def count_tokens(self, text: str) -> int:
            return len(text.split())

        def count_tokens_batch(self, texts: Any) -> list[int]:
            return [len(text.split()) for text in texts]

        def encode(self, texts: Any) -> list[list[float]]:
            return [[0.0] * 384 for _ in texts]

    report = pipeline.run_ingest(
        with_corpus(settings, wikipedia_depth=2),
        now=ts("2026-09-19T00:00:00Z"),
        sources=("wikipedia",),
        embedder=Embedder(),  # type: ignore[arg-type]
        free_disk_gb=lambda: 500.0,
    )
    [source] = report.sources
    assert (source.units_skipped, source.units_done, source.units_failed) == (1, 1, 0)
    assert [entry["unit_key"] for entry in marked] == [second]
    assert marked[0]["state"] == "done"
    assert "wikipedia:rev:1096490688" in written, "Twitter, Inc. as of 2022-07-04"
    # The Wikipedia fetcher reads maxlag refusals as refusals, at its own pace.
    [wiki] = [kw for kw in built if kw.get("classify_body") is wikipedia.classify_body]
    assert wiki["requests_per_second"] == settings.corpus.wikipedia_requests_per_second <= 2.0


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------


def _maxlag() -> dict[str, Any]:
    recorded: dict[str, Any] = json.loads((FIXTURES / "maxlag.json").read_text())
    return recorded


def test_maxlag_is_a_throttle_that_backs_off_by_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recorded: a lagged replica answers HTTP 200 with ``error.code`` "maxlag"
    and ``Retry-After: 5``. Read as an answer it says "no such page", and the
    unit would be marked done with nothing in it."""
    from cascade.corpus import fetch

    recorded = _maxlag()
    answer = Cassette("anthropic_at_the_cutoff")
    slept: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", slept.append)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            return httpx.Response(
                recorded["status"], json=recorded["body"], headers=recorded["headers"]
            )
        return answer.handler(request)

    fetcher = answer.fetcher(handler)
    cutoff = datetime(2026, 2, 17, 17, 10, 13, tzinfo=UTC)
    found = wikipedia.revision_before(fetcher, title="Anthropic", as_of=cutoff)
    assert found is not None and found.revision_id == 1338826479
    assert fetcher.throttled == 2
    # The rate limiter's own sub-millisecond pacing waits land here too.
    assert [wait for wait in slept if wait >= 1.0] == [5.0, 5.0], "waited Retry-After, twice"


def test_a_throttle_that_never_lifts_fails_the_unit_rather_than_emptying_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existence check succeeds; every as-of lookup after it is refused.

    The unit must fail -- the pipeline then records it `failed` and retries it.
    An adapter that skipped the refused article would hand back an empty unit,
    which is recorded `done` and never visited again.
    """
    from cascade.corpus import fetch

    recorded = _maxlag()
    monkeypatch.setattr(fetch.time, "sleep", lambda seconds: None)
    cassette = Cassette("anthropic_two_cutoffs")

    def lagging(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("prop") == "info":
            return cassette.handler(request)
        return httpx.Response(
            recorded["status"], json=recorded["body"], headers=recorded["headers"]
        )

    unit = wikipedia.SnapshotUnit(as_of=ANTHROPIC_LATE, titles=("Anthropic",))
    with pytest.raises(RateLimited):
        list(wikipedia.load_snapshots(cassette.fetcher(lagging), unit))
    assert cassette.sent("prop") == ["info"], "refused after the existence check, not at it"


def test_classify_body_verdicts() -> None:
    def response(payload: Any) -> httpx.Response:
        return httpx.Response(200, json=payload)

    assert wikipedia.classify_body(response({"query": {"pages": []}})) is None
    assert wikipedia.classify_body(response(_maxlag()["body"])) == "throttled"
    assert wikipedia.classify_body(response({"error": {"code": "ratelimited"}})) == "throttled"
    assert wikipedia.classify_body(response({"error": {"code": "badvalue"}})) == "rejected"
    assert wikipedia.classify_body(httpx.Response(200, text="<html>")) == "unusable"


def test_a_429_is_retried_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    from cascade.corpus import fetch

    monkeypatch.setattr(fetch.time, "sleep", lambda seconds: None)
    answer = Cassette("anthropic_at_the_cutoff")
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return answer.handler(request)

    fetcher = answer.fetcher(handler)
    cutoff = datetime(2026, 2, 17, 17, 10, 13, tzinfo=UTC)
    found = wikipedia.revision_before(fetcher, title="Anthropic", as_of=cutoff)
    assert found is not None and fetcher.throttled == 1
