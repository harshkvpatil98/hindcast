"""Entity terms for the hybrid keyword pool (M14).

The extractor fails closed, and these tests pin the direction of each failure.
A missed name costs little -- the vector pool still runs. A *manufactured* one
("keep", "company", "april") is a flood term that fills the keyword pool with
chunks chosen for no reason, and then earns them fusion credit.
"""

from __future__ import annotations

import re

import pytest

from cascade.retrieval.keywords import MAX_TERM_WORDS, entity_terms
from cascade.retrieval.queries import agent_evidence_query

SAFE_TERM = re.compile(r"^[a-z0-9]+(?: [a-z0-9]+)*$")


def terms(text: str, *entities: str, max_terms: int = 8) -> tuple[str, ...]:
    return entity_terms(text, entities=entities, max_terms=max_terms)


class TestWhatIsExtracted:
    def test_multi_word_names_stay_together(self) -> None:
        """One AND-term per name: a chunk must say both words to match."""
        assert terms("Will the Baltimore Ravens win the AFC North?") == (
            "baltimore ravens",
            "afc north",
        )

    def test_connectors_bind_a_name_without_joining_the_term(self) -> None:
        assert terms("Will the Bank of England cut rates near the Strait of Hormuz?") == (
            "bank england",
            "strait hormuz",
        )

    def test_and_separates_two_parties(self) -> None:
        """'Russia and Ukraine' is two parties. One term requiring both would
        miss every chunk about either -- ADR-0009's phantom-party defect, again."""
        assert terms("Will Russia and Ukraine sign a ceasefire?") == ("russia", "ukraine")

    def test_a_comma_separates_names(self) -> None:
        assert terms("Will Pfizer, Moderna or AstraZeneca win approval?") == (
            "pfizer",
            "moderna",
            "astrazeneca",
        )

    def test_a_possessive_ends_the_name_it_is_attached_to(self) -> None:
        curly = "Will Israel strike Iran" + chr(0x2019) + "s Natanz facility?"
        assert terms(curly) == ("israel", "iran", "natanz")
        assert terms("Will Israel strike Iran's Natanz facility?") == ("israel", "iran", "natanz")

    def test_acronyms_and_mixed_case_names_are_names_anywhere(self) -> None:
        assert terms("NATO will respond. OpenAI ships. G7 meets.") == ("nato", "openai", "g7")


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "text",
        [
            "Will it happen before 1 April 2019?",
            "Does this resolve by Friday?",
            "Is there any chance?",
            "Will January 2026 be the hottest on record?",
        ],
    )
    def test_scaffolding_and_calendar_words_are_not_names(self, text: str) -> None:
        assert terms(text) == ()

    def test_a_sentence_initial_capital_proves_nothing(self) -> None:
        """'Keep' is capitalised because English capitalises sentences. As a term
        it would match a large share of the corpus."""
        assert terms("Keep borrowing costs low. Protect the pension scheme.") == ()

    def test_a_sentence_initial_verb_does_not_ride_in_on_a_real_name(self) -> None:
        assert terms("Keep Iran from closing the Strait of Hormuz.") == ("iran", "strait hormuz")

    def test_a_name_that_opens_a_sentence_is_kept_when_the_text_proves_it(self) -> None:
        """Proved by appearing capitalised mid-sentence, or by the actor lexicon."""
        assert terms("Gyokeres may sign. Will Arsenal sign Gyokeres?") == ("gyokeres", "arsenal")
        assert terms("Iran Strike on Israel by February 28?") == ("iran strike", "israel")

    def test_numbers_and_single_letters_are_dropped(self) -> None:
        assert terms("Will the 2026 FIFA World Cup expand?") == ("fifa world cup",)

    def test_registry_placeholders_are_dropped(self) -> None:
        """Sibling markets are stored as 'Company A', 'Team B'. With the letter
        gone the noun names every company there is."""
        assert terms("Will Company A be the largest company in the world?") == ()
        assert terms("Will Team B make the first pick of the NFL Draft?") == ("nfl draft",)

    def test_a_name_that_splits_into_a_letter_and_a_common_word_is_given_up(self) -> None:
        """'T-Mobile' -> 'mobile' is a different and far commoner word."""
        assert terms("Will AT&T, T-Mobile or Verizon raise prices?") == ("verizon",)

    def test_empty_text_yields_no_terms(self) -> None:
        assert terms("") == ()
        assert terms("   \n  ") == ()


