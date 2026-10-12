"""Wikipedia revision snapshots (spec §3.2) -- the one source that is about the question.

CC-NEWS is a slice of whatever the world published in a few hours; whether it
mentions one particular merger or election is luck. The article *on* that
merger, as it read the day before the cutoff, is on topic by construction. So
this source is small in volume and large in value, and it is the most
leakage-prone source in the corpus: the current article states the outcome.

Four rules keep it honest, and each is asserted by a test.

**The revision is strictly before the cutoff.** MediaWiki's ``rvstart`` is
inclusive -- measured: asking for ``rvstart=2026-02-17T17:10:13Z`` returns the
revision saved at exactly that second. Chronofence admits ``published_at <
as_of`` and nothing else, so the request is anchored one second earlier *and*
the answer is checked, because the first is a courtesy from the server and the
second is the guarantee.

**The body is the revision's own wikitext, never a render.**
``action=parse&oldid=`` expands an old revision against the templates *as they
are today*. Measured: the 2024-25 Houston Rockets season article as of
2025-02-14 renders with "Updated: August 26, 2026" and the final standings
(52-30, second in the West) -- the answer to a scenario whose cutoff is
2025-02-17, inside a document dated three days before it. A revision's
wikitext is exactly the bytes that existed at its timestamp, so the text is
built from that and every transclusion is dropped; an infobox's parameters are
kept, because they are written in the article itself.

**A title means what it meant at the cutoff.** A redirect is followed only as
the redirect page read at the cutoff, never as it points today, so a title
repointed afterwards cannot bring its new target in; a page created afterwards
has no revision to return, and the first revision is never a fallback. When a
title held nothing then, or only a redirect to itself, the move log names the
page that was moved away from it, and that page is read as of the cutoff too.
A disambiguation page is followed only where it declared a primary topic at
the cutoff, and only to one the scenario's own words confirm. What is stored
is always a revision strictly before the cutoff; what today's wiki still
decides is which page a title is looked up on (see :func:`resolve`).

**Selection reads the scenario's text, names and cutoff, and nothing else.**
No search: CirrusSearch ranks against today's index, so which articles it
returns is downstream of everything that has happened since. Candidate titles
are derived from the question and the registry's ``party_names``; the one-hop
expansion follows links in the lead *of the as-of revision*.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from cascade.corpus.fetch import BodyVerdict, Fetcher
from cascade.corpus.schema import RawDocument

__all__ = [
    "API_URL",
    "HOP_SOURCES",
    "LEGACY_DEPTH",
    "PASS_TITLES",
    "Revision",
    "Selection",
    "SnapshotUnit",
    "candidate_slots",
    "candidate_titles",
    "classify_body",
    "existing_titles",
    "hop_titles",
    "is_disambiguation",
    "lead_links",
    "lead_text",
    "load_snapshots",
    "mentions",
    "moved_page",
    "plan_unit",
    "primary_topics",
    "rank_primary",
    "redirect_target",
    "resolve",
    "revision_before",
    "select",
    "split_unit",
    "stems",
    "unit_key",
    "wikitext_to_text",
]

API_URL = "https://en.wikipedia.org/w/api.php"

# Wikimedia's API etiquette: send `maxlag` so a lagged replica refuses the
# request instead of serving it, and wait when it does. Five seconds is the
# value their documentation recommends for non-interactive clients.
MAXLAG_SECONDS = 5

# Candidate titles one unit covers. **Part of what a unit key means, so it is a
# constant and not configuration**: a unit is recorded `done` forever, and if
# this were a setting, raising it would change what depth 1 covers without
# changing depth 1's key -- the units already done would stay frozen at the old
# width and nothing could reach the difference (the ADR-0023 trap). Deeper
# coverage is bought with `corpus.wikipedia_depth`, which adds keys.
PASS_TITLES = 12

# Articles whose lead links feed the one-hop expansion: the first this many
# candidates, in priority order, that resolve to a real article at the cutoff.
HOP_SOURCES = 3

# Depth of a unit keyed by the bare scenario id. Those rows were written by the
# adapter this one replaces -- the first six `party_names` as literal titles --
# and are read as what they meant, never regenerated and never re-run.
LEGACY_DEPTH = 0

_UNIT_KEY = re.compile(r"^d(?P<depth>\d{2})\.(?P<cutoff>\d{8})\.(?P<scenario>.+)$", re.DOTALL)

_MAX_REDIRECT_HOPS = 2


@dataclass(frozen=True, slots=True)
class SnapshotUnit:
    """What one ``(scenario, depth)`` unit asks for, anchored to one instant.

    ``titles`` are fetched as articles. ``hop_sources`` are read only for the
    links in their leads, of which this unit owns ``[hop_skip, hop_skip +
    hop_take)`` -- a window, so that no two depths of one scenario ever ask
    for the same link.

    The anchor is required and must be aware, for the reason ``as_of`` is
    required everywhere else (invariant 1): a naive instant cannot be ordered
    against a revision timestamp, and guessing its offset is a leak.
    """

    as_of: datetime
    titles: tuple[str, ...]
    hop_sources: tuple[str, ...] = ()
    hop_skip: int = 0
    hop_take: int = 0
    # What the question itself names, and which titles -- direct or hop
    # sources -- count only if their as-of lead mentions one of them.
    names: tuple[str, ...] = ()
    gated: frozenset[str] = frozenset()
    # Word stems of the question and its parties: how a disambiguation page's
    # declared primary topics are told apart (see `choose_primary`).
    context: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware; a naive anchor cannot order revisions")
        if self.hop_skip < 0 or self.hop_take < 0:
            raise ValueError("a hop window cannot be negative")


@dataclass(frozen=True, slots=True)
class Revision:
    """One page as it read at one instant: the unit of time-locked evidence.

    ``title`` is the title the page was *asked for* under, which comes from the
    scenario's text or from as-of wikitext. The API also reports the page's
    current title, and that is deliberately not kept: a page renamed after the
    cutoff ("Shooting of X" to "Assassination of X") would carry the outcome
    in its name.
    """

    title: str
    revision_id: int
    timestamp: datetime
    wikitext: str


# ---------------------------------------------------------------------------
# Unit keys
# ---------------------------------------------------------------------------


def unit_key(scenario_id: str, cutoff: datetime, depth: int) -> str:
    """``d{depth}.{cutoff date}.{scenario}`` -- everything the unit's contents depend on.

    Preserves the rule that a `done` row can only ever mean one thing. Depth is
    in the key so a deeper pass is new work rather than work already recorded
    (ADR-0023); the cutoff is in the key because a snapshot taken at a
    different cutoff is a different snapshot, so a moved cutoff re-fetches
    instead of silently keeping revisions anchored to the old one.

    The order of the parts is load-bearing. The queue sorts Wikipedia units by
    key alone (they carry no month, so `order_units` gives them equal depth and
    demand), which makes the run depth-major -- an interrupted pass leaves every
    scenario with its direct articles rather than a few scenarios complete --
    and, within a depth, earliest cutoff first. That second property is what
    near-duplicate collapse needs: the earliest revision of an article is the
    one every later scenario may still read, so it should be the one stored.
    """
    if not 1 <= depth <= 99:
        raise ValueError(f"depth must be in [1, 99], got {depth}")
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError(f"cutoff must be timezone-aware for {scenario_id!r}")
    return f"d{depth:02d}.{cutoff.astimezone(UTC):%Y%m%d}.{scenario_id}"


def split_unit(key: str) -> tuple[str, int]:
    """Return ``(scenario_id, depth)``; a bare scenario id is the legacy depth.

    Total over every key this source has ever written, so a row recorded by
    the previous adapter is recognised rather than orphaned.
    """
    match = _UNIT_KEY.match(key)
    if match is None:
        return key, LEGACY_DEPTH
    return match.group("scenario"), int(match.group("depth"))


# ---------------------------------------------------------------------------
# Wikitext -> text. Pure.
# ---------------------------------------------------------------------------

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_REF = re.compile(r"<ref\b[^>]*?/>|<ref\b[^>]*>.*?</ref\s*>", re.DOTALL | re.IGNORECASE)
# Extension tags whose contents are markup or data, not prose.
_OPAQUE_TAG = re.compile(
    r"<(gallery|math|timeline|imagemap|syntaxhighlight|source|score|chem|graph|"
    r"mapframe|templatestyles|pre|code)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HEADING = re.compile(r"^\s*(={2,6})\s*(.*?)\s*\1\s*$")
_EXTERNAL_LINK = re.compile(r"\[(?:https?:)?//[^\s\]]+\s*([^\]]*)\]")
_BARE_URL = re.compile(r"(?:https?:)?//[^\s\]|}]+")
_MAGIC_WORD = re.compile(r"__[A-Z]+__")
_QUOTES = re.compile(r"'{2,5}")
_LIST_MARKER = re.compile(r"^[*#:;]+\s*")
_REDIRECT = re.compile(r"#REDIRECT\s*:?\s*\[\[\s*([^\]|#]+)", re.IGNORECASE)
_WIKILINK = re.compile(r"\[\[([^\[\]|]+)(?:\|[^\[\]]*)?\]\]")
_DISAMBIGUATION = re.compile(
    r"\{\{\s*(?:disambig|disambiguation|dab|disamb|hndis|geodis|schooldis|roaddis|"
    r"numberdis|letter-numbercombdisambig|mil-unit-dis|surname|given name|"
    r"[a-z -]*disambiguation[a-z -]*)\s*(?:\||\}\})",
    re.IGNORECASE,
)

# Namespaces whose links are media, categories or other wikis: not articles.
_NON_ARTICLE_PREFIX = re.compile(
    r"^\s*:?\s*(?:file|image|media|category|wikipedia|wp|template|help|portal|draft|"
    r"special|user|talk|module|wikt|wiktionary|commons|s|q|d|m|[a-z]{2,3}(?:-[a-z]+)?)\s*:",
    re.IGNORECASE,
)

# Sections that are lists of links and citations. With the refs stripped, what
# is left of them is a column of titles, which embeds as noise.
_TRAILING_SECTIONS = frozenset(
    {
        "references",
        "external links",
        "see also",
        "further reading",
        "notes",
        "citations",
        "footnotes",
        "bibliography",
        "sources",
        "explanatory notes",
    }
)

# Inline templates whose *arguments* are the sentence's own words. Everything
# else is dropped: what a template expands to is decided by the template page,
# which is not part of this revision and cannot be read as of its date.
_KEEP_LAST_ARGUMENT = frozenset(
    {"lang", "langx", "nowrap", "nobr", "nowr", "abbr", "small", "big", "em", "strong"}
)
_DASH_TEMPLATES = frozenset({"snd", "spnd", "ndash", "spaced ndash", "en dash", "mdash", "snds"})
# Infobox parameters that name a file or style the box rather than state a fact.
_INFOBOX_MARKUP_KEY = re.compile(
    r"^(?:image|img|logo|signature|caption|alt|map|flag|seal|coat|width|size|colou?r|"
    r"website|url|module|embed|child|bodyclass|titlestyle)",
    re.IGNORECASE,
)


def _balanced(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the ``closer`` matching the ``opener`` at ``start``, or -1."""
    depth = 0
    position = start
    width = len(opener)
    while position < len(text):
        if text.startswith(opener, position):
            depth += 1
            position += width
        elif text.startswith(closer, position):
            depth -= 1
            position += width
            if depth == 0:
                return position
        else:
            position += 1
    return -1


