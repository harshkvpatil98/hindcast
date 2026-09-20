"""Entity terms for the hybrid keyword pool (migration 018).

Pure: no I/O, no clock, no RNG.

The keyword pool exists to answer one question the embedder answers badly --
*does this chunk name the party?* -- so what it is asked for must be names and
nothing else. A study query is "<question> <actor name> <actor objective>",
thirty-odd words of which perhaps four identify anything. Handing all thirty
to ``plainto_tsquery`` ANDs them and matches no chunk; OR-ing them matches the
corpus. This module picks the four.

**Two sources, trusted differently.**

*Explicit entities* -- an actor's name from a compiled graph -- are taken
whole. Somebody already decided that string names a party.

*Free text* -- the question, an objective -- is mined for capitalised runs,
which is a guess, so it fails closed. The expensive mistake here is not a
missed name (the vector pool still runs) but a *manufactured* one: a term like
"keep" or "company" is a flood term that fills the keyword pool with chunks
chosen for no reason. So a capitalised word that opens a sentence is dropped
unless the text proves it is a name some other way, because English
capitalises every sentence and that capital carries no information.

**Nothing here may read an outcome.** Terms are built from the question and
the actor, never from a label or from resolution text. Resolution criteria in
particular are kept out by the callers: they are boilerplate ("12:00 PM ET",
"Associated Press") whose proper nouns name the resolver, not the parties.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from cascade.ledger.taxonomy import extract_known_actors

__all__ = ["MAX_TERM_WORDS", "entity_terms"]

# A name longer than this is a description, and an AND of seven lexemes
# matches nothing. Truncating keeps the head of the name, which is where the
# distinguishing words of an institutional name sit.
MAX_TERM_WORDS = 6

# Lower-case words that may sit *inside* a name ("Bank of England", "Strait of
# Hormuz", "Rassemblement de la ..."). "and" is deliberately absent: it joins
# two different parties, and admitting it would fuse "Russia and Ukraine" into
# one term that requires both -- the same phantom-party defect ADR-0009 records
# for the registry screen.
_CONNECTORS = frozenset({"al", "de", "del", "du", "la", "of", "the"})
_MAX_CONNECTORS_IN_A_ROW = 2

# Capitalised words that are never a party: question scaffolding, calendar
# words, and the vocabulary of a prediction market's own page. Compared
# casefolded -- "YES" opens most resolution criteria, and a case-sensitive
# list let it through as a proper noun once already (M1).
_NOT_A_NAME = frozenset(
    {
        # interrogatives and auxiliaries
        "are", "can", "could", "did", "do", "does", "had", "has", "have", "how", "is",
        "may", "might", "must", "shall", "should", "was", "were", "what", "when", "where",
        "whether", "which", "who", "whom", "whose", "why", "will", "would",
        # determiners, pronouns and prepositions that open a clause
        "a", "after", "all", "an", "and", "any", "as", "at", "before", "between", "but",
        "by", "during", "each", "every", "for", "from", "however", "if", "in", "it", "its",
        "no", "none", "not", "on", "only", "or", "other", "otherwise", "since", "some",
        "that", "their", "there", "these", "this", "those", "thus", "to", "under", "until",
        "with", "within", "without", "yes",
        # the calendar
        "january", "february", "march", "april", "june", "july", "august",
        "september", "october", "november", "december", "jan", "feb", "mar", "apr", "jun",
        "jul", "aug", "sep", "sept", "oct", "nov", "dec", "monday", "tuesday", "wednesday",
        "thursday", "friday", "saturday", "sunday", "am", "pm", "et", "est", "edt", "utc",
        "gmt",
        # a market page describing itself
        "market", "note", "question", "resolution", "resolve", "resolved", "resolves",
        "source",
        # the registry's own placeholders: sibling markets are stored as
        # "Company A", "Team B", "Party M", "Country N", and the noun left
        # behind once the letter is dropped names every company there is
        "candidate", "company", "country", "party", "person", "placeholder", "player",
        "team",
    }
)  # fmt: skip

_OPENERS = "([{\"'\u201c\u2018"
_RUN_CLOSERS = frozenset(',;:.?!)]}"\u201d')
_SENTENCE_CLOSERS = frozenset(".?!:;")
# Straight and curly apostrophes both: real question text uses both, and
# treating them as different characters would split one name in two (M1).
_APOSTROPHES = "'\u2019"
_CORE = re.compile(rf"[A-Za-z0-9](?:[A-Za-z0-9{_APOSTROPHES}&.\-]*[A-Za-z0-9])?")
_PIECE = re.compile(r"[A-Za-z0-9]+")
_POSSESSIVE = re.compile(rf"[{_APOSTROPHES}]s$", re.IGNORECASE)


@dataclass(frozen=True)
class _Token:
    core: str
    opens_sentence: bool
    opened_by_punctuation: bool
    closes_run: bool

    @property
    def is_capitalised(self) -> bool:
        return any(character.isupper() for character in self.core)

    @property
    def is_connector(self) -> bool:
        return self.core in _CONNECTORS

    @property
    def is_evidently_a_name(self) -> bool:
        """True when the capital cannot be explained by sentence position.

        "NATO", "OpenAI" and "G7" are names wherever they stand; "Keep" at the
        head of a sentence is not, and nothing about the word itself says so.
        """
        return any(character.isupper() for character in self.core[1:]) or any(
            character.isdigit() for character in self.core
        )


def _tokens(line: str) -> list[_Token]:
    out: list[_Token] = []
    opens_sentence = True
    for raw in line.split():
        stripped = raw.lstrip(_OPENERS)
        match = _CORE.search(stripped)
        if match is None:
            # Pure punctuation -- a dash, an ampersand. It separates whatever
            # stood on either side of it.
            if out:
                last = out[-1]
                out[-1] = _Token(last.core, last.opens_sentence, last.opened_by_punctuation, True)
            continue
        whole = match.group(0)
        core = _POSSESSIVE.sub("", whole)
        tail = stripped[match.end() :]
        out.append(
            _Token(
                core=core,
                opens_sentence=opens_sentence,
                opened_by_punctuation=len(stripped) != len(raw) or match.start() > 0,
                # A possessive ends the name it is attached to: in "Iran's
                # Natanz facility" the owner and the owned are two entities,
                # and one AND-term would require a chunk to name both.
                closes_run=core != whole or any(character in _RUN_CLOSERS for character in tail),
            )
        )
        opens_sentence = any(character in _SENTENCE_CLOSERS for character in tail)
    return out


def _runs(tokens: Sequence[_Token]) -> list[list[_Token]]:
    """Maximal capitalised runs, with lower-case connectors allowed inside."""
    runs: list[list[_Token]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.is_capitalised:
            index += 1
            continue
        run = [token]
        cursor = index
        while not tokens[cursor].closes_run:
            # Look past at most two connectors for the next capitalised word;
            # a connector followed by nothing capitalised ends the name.
            ahead = cursor + 1
            connectors = 0
            while (
                ahead < len(tokens)
                and tokens[ahead].is_connector
                and not tokens[ahead].closes_run
                and not tokens[ahead].opened_by_punctuation
                and connectors < _MAX_CONNECTORS_IN_A_ROW
            ):
                ahead += 1
                connectors += 1
            if (
                ahead >= len(tokens)
                or not tokens[ahead].is_capitalised
                or tokens[ahead].opened_by_punctuation
            ):
                break
            # Connectors stay in the run so the lexicon sees "Bank of England"
            # whole; `_pieces` drops them from the term itself.
            run.extend(tokens[cursor + 1 : ahead + 1])
            cursor = ahead
        runs.append(run)
        index = cursor + 1
    return runs


def _pieces(core: str, *, screen: bool) -> list[str]:
    """Lower-cased alphanumeric pieces of one word, minus what cannot identify.

    Single characters and bare numbers go: "A" in "Company A" and "2026" match
    a large share of any news corpus. ``screen`` additionally removes
    scaffolding words, and is off for explicit entities, whose words were
    chosen by whoever named the actor.
    """
    out: list[str] = []
    found = _PIECE.findall(core)
    if len(found) > 1 and any(len(piece) < 2 for piece in found):
        # "T-Mobile" and "AT&T" break into a letter and a remainder. The letter
        # cannot be searched for, and the remainder alone ("mobile") is a
        # different and far commoner word, so the whole token is given up.
        return []
    for piece in found:
        lowered = piece.casefold()
        if len(lowered) < 2 or lowered.isdigit() or lowered in _CONNECTORS:
            continue
        if screen and lowered in _NOT_A_NAME:
            continue
        out.append(lowered)
    return out


def entity_terms(text: str, *, entities: Sequence[str] = (), max_terms: int) -> tuple[str, ...]:
    """The entity phrases to ask the keyword pool for, most trusted first.

    Preserves three properties the keyword pool depends on. The result is a
    function of its arguments alone, so a replayed retrieval asks for the same
    terms. Every term is lower-case words joined by single spaces, so nothing
    in it can be read as query syntax by any text-search parser. And there are
    at most ``max_terms`` of them, which is the bound migration 018 also
    enforces on the work one call can cause.

    Explicit ``entities`` come first and survive truncation before anything
    mined from ``text``: an actor's own name is the term most worth keeping.
    An empty result is a valid answer -- it means "no name was found", and the
    hybrid path then degrades to the vector pool re-ranked by recency rather
    than searching for a guess.
    """
    if max_terms <= 0:
        raise ValueError(f"max_terms must be positive, got {max_terms}")

    terms: list[str] = []
    seen: set[str] = set()

    def add(words: list[str]) -> None:
        term = " ".join(words[:MAX_TERM_WORDS])
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    for entity in entities:
        words: list[str] = []
        for token in _tokens(entity.replace("\n", " ")):
            words.extend(_pieces(token.core, screen=False))
        add(words)

    lines = [_tokens(line) for line in text.splitlines()]
    # A word seen capitalised where no sentence starts is a name wherever else
    # it appears, including at the head of one.
    proven = {
        token.core
        for line in lines
        for token in line
        if token.is_capitalised and not token.opens_sentence
    }
    for line in lines:
        for run in _runs(line):
            words = []
            # The registry's actor lexicon is a third proof, and the only one
            # a headline-style question offers: "Iran Strike on Israel" opens
            # with a capital that position explains and the lexicon overrules.
            # Asked of the run with and without its first word, because the
            # lexicon finds "Iran" inside "Keep Iran" just as readily: only a
            # first word the recognition *depends on* is vouched for by it.
            names_a_known_actor = set(
                extract_known_actors(" ".join(token.core for token in run))
            ) != set(extract_known_actors(" ".join(token.core for token in run[1:])))
            for position, token in enumerate(run):
                unexplained_capital = (
                    position == 0
                    and token.opens_sentence
                    and not token.is_evidently_a_name
                    and token.core not in proven
                    and not names_a_known_actor
                )
                if unexplained_capital:
                    continue
                words.extend(_pieces(token.core, screen=True))
            add(words)

    return tuple(terms[:max_terms])