class TestExplicitEntities:
    def test_an_entity_is_taken_whole_and_comes_first(self) -> None:
        """'and' stays inside an explicit name -- it is part of this regulator's
        name, and the text-search parser drops it as a stop word anyway."""
        assert terms("Will Microsoft close the deal?", "Competition and Markets Authority") == (
            "competition and markets authority",
            "microsoft",
        )

    def test_an_entity_is_not_subject_to_the_sentence_initial_rule(self) -> None:
        """Somebody already decided this string names a party."""
        assert terms("", "Labour Party") == ("labour party",)
        assert terms("", "Keep") == ("keep",)

    def test_entities_survive_truncation_before_mined_terms(self) -> None:
        text = "Will Pfizer, Moderna, Merck, Intel, Oracle, Nvidia, Tesla or Boeing act?"
        found = terms(text, "UK Government", max_terms=3)
        assert found == ("uk government", "pfizer", "moderna")

    def test_duplicates_are_asked_for_once(self) -> None:
        assert terms("Will Anthropic ship? Anthropic says yes.", "Anthropic") == ("anthropic",)

    def test_a_long_name_is_truncated_not_dropped(self) -> None:
        name = "The Very Long Interministerial Committee For Regional Broadband Licensing Review"
        (term,) = terms("", name)
        assert len(term.split()) == MAX_TERM_WORDS


class TestContract:
    def test_max_terms_is_enforced_and_must_be_positive(self) -> None:
        text = "Will Pfizer, Moderna, Merck, Intel, Oracle, Nvidia, Tesla, Boeing or Sony act?"
        assert len(terms(text, max_terms=8)) == 8
        assert len(terms(text, max_terms=2)) == 2
        with pytest.raises(ValueError, match="max_terms must be positive"):
            terms(text, max_terms=0)

    def test_extraction_is_deterministic(self) -> None:
        text = "Will the UAW strike Ford, General Motors and Stellantis?"
        assert terms(text) == terms(text)

    @pytest.mark.parametrize(
        "hostile",
        [
            "Will O'Brien & Sons | Acme:* win?",
            "Will (Foo) <-> !Bar 'Baz' \\Qux win?",
            "Will Foó Bär win?  \t Will X—Y?",
            "'; DROP TABLE chunks; -- Will Evil Corp win?",
        ],
    )
    def test_a_term_can_never_carry_query_syntax(self, hostile: str) -> None:
        """Lower-case alphanumerics and single spaces only. The SQL uses
        `plainto_tsquery`, which cannot raise -- this is the second lock."""
        for term in terms(hostile, "A&B | C:*", "x' OR '1'='1"):
            assert SAFE_TERM.match(term), term


# Questions as the sealed registry words them -- label-free, and chosen for the
# shapes that broke earlier drafts of the extractor: a headline with no verb, a
# placeholder sibling market, a possessive, a hyphenated range, a title in
# title case.
REGISTRY_QUESTIONS = (
    "Will the House of Commons approve the Withdrawal Agreement before 1 April 2019?",
    "Will the United States announce withdrawal from the JCPOA before the end of May 2018?",
    "The US and/or Israel will strike Iran\u2019s Natanz nuclear facility in 2025",
    "Will January 2026 be the 4th or lower hottest on record?",
    "Will Country N be the Jury Winner in the Eurovision 2026 Grand Final?",
    "Will the Fed Pause\u2013Pause\u2013Pause in the next three decisions (Mar\u2013Apr\u2013Jun)?",
    "Will Gold (GC) settle at $3,800-$4,200 in June?",
    "Iran Strike on Israel by February 28?",
    "Nothing Ever Happens: Russia Edition",
    "Will Party M win the most seats in the 2026 Slovenian parliamentary election?",
    "Will The Life of a Showgirl\u2019s first-week album sales be between 3000000 and 3250000?",
    'Will Jerome Powell say "good afternoon" during the May meeting?',
    "Will Company A be the second-largest company in the world by market cap on June 30?",
)


class TestOnRegistryShapedQuestions:
    @pytest.mark.parametrize("question", REGISTRY_QUESTIONS)
    def test_every_term_is_safe_and_bounded(self, question: str) -> None:
        found = entity_terms(question, max_terms=8)
        assert len(found) <= 8
        assert all(SAFE_TERM.match(term) for term in found), found

    @pytest.mark.parametrize("question", REGISTRY_QUESTIONS)
    def test_no_calendar_or_scaffolding_word_is_ever_a_term(self, question: str) -> None:
        """Checked on the agent's real query shape, objective sentence included."""
        banned = {"will", "april", "may", "june", "yes", "no", "other", "any", "company", "keep"}
        query = agent_evidence_query(question, "Some Actor", "Keep things as they are.")
        found = entity_terms(query.keyword_text, entities=query.entities, max_terms=8)
        words = {word for term in found for word in term.split()}
        assert not banned & words, found

    def test_the_known_answers(self) -> None:
        assert entity_terms(REGISTRY_QUESTIONS[0], max_terms=8) == (
            "house commons",
            "withdrawal agreement",
        )
        assert entity_terms(REGISTRY_QUESTIONS[1], max_terms=8) == ("united states", "jcpoa")
        assert entity_terms(REGISTRY_QUESTIONS[3], max_terms=8) == ()
        assert entity_terms(REGISTRY_QUESTIONS[12], max_terms=8) == ()