def _split_arguments(body: str) -> list[str]:
    """Split a template body on top-level pipes only."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    position = 0
    while position < len(body):
        pair = body[position : position + 2]
        if pair in ("{{", "[["):
            depth += 1
            current.append(pair)
            position += 2
        elif pair in ("}}", "]]"):
            depth -= 1
            current.append(pair)
            position += 2
        elif body[position] == "|" and depth <= 0:
            parts.append("".join(current))
            current = []
            position += 1
        else:
            current.append(body[position])
            position += 1
    parts.append("".join(current))
    return parts


def _template_text(body: str, *, infobox: bool) -> str:
    """What one ``{{...}}`` contributes to the text: its own words, or nothing."""
    parts = _split_arguments(body)
    name = " ".join(parts[0].replace("_", " ").split()).lower()
    arguments = parts[1:]
    if name.startswith("infobox"):
        if not infobox:
            return ""
        # An infobox's parameters are typed into the article, so they are as
        # old as the revision. Rendered as "key: value" lines.
        lines = []
        for argument in arguments:
            key, separator, value = argument.partition("=")
            rendered = _strip_templates(value, infobox=False).strip() if separator else ""
            if rendered and key.strip() and not _INFOBOX_MARKUP_KEY.match(key.strip()):
                lines.append(f"{key.strip().replace('_', ' ')}: {rendered}")
        return "\n" + "\n".join(lines) + "\n" if lines else ""
    if name in _DASH_TEMPLATES:
        return " \u2013 "
    positional = [argument for argument in arguments if "=" not in argument]
    if name in _KEEP_LAST_ARGUMENT and positional:
        return _strip_templates(positional[-1], infobox=False)
    if name == "convert" and len(positional) >= 2:
        return f"{positional[0].strip()} {positional[1].strip()}"
    if name == "as of" and positional:
        return "As of " + " ".join(part.strip() for part in positional)
    return ""


def _strip_templates(text: str, *, infobox: bool = True) -> str:
    """Replace every ``{{...}}`` with the words it carries itself, if any."""
    out: list[str] = []
    position = 0
    while True:
        start = text.find("{{", position)
        if start < 0:
            out.append(text[position:])
            break
        out.append(text[position:start])
        end = _balanced(text, start, "{{", "}}")
        if end < 0:
            # Unbalanced markup: drop the rest rather than emit raw braces.
            break
        out.append(_template_text(text[start + 2 : end - 2], infobox=infobox))
        position = end
    return "".join(out)


def _strip_links(text: str) -> str:
    """``[[target|label]]`` to its label; media and category links to nothing."""
    out: list[str] = []
    position = 0
    while True:
        start = text.find("[[", position)
        if start < 0:
            out.append(text[position:])
            break
        out.append(text[position:start])
        end = _balanced(text, start, "[[", "]]")
        if end < 0:
            out.append(text[start + 2 :])
            break
        inner = text[start + 2 : end - 2]
        # A leading colon ("[[:Category:X]]") makes a media or category link an
        # ordinary visible one; nested brackets only occur in file captions.
        if "[[" not in inner and (
            not _NON_ARTICLE_PREFIX.match(inner) or inner.lstrip().startswith(":")
        ):
            out.append(inner.rsplit("|", 1)[-1].lstrip(":"))
        position = end
    return "".join(out)


def _strip_tables(text: str) -> str:
    """Flatten table markup to one line per row, cells separated by `` | ``.

    Row and cell lines are recognised outside ``{| ... |}`` as well: a table
    opened by a template (a game log, a results grid) leaves its rows behind
    once the template is gone, and they are the article's own as-of data.
    Expects links already reduced to their labels, so a pipe is a cell
    boundary or an attribute separator and nothing else.
    """
    lines: list[str] = []
    row: list[str] = []

    def flush() -> None:
        if row:
            lines.append(" | ".join(row))
            row.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("{|", "|}", "|-")):
            flush()
        elif stripped.startswith("|+"):
            flush()
            lines.append(stripped[2:].strip())
        elif stripped.startswith(("|", "!")):
            for cell in re.split(r"\|\||!!", stripped[1:]):
                # `style="..." | value`: what precedes a lone pipe is markup.
                content = cell.rpartition("|")[2].strip()
                if content:
                    row.append(content)
        else:
            flush()
            lines.append(line)
    flush()
    return "\n".join(lines)


def _drop_media_links(text: str) -> str:
    """Remove ``[[File:...]]``-style links whole, nested caption links included."""
    out: list[str] = []
    position = 0
    while True:
        start = text.find("[[", position)
        if start < 0:
            out.append(text[position:])
            break
        end = _balanced(text, start, "[[", "]]")
        if end < 0:
            out.append(text[position:])
            break
        inner = text[start + 2 : end - 2]
        keep = not _NON_ARTICLE_PREFIX.match(inner)
        out.append(text[position:end] if keep else text[position:start])
        position = end
    return "".join(out)


def _lead(wikitext: str) -> str:
    """The wikitext before the first section heading."""
    lines: list[str] = []
    for line in wikitext.splitlines():
        if _HEADING.match(line):
            break
        lines.append(line)
    return "\n".join(lines)


def _normalise_title(title: str) -> str:
    cleaned = " ".join(html.unescape(title).replace("_", " ").split())
    return cleaned[:1].upper() + cleaned[1:]


def redirect_target(wikitext: str) -> str | None:
    """The title this revision redirected to, or ``None`` if it was an article.

    Read from the revision's own text so a redirect is followed as it stood at
    the cutoff. A short page that *contains* a redirect also counts: measured,
    "Trump" on 2026-02-18 was a redirect wrapped in a redirects-for-discussion
    notice, which MediaWiki does not treat as a redirect and a reader does.
    """
    head = _COMMENT.sub("", wikitext).lstrip()
    match = _REDIRECT.match(head)
    if match is None and len(wikitext) <= 2000:
        match = _REDIRECT.search(head)
    return _normalise_title(match.group(1)) if match is not None else None


def is_disambiguation(wikitext: str) -> bool:
    """Whether the revision was a disambiguation or name-list page.

    Such a page is a menu of unrelated subjects sharing a word. The registry's
    noisier party names ("Any", "Only", "Other") land on exactly these.
    """
    return _DISAMBIGUATION.search(wikitext) is not None


def lead_links(wikitext: str) -> list[str]:
    """Article titles linked from the lead's prose, in order of first appearance.

    Preserves the time lock on selection: the links are read from the as-of
    revision, so an article can only be reached through a connection that
    existed before the cutoff. Templates and media are removed first -- an
    infobox links every country and flag it mentions, a hatnote links the
    *other* topic, and an image caption is not the lead's prose.
    """
    lead = _strip_templates(_REF.sub("", _COMMENT.sub("", _lead(wikitext))), infobox=False)
    lead = _drop_media_links(lead)
    seen: set[str] = set()
    titles: list[str] = []
    for match in _WIKILINK.finditer(lead):
        target = match.group(1)
        if _NON_ARTICLE_PREFIX.match(target):
            continue
        title = _normalise_title(target.split("#", 1)[0])
        if title and title not in seen:
            seen.add(title)
            titles.append(title)
    return titles


def lead_text(wikitext: str) -> str:
    """The lead's prose without its infobox: what every relevance check reads.

    The first few thousand characters of a large article are its infobox, one
    field per line, so a check on "the start of the text" would never reach
    the sentences that say what the article is about.
    """
    return wikitext_to_text(_lead(wikitext), infobox=False)


def wikitext_to_text(wikitext: str, *, infobox: bool = True) -> str:
    """Plain text of a revision, from that revision's bytes alone.

    Preserves the time lock at the level of the body: nothing in the output
    can postdate the revision, because nothing outside the revision is
    consulted. That is the property a render cannot offer (see the module
    docstring), and it is why templates are dropped rather than expanded.
    """
    text = _COMMENT.sub("", wikitext)
    text = _REF.sub("", text)
    text = _OPAQUE_TAG.sub("", text)
    text = _strip_templates(text, infobox=infobox)
    text = _strip_links(text)
    text = _strip_tables(text)
    text = _EXTERNAL_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    text = _MAGIC_WORD.sub("", text)
    text = _QUOTES.sub("", text)
    text = _LINE_BREAK.sub(" ", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)

    lines: list[str] = []
    pending: list[str] = []  # headings not yet followed by any text
    skipping = False
    for raw in text.splitlines():
        heading = _HEADING.match(raw)
        if heading is not None:
            title = heading.group(2).strip()
            # A trailing section ends at the next heading of any level that is
            # not itself one; sub-headings inside it are skipped with it.
            skipping = title.lower() in _TRAILING_SECTIONS or (
                skipping and len(heading.group(1)) > 2
            )
            if len(heading.group(1)) == 2:
                # A section whose whole content was a transclusion ("Standings")
                # leaves a bare heading, which would embed as a claim of content.
                pending.clear()
            if not skipping and title:
                pending.append(title)
            continue
        if skipping:
            continue
        line = " ".join(_LIST_MARKER.sub("", raw).split())
        if line and line not in {"|", "}}", "{{"}:
            lines.extend(pending)
            pending.clear()
            lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Selection. Pure: scenario text, names and cutoff in, titles out.
# ---------------------------------------------------------------------------

_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_SEASON_YEAR = re.compile(r"^(?:19|20)\d{2}[-\u2013/]\d{2,4}$")
_POSSESSIVE = re.compile(r"(?:['\u2019]s|['\u2019])(?= |$)")
_YEAR_RANGE = re.compile(r"\b((?:19|20)\d{2})[-\u2013](\d{2}|\d{4})\b")
_NAME_SEPARATOR = re.compile(r"(?<=[A-Za-z.])[/\u2013\u2014](?=[A-Za-z])|(?<=[A-Za-z])-(?=[A-Z])")
# Markets opened before their field is known carry stand-in names ("Party I",
# "Company A", "Placeholder V"). They name nobody, and as titles they land on
# whatever happens to be called that -- "Team B" is a 1976 CIA exercise.
_PLACEHOLDER = re.compile(
    r"^(?:Artist|Candidate|Company|Country|Driver|Movie|Option|Party|Person|Placeholder|"
    r"Player|Song|Team)"
    r"(?: [A-Za-z]{1,2})?$"
)
# Punctuation that ends a name: "Erin Doherty (Adolescence)" is two names.
_BREAK_BEFORE = '("\u201c['
_BREAK_AFTER = ')"\u201d],;:?!'
_EDGE_PUNCTUATION = _BREAK_BEFORE + _BREAK_AFTER + ".'\u2018\u2019\u2026*"

# Words that open a question or join its clauses. Capitalised in a title-cased
# market question, and never the name of anything.
_NOT_A_NAME = frozenset(
    (  # noqa: SIM905 -- a hundred one-word lines are harder to review than eight
        "will who what which when where how does do did is are was were can could should would "
        "has have had the a an in on at by of for to from before after during until if or and "
        "nor but than then this that these those any all other only yes no not next "
        "more most less least first last over under between above below "
        "january february march april may june july august september october november december "
        "jan feb mar apr jun jul aug sep sept oct nov dec "
        "monday tuesday wednesday thursday friday saturday sunday "
        "q1 q2 q3 q4 pm am et est edt utc gmt"
    ).split()
)
_ARTICLE = "The"
# Words allowed *inside* a name, between two capitalised words.
_CONNECTORS = frozenset(
    {"of", "the", "a", "an", "de", "del", "la", "le", "van", "von", "for", "&", "al", "bin"}
)


def _tokens(question: str) -> list[tuple[str, bool]]:
    """Words of the question, each with whether a name may continue past it."""
    tokens: list[tuple[str, bool]] = []
    for raw in question.split():
        # "US/Israel" and "Jun\u2013Jul" are lists, not names, and "USA-Iran" is two;
        # "2025\u20132026" is one season and stays whole.
        pieces = [piece for piece in _NAME_SEPARATOR.split(raw) if piece]
        for index, piece in enumerate(pieces):
            word = piece.strip(_EDGE_PUNCTUATION + "-\u2013\u2014")
            broken_before = piece[:1] in _BREAK_BEFORE or index > 0 or not word
            # "Iran's Natanz facility" names Iran and Natanz, not "Iran's Natanz".
            broken_after = (
                piece[-1:] in _BREAK_AFTER
                or index < len(pieces) - 1
                or _POSSESSIVE.search(word) is not None
            )
            if broken_before and tokens:
                tokens[-1] = (tokens[-1][0], False)
            if word:
                tokens.append((word, not broken_after))
    return tokens


def _is_name_token(token: str, *, first: bool) -> bool:
    if _YEAR.match(token) or _SEASON_YEAR.match(token):
        return True
    if not first and token == _ARTICLE:
        # Capitalised mid-sentence, "The" belongs to a title: "The Life of a Showgirl".
        return True
    return token[:1].isupper() and token.lower() not in _NOT_A_NAME


def _name_runs(tokens: Sequence[tuple[str, bool]]) -> list[list[str]]:
    """Maximal runs of name tokens, connectors allowed only in the interior."""
    runs: list[list[str]] = []
    current: list[str] = []
    for index, (token, continues) in enumerate(tokens):
        if _is_name_token(token, first=index == 0) or (current and token.lower() in _CONNECTORS):
            current.append(token)
        else:
            runs.append(current)
            current = []
        if not continues:
            runs.append(current)
            current = []
    runs.append(current)
    trimmed: list[list[str]] = []
    for run in runs:
        while run and run[-1].lower() in _CONNECTORS:
            run = run[:-1]
        if run:
            trimmed.append(run)
    return trimmed


def _year_phrases(tokens: Sequence[tuple[str, bool]]) -> list[str]:
    """A year and the words that follow it: "2026 Slovenian parliamentary election".

    Wikipedia titles its event articles this way and writes the descriptive
    part in lower case, so a capitalised-run rule alone stops at "2026
    Slovenian". The word after the year must be capitalised -- that is what
    separates an event's name from "2026 be the 4th". Longest first: the
    batched existence check is what decides which of them is a title.
    """
    phrases: list[str] = []
    for index, (token, continues) in enumerate(tokens):
        if not (_YEAR.match(token) or _SEASON_YEAR.match(token)) or not continues:
            continue
        tail: list[str] = []
        for following, carries_on in tokens[index + 1 : index + 6]:
            if following.lower() in _NOT_A_NAME and following.lower() not in _CONNECTORS:
                break
            tail.append(following)
            if not carries_on:
                break
        while tail and tail[-1].lower() in _CONNECTORS:
            tail.pop()
        if tail and tail[0][:1].isupper():
            phrases.extend(" ".join([token, *tail[:length]]) for length in range(len(tail), 0, -1))
    return phrases


def _dated_forms(name: str, year: int) -> list[str]:
    """How Wikipedia titles the season, edition or year of ``name`` around ``year``.

    Most sports questions do not state their year ("Will the Denver Broncos
    win the AFC West?"), and the article that briefs them is the season's, not
    the franchise's; for a country it is "2025 in Iran". The year comes from
    the cutoff, which is the scenario's own; whether any of these existed by
    then is for the as-of lookup to say.
    """
    short = f"{year % 100:02d}"
    following = f"{(year + 1) % 100:02d}"
    return [
        f"{year} {name} season",
        f"{year - 1}\u2013{short} {name} season",
        f"{year}\u2013{following} {name} season",
        f"{year} {name}",
        f"{year} in {name}",
    ]


def _spellings(name: str) -> list[str]:
    """The ways Wikipedia might spell ``name``; the last is the plainest.

    Typewriter apostrophes, en-dashed seasons written its way ("2025\u201326",
    never "2025-2026"), and the name without its possessive ("Taylor Swift's").
    """

    def season(match: re.Match[str]) -> str:
        first, second = match.group(1), match.group(2)
        return f"{first}\u2013{second[-2:] if second[:2] == first[:2] else second}"

    plain = name.replace("\u2019", "'").replace("\u2018", "'")
    plain = _YEAR_RANGE.sub(season, plain)
    return [plain, _POSSESSIVE.sub("", plain)]


@dataclass(frozen=True, slots=True)
class Selection:
    """What a scenario's own text asks Wikipedia for.

    ``slots`` is the priority order: one name per slot, with its spellings and
    dated forms. ``names`` is what the *question* calls by name; ``sources``
    are the titles built from those names, whose leads feed the one-hop
    expansion. ``gated`` titles are single words of uncertain standing -- a
    registered party the question never mentions, or a capitalised word nobody
    registered; the class that holds "NASA" and "Freecs" and also "Year",
    "Song" and "Meeting" -- kept only when their as-of lead mentions one of
    the *other* ``names``. ``context`` is the stems of the question's and the
    parties' words, for :func:`choose_primary`.
    """

    slots: tuple[tuple[str, ...], ...]
    names: tuple[str, ...]
    sources: tuple[str, ...]
    gated: frozenset[str]
    context: frozenset[str]


def select(*, question: str, party_names: Sequence[str], cutoff: datetime) -> Selection:
    """Titles worth asking Wikipedia for, most specific first, grouped by name. Pure.

    Preserves the selection rule: the output is a function of the scenario's
    question, its registered party names and its cutoff. It never sees an
    outcome, a resolution date or anything Wikipedia says today, so two
    scenarios that differ only in how they resolved get the same selection.

    A slot is one name with its spellings and dated forms, and a unit covers a
    fixed number of *slots* (``PASS_TITLES``) -- so a guess that names no page
    costs a better candidate nothing. Order is priority: the question's own
    dated event, then the names the question itself uses, then the registry's
    remaining parties. Those include extraction noise ("PM ET", "Any"), so
    they go last, where a missing or disambiguation page displaces nothing.
    """
    tokens = _tokens(question)
    runs = _name_runs(tokens)
    words = {_POSSESSIVE.sub("", word) for word, _ in tokens}
    year = cutoff.astimezone(UTC).year

    dated = _year_phrases(tokens)
    dated.extend(
        " ".join(run) for run in runs if len(run) > 1 and any(_YEAR.match(word) for word in run)
    )
    phrases = [" ".join(run) for run in runs if len(run) > 1]
    # "LCK 2026" is LCK with a year on it, not a longer name that contains LCK.
    undated = [[word for word in run if not _YEAR.match(word)] for run in runs]
    inside_a_phrase = {word.lower() for run in undated if len(run) > 1 for word in run}
    singles = [run[0] for run in runs if len(run) == 1 and not _YEAR.match(run[0])]
    # The registry's extraction keeps some words that are no one's name ("PM ET",
    # "Any", "Jun"); a party made only of such words is not asked for.
    parties = [
        name.strip()
        for name in party_names
        if not all(word.lower() in _NOT_A_NAME for word in name.split())
    ]
    # Whole words in the question's own case: "Search" is not named by
    # "searched", nor "Year" by "this year".
    named = [name for name in parties if all(part in words for part in _spellings(name)[1].split())]
    unnamed = sorted((name for name in parties if name not in named), key=_party_rank)
    # A single word the question only uses inside a longer name -- the "Year" of
    # "Song of the Year" -- says less than the name does, so it waits its turn.
    subsumed = [name for name in [*named, *singles] if name.lower() in inside_a_phrase]

    slots: list[tuple[str, ...]] = []
    seen: set[str] = set()

    def add(name: str, *, with_dated_forms: bool) -> tuple[str, ...]:
        spellings = _spellings(name)
        base = spellings[-1]
        if len(base) <= 2 or _PLACEHOLDER.match(base) or _normalise_title(base).lower() in seen:
            return ()
        dated_forms = _dated_forms(base, year) if with_dated_forms else []
        titles = []
        for form in [*spellings, *dated_forms]:
            title = _normalise_title(form)
            if title.lower() not in seen:
                seen.add(title.lower())
                titles.append(title)
        slots.append(tuple(titles))
        return tuple(titles)

    # Everything the question calls by name vouches for an article that
    # mentions it, whether or not it also became a slot of its own.
    names = list(dict.fromkeys(_spellings(name)[-1] for name in [*phrases, *named, *singles]))
    sources: list[str] = []
    gated: set[str] = set()
    for name in dated:
        sources.extend(add(name, with_dated_forms=False))
    for name in [*phrases, *named, *singles]:
        if name not in subsumed:
            added = add(name, with_dated_forms=not any(ch.isdigit() for ch in name))
            sources.extend(added)
            if name in singles and name not in named:
                # One capitalised word nobody registered: "Freecs", and also
                # the "Meeting" of a title-cased "June Meeting".
                gated.update(added)
    for name in unnamed:
        added = add(name, with_dated_forms=False)
        if " " not in name:
            gated.update(added)
    for name in subsumed:
        gated.update(add(name, with_dated_forms=False))
    return Selection(
        slots=tuple(slots),
        names=tuple(names),
        sources=tuple(sources),
        gated=frozenset(gated),
        context=stems(" ".join([question, *party_names])),
    )


def stems(text: str) -> frozenset[str]:
    """Five-letter stems of the words of ``text`` that could carry a topic. Pure.

    Crude on purpose: "vehicles" and "vehicle" share "vehic", and nothing here
    needs to be finer than telling Tesla, Inc. from Nikola Tesla.
    """
    words = re.findall(r"[A-Za-z]{4,}", _COMMENT.sub("", text))
    return frozenset(word.lower()[:5] for word in words if word.lower() not in _NOT_A_NAME)


# Declared primary topics tried per disambiguation page: each untried one costs
# a lookup, and a page that declares more than a handful is a menu anyway.
_PRIMARY_TRIES = 4
_PRIMARY = re.compile(
    r"\b(?:most|more|usually|primarily|commonly|often|generally)\b[^.:\n]{0,20}\brefers?\s+to\b:?",
    re.IGNORECASE,
)


def primary_topics(wikitext: str) -> list[tuple[str, str]]:
    """The entries a disambiguation page declares primary: ``(title, entry text)``. Pure.

    Only a page that says so -- "Trump most commonly refers to:" -- declares
    any; "Solana may refer to:" is a menu, and choosing from a menu is a
    guess. Read from the as-of revision, so the declaration is the editors'
    view at the cutoff rather than today's redirect.
    """
    match = _PRIMARY.search(wikitext)
    if match is None:
        return []
    entries: list[tuple[str, str]] = []
    rest = wikitext[match.end() :].splitlines()
    inline = rest[0] if rest else ""
    lines = rest[1:] if not _WIKILINK.search(inline) else [inline]
    for line in lines:
        stripped = line.strip()
        if not stripped and not entries:
            continue
        if not stripped.startswith("*") and line is not inline:
            break
        if stripped.startswith("**"):
            continue
        link = _WIKILINK.search(stripped)
        if link is not None and not _NON_ARTICLE_PREFIX.match(link.group(1)):
            entries.append((_normalise_title(link.group(1).split("#", 1)[0]), stripped))
        if line is inline:
            break
    return entries


def rank_primary(
    entries: Sequence[tuple[str, str]], *, context: frozenset[str], title: str
) -> list[tuple[str, bool]]:
    """Declared primary topics in the order to try them, each with whether its
    own entry line already shares a word with the scenario. Pure.

    Preserves the selection rule: the inputs are an as-of page and the
    scenario's own words. The title's stems do not count -- every entry under
    "Tesla" says "Tesla". Entries whose line overlaps come first, best first;
    the rest follow in the editors' order and must prove themselves on their
    own as-of lead (see ``load_snapshots``). At most ``_PRIMARY_TRIES``.
    """
    own = stems(title)
    scored = [
        (len((stems(text) - own) & context), index, target)
        for index, (target, text) in enumerate(entries)
        if target != title
    ]
    ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
    return [(target, score > 0) for score, _, target in ranked[:_PRIMARY_TRIES]]


def candidate_slots(
    *, question: str, party_names: Sequence[str], cutoff: datetime
) -> tuple[tuple[str, ...], ...]:
    """The slots of :func:`select`, in priority order. Pure."""
    return select(question=question, party_names=party_names, cutoff=cutoff).slots


def mentions(text: str, names: Iterable[str]) -> tuple[int, int]:
    """How many of ``names`` occur in ``text`` as whole words: ``(multi-word, single-word)``.

    The relevance signal behind every conditional inclusion. Both arguments
    are time-locked -- an as-of lead and the scenario's own names -- so what it
    admits cannot depend on anything that happened after the cutoff.
    """
    haystack = text.lower()
    phrases = words = 0
    for name in sorted({name.lower() for name in names if len(name) > 2}):
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", haystack) is None:
            continue
        if " " in name:
            phrases += 1
        else:
            words += 1
    return phrases, words


def candidate_titles(*, question: str, party_names: Sequence[str], cutoff: datetime) -> list[str]:
    """Every candidate title, flattened in priority order. Pure."""
    slots = candidate_slots(question=question, party_names=party_names, cutoff=cutoff)
    return [title for slot in slots for title in slot]


def _party_rank(name: str) -> tuple[int, str]:
    """Multi-word names before single words: "Riot Games" before "Other"."""
    return (0 if " " in name.strip() else 1, name)


def plan_unit(
    *, question: str, party_names: Sequence[str], cutoff: datetime, depth: int
) -> SnapshotUnit:
    """The work one ``(scenario, depth)`` unit names. Pure.

    Preserves additivity across depths. A scenario has one ordered stream --
    its candidate slots, then the links in the leads of the first
    ``HOP_SOURCES`` of the question's own names that resolve -- and depth ``d`` owns positions
    ``[(d-1) * PASS_TITLES, d * PASS_TITLES)`` of it. Windows are disjoint by
    arithmetic, so a deeper pass can never repeat a shallower one and a
    shallower one never has to be reopened.

    The hop's links are not known until the sources have been read, so the
    unit carries the *window* into them rather than the titles.
    """
    if depth < 1:
        raise ValueError(f"depth must be >= 1, got {depth}; depth 0 is the legacy adapter's")
    selection = select(question=question, party_names=party_names, cutoff=cutoff)
    slots = selection.slots
    start = (depth - 1) * PASS_TITLES
    end = start + PASS_TITLES
    window = slots[start:end]
    titles = tuple(title for slot in window for title in slot)
    # The whole scenario's gated set, not this window's: hop sources are gated
    # too, and they are the same at every depth.
    gated = selection.gated
    if end <= len(slots):
        return SnapshotUnit(
            as_of=cutoff,
            titles=titles,
            names=selection.names,
            gated=gated,
            context=selection.context,
        )
    return SnapshotUnit(
        as_of=cutoff,
        titles=titles,
        hop_sources=selection.sources,
        hop_skip=max(0, start - len(slots)),
        hop_take=PASS_TITLES - len(window),
        names=selection.names,
        gated=gated,
        context=selection.context,
    )


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def classify_body(response: httpx.Response) -> BodyVerdict | None:
    """Classify a MediaWiki 2xx body: ``None`` when it is a real answer.

    MediaWiki reports every API failure under HTTP 200 with an ``error``
    object. Two of them mean "later": ``maxlag`` (a replica is behind, sent
    with ``Retry-After``) and ``ratelimited``. Treating those as answers is how
    a throttled ingest records a scenario as having no articles and marks the
    unit done -- permanently, because done units are skipped. Any other error
    is about the request and will not change, so it is not retried.
    """
    try:
        payload = response.json()
    except ValueError:
        return "unusable"
    if not isinstance(payload, dict):
        return "unusable"
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    if error.get("code") in {"maxlag", "ratelimited", "readonly"}:
        return "throttled"
    return "rejected"


def _api_url(**parameters: str) -> str:
    # Encoded, because a title is data: "AT&T" pasted into a query string is
    # the title "AT" and a stray parameter named "T".
    query = {
        **parameters,
        "format": "json",
        "formatversion": "2",
        "maxlag": str(MAXLAG_SECONDS),
    }
    return f"{API_URL}?{urlencode(sorted(query.items()))}"


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed.astimezone(UTC)


def _pages(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    query = payload.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    return [page for page in pages if isinstance(page, dict)] if isinstance(pages, list) else []


def existing_titles(fetcher: Fetcher, titles: Sequence[str]) -> set[str]:
    """Which of ``titles`` name a page today, in one request per fifty.

    An economy and never a selector: a title that survives this is still put
    to the as-of lookup, which alone decides whether it existed at the cutoff.
    All this removes is lookups that could not have succeeded -- a page absent
    today has no revisions to return -- so it cannot add an article, and the
    only ones it can drop are pages deleted since, which no client can read.
    """
    found: set[str] = set()
    for start in range(0, len(titles), 50):
        batch = titles[start : start + 50]
        payload = fetcher.get_json(_api_url(action="query", prop="info", titles="|".join(batch)))
        query = payload.get("query", {}) if isinstance(payload, dict) else {}
        renamed = {
            entry["to"]: entry["from"]
            for entry in query.get("normalized", [])
            if isinstance(entry, dict) and "to" in entry and "from" in entry
        }
        for page in _pages(payload):
            title = page.get("title")
            if isinstance(title, str) and not page.get("missing") and not page.get("invalid"):
                found.add(renamed.get(title, title))
    return found


def revision_before(
    fetcher: Fetcher, *, title: str, as_of: datetime, page_id: int | None = None
) -> Revision | None:
    """The last revision of ``title`` saved **strictly before** ``as_of``, or ``None``.

    Preserves the time lock: no revision at or after ``as_of`` is ever
    returned, whatever the server sends. ``rvstart`` is inclusive, so the
    request is anchored at the last whole second before ``as_of``; the answer
    is then checked against ``as_of`` itself, because a guarantee that rests
    on a remote parameter is a hope.

    ``None`` is the answer for a page that did not exist yet, and there is no
    fallback to its first revision. A page created after the cutoff -- "2026 X
    crisis", written once the crisis had happened -- is exactly the document
    that must not enter, and its earliest text already knows the outcome.

    ``page_id`` addresses the page by identity instead of by where it sits
    today (see :func:`resolve`); ``title`` is then only the name it is filed
    under.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError(f"as_of must be timezone-aware for {title!r}")
    # One microsecond back, then truncated to MediaWiki's one-second
    # resolution: the last instant a revision could carry and still be before.
    anchor = (as_of.astimezone(UTC) - timedelta(microseconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    page = {"titles": title} if page_id is None else {"pageids": str(page_id)}
    payload = fetcher.get_json(
        _api_url(
            action="query",
            prop="revisions",
            **page,
            rvstart=anchor,
            rvdir="older",
            rvlimit="1",
            rvprop="ids|timestamp|content",
            rvslots="main",
        )
    )
    pages = _pages(payload)
    if not pages or pages[0].get("missing") or pages[0].get("invalid"):
        return None
    revisions = pages[0].get("revisions")
    if not isinstance(revisions, list) or not revisions or not isinstance(revisions[0], dict):
        return None
    revision = revisions[0]
    revision_id = revision.get("revid")
    timestamp = _parse_ts(revision.get("timestamp"))
    slots = revision.get("slots")
    main = slots.get("main") if isinstance(slots, dict) else None
    wikitext = main.get("content") if isinstance(main, dict) else None
    if not isinstance(revision_id, int) or timestamp is None or not isinstance(wikitext, str):
        return None
    if timestamp >= as_of:
        # The anchor should make this unreachable. It is checked anyway: a
        # violation here would be a silent leak rather than a visible failure.
        return None
    return Revision(title=title, revision_id=revision_id, timestamp=timestamp, wikitext=wikitext)


def moved_page(fetcher: Fetcher, *, title: str, as_of: datetime) -> int | None:
    """The id of the page that was moved away from ``title`` first after ``as_of``.

    A revision belongs to a *page*, and a page can be renamed. Measured on
    this registry: the article at "Joint Comprehensive Plan of Action" on
    2018-01-12 was moved to another title in 2026 and its old title handed to a
    former redirect, so asking for that title as of 2018 returns a 48-byte
    redirect to itself -- and the one article the scenario is about is lost.
    The move log names the page that left; its id finds the same bytes
    wherever they are filed now.

    This reads a log entry written after the cutoff, and it is the one place
    that does. What it yields is where pre-cutoff text is *stored*, never text:
    the revision is still fetched strictly before ``as_of``, a page that did
    not exist by then still has none, and the document is filed under the
    title it was asked for -- the new title never reaches the corpus.
    """
    payload = fetcher.get_json(
        _api_url(
            action="query",
            list="logevents",
            letype="move",
            letitle=title,
            ledir="newer",
            lestart=as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            lelimit="1",
            leprop="ids|title|timestamp|details",
        )
    )
    query = payload.get("query") if isinstance(payload, dict) else None
    events = query.get("logevents") if isinstance(query, dict) else None
    if not isinstance(events, list) or not events or not isinstance(events[0], dict):
        return None
    page_id = events[0].get("logpage")
    return page_id if isinstance(page_id, int) and page_id > 0 else None


def resolve(fetcher: Fetcher, *, title: str, as_of: datetime) -> Revision | None:
    """``title`` as a reader would have found it at ``as_of``: redirects followed *as of then*.

    Preserves the rule that a title means what it meant at the cutoff. The
    redirect's target is read from the redirect page's own as-of revision, and
    the target is then looked up as of the same instant -- so a redirect
    created or repointed after the cutoff contributes nothing, and neither
    does a target that did not exist yet. The server is never asked to follow
    redirects, because it would follow today's.

    When the title holds nothing from before the cutoff, or only a redirect to
    itself, the page that held it then has been moved since;
    :func:`moved_page` finds it.
    """
    current = title
    visited = {title}
    for _ in range(_MAX_REDIRECT_HOPS + 1):
        revision = revision_before(fetcher, title=current, as_of=as_of)
        target = redirect_target(revision.wikitext) if revision is not None else None
        if revision is None or target == current:
            page_id = moved_page(fetcher, title=current, as_of=as_of)
            if page_id is None:
                return None
            revision = revision_before(fetcher, title=current, as_of=as_of, page_id=page_id)
            if revision is None:
                return None
            target = redirect_target(revision.wikitext)
        if target is None:
            return revision
        if target in visited:
            return None
        visited.add(target)
        current = target
    return None


_GEOGRAPHIC = re.compile(
    r"\{\{\s*Infobox (?:country|former country|settlement|continent|U\.S\. state|"
    r"province|region|political division)",
    re.IGNORECASE,
)
# How much of a lead's prose the relevance checks read. Deeper than that,
# every long article mentions everything.
_GATE_CHARS = 2500


def _document(revision: Revision, body: str) -> RawDocument:
    return RawDocument(
        source="wikipedia",
        # Distinct from the bare revision id the previous adapter wrote. That
        # adapter stored a *render*, which carries present-day template
        # content; sharing its id would let a tainted row shadow this one as a
        # "duplicate", and would leave no way to tell the two apart in SQL.
        source_ref=f"rev:{revision.revision_id}",
        url=f"https://en.wikipedia.org/w/index.php?oldid={revision.revision_id}",
        title=revision.title,
        body=body,
        published_at=revision.timestamp,
    )


def hop_titles(leads: Sequence[Sequence[str]], *, exclude: Iterable[str]) -> list[str]:
    """Order the links of several leads into one list. Pure.

    A title linked from more than one lead comes first -- two articles about
    the same question agreeing that something matters is the strongest signal
    available without reading an outcome -- and ties fall back to position in
    the lead, so the most specific source does not crowd out the others.
    Deterministic, because units window into this order.
    """
    excluded = {title.lower() for title in exclude}
    votes: dict[str, int] = {}
    position: dict[str, tuple[int, int]] = {}
    for source, lead in enumerate(leads):
        for index, title in enumerate(lead):
            if title.lower() in excluded:
                continue
            votes[title] = votes.get(title, 0) + 1
            position.setdefault(title, (index, source))
    return sorted(votes, key=lambda title: (-votes[title], position[title], title))


def load_snapshots(fetcher: Fetcher, unit: SnapshotUnit) -> Iterator[RawDocument]:
    """Yield one document per article the unit reaches, earliest revision first.

    Preserves three things.

    *The time lock.* Every document is a revision strictly before the unit's
    ``as_of``, with ``published_at`` that revision's own timestamp. The same
    article asked for at two cutoffs is two documents with two dates, and
    Chronofence's ``published_at < as_of`` hands each scenario only what
    precedes *its* cutoff -- storing just the latest would starve the earlier
    scenario, and would be the one copy a careless filter could leak.

    *Completeness of a `done` unit.* A fetch failure propagates instead of
    skipping the article: the pipeline then records the unit `failed` and
    retries it. Swallowing it would mark a half-fetched unit done, and done
    units are skipped forever.

    *Order.* Documents leave sorted by ``published_at``. Near-duplicate
    collapse keeps the first body it sees and the earliest date, so any other
    order could pair a later revision's text with an earlier one's date.
    """
    as_of = unit.as_of
    asked = sorted({*unit.titles, *(unit.hop_sources if unit.hop_take > 0 else ())})
    known = existing_titles(fetcher, asked) if asked else set()
    resolved: dict[str, Revision | None] = {}
    kept: dict[int, tuple[Revision, str]] = {}

    def article(title: str, *, checked: bool) -> tuple[Revision, str] | None:
        """``title`` as a real article at the cutoff, with its text; else ``None``."""
        if title not in resolved:
            exists = title in known or not checked
            resolved[title] = resolve(fetcher, title=title, as_of=as_of) if exists else None
        revision = resolved[title]
        if revision is not None and is_disambiguation(revision.wikitext):
            # A page of meanings; follow it only where, at the cutoff, it
            # declared primary ones, and only to one that is about this
            # scenario -- by its entry line, or else by its own as-of lead.
            ranked = rank_primary(
                primary_topics(revision.wikitext), context=unit.context, title=title
            )
            revision = None
            own = stems(title)
            for target, confirmed in ranked:
                if target not in resolved:
                    resolved[target] = resolve(fetcher, title=target, as_of=as_of)
                followed = resolved[target]
                if followed is None or is_disambiguation(followed.wikitext):
                    continue
                lead = stems(f"{target}\n{lead_text(followed.wikitext)[:_GATE_CHARS]}")
                if confirmed or (lead - own) & unit.context:
                    revision = followed
                    break
        if revision is None:
            return None
        return revision, wikitext_to_text(revision.wikitext)

    def vouched(title: str, revision: Revision) -> bool:
        """A gated title counts only if its as-of lead names something else the question does."""
        # Its own name does not vouch for it: "Meeting" mentions meetings.
        # And a name too short to count ("GC") cannot vouch for anything.
        others = [name for name in unit.names if name.lower() != title.lower() and len(name) > 2]
        return (
            title not in unit.gated
            or not others
            or sum(mentions(lead_text(revision.wikitext)[:_GATE_CHARS], others)) > 0
        )

    for title in unit.titles:
        found = article(title, checked=True)
        if found is not None and vouched(title, found[0]):
            kept.setdefault(found[0].revision_id, found)

    if unit.hop_take > 0:
        leads: list[list[str]] = []
        for title in unit.hop_sources:
            source = article(title, checked=True)
            # A country's lead links the whole world; what it links is context
            # for everything and evidence for nothing in particular. And a page
            # that does not count as evidence does not choose any either.
            if (
                source is not None
                and vouched(title, source[0])
                and not _GEOGRAPHIC.search(source[0].wikitext)
            ):
                leads.append(lead_links(source[0].wikitext))
            if len(leads) == HOP_SOURCES:
                break
        # Excludes the sources alone -- the same list at every depth. Excluding
        # this unit's own titles too would make the list, and so every
        # window into it, depend on the depth asking.
        linked = hop_titles(leads, exclude=unit.hop_sources)
        for title in linked[unit.hop_skip : unit.hop_skip + unit.hop_take]:
            # Not put to the existence check: a link in an as-of lead names a
            # page that existed then, so there is nothing for it to save.
            found = article(title, checked=False)
            if found is None:
                continue
            # A lead links its subject's whole context -- "American football",
            # "West Asia". One that is about *this* question says so: its own
            # lead names something the question names.
            about = lead_text(found[0].wikitext)[:_GATE_CHARS]
            phrases, words = mentions(f"{title}\n{about}", unit.names)
            if phrases >= 1 or words >= 2:
                kept.setdefault(found[0].revision_id, found)

    for _, (revision, body) in sorted(
        kept.items(), key=lambda item: (item[1][0].timestamp, item[0])
    ):
        yield _document(revision, body)
