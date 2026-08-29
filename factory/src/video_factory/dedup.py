"""Deterministic, dependency-free duplicate detection for IdeaCard-like objects.

The module deliberately measures lexical overlap rather than semantic meaning.  It
is suitable for a fast editorial pre-check and keeps borderline paraphrases in a
human review queue.  Inputs may be mappings (the JSON IdeaCard representation) or
objects exposing ``title``, ``hook``, ``message``, and ``source_candidates``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
import re
import unicodedata


Decision = Literal["allow", "review", "block"]

_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)


@dataclass(frozen=True, slots=True)
class DedupThresholds:
    """Thresholds for converting a similarity score into an editorial decision."""

    # Lexical paraphrases in morphologically rich languages often share only
    # about one fifth of their exact tokens/character grams.  The lower review
    # threshold intentionally creates a human-review band; blocking remains
    # conservative.
    review: float = 0.20
    block: float = 0.78

    def __post_init__(self) -> None:
        if not 0.0 <= self.review <= 1.0:
            raise ValueError("review threshold must be between 0 and 1")
        if not 0.0 <= self.block <= 1.0:
            raise ValueError("block threshold must be between 0 and 1")
        if self.review > self.block:
            raise ValueError("review threshold cannot be greater than block threshold")


@dataclass(frozen=True, slots=True)
class SimilarityWeights:
    """Weights used by :func:`compare_idea_cards`.

    Field weights and metric weights are normalized at calculation time, so a
    caller may provide any non-negative values rather than values summing to one.
    """

    title: float = 0.40
    hook: float = 0.35
    message: float = 0.25
    token: float = 0.65
    char_ngram: float = 0.35

    def __post_init__(self) -> None:
        values = (self.title, self.hook, self.message, self.token, self.char_ngram)
        if any(value < 0.0 for value in values):
            raise ValueError("similarity weights cannot be negative")
        if self.title + self.hook + self.message <= 0.0:
            raise ValueError("at least one field weight must be positive")
        if self.token + self.char_ngram <= 0.0:
            raise ValueError("at least one metric weight must be positive")


@dataclass(frozen=True, slots=True)
class CanonicalField:
    """Canonical text and tokens for one IdeaCard field."""

    text: str
    tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CanonicalIdea:
    """Canonical lexical representation of an IdeaCard."""

    title: CanonicalField
    hook: CanonicalField
    message: CanonicalField
    source_urls: frozenset[str]


@dataclass(frozen=True, slots=True)
class FieldSimilarity:
    """Similarity components for one text field."""

    token_jaccard: float
    char_ngram_jaccard: float
    combined: float


@dataclass(frozen=True, slots=True)
class DedupResult:
    """Pairwise comparison result."""

    decision: Decision
    score: float
    reason: str
    shared_source_urls: tuple[str, ...]
    title: FieldSimilarity
    hook: FieldSimilarity
    message: FieldSimilarity

    @property
    def source_url_collision(self) -> bool:
        return bool(self.shared_source_urls)

    @property
    def field_scores(self) -> dict[str, FieldSimilarity]:
        return {"title": self.title, "hook": self.hook, "message": self.message}

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        def serialize_field(value: FieldSimilarity) -> dict[str, float]:
            return {
                "token_jaccard": value.token_jaccard,
                "char_ngram_jaccard": value.char_ngram_jaccard,
                "combined": value.combined,
            }

        return {
            "decision": self.decision,
            "score": self.score,
            "reason": self.reason,
            "source_url_collision": self.source_url_collision,
            "shared_source_urls": list(self.shared_source_urls),
            "field_scores": {
                "title": serialize_field(self.title),
                "hook": serialize_field(self.hook),
                "message": serialize_field(self.message),
            },
        }


@dataclass(frozen=True, slots=True)
class DedupEvaluation:
    """A candidate evaluated against an existing IdeaCard collection."""

    decision: Decision
    best_match_index: int | None
    best_match: DedupResult | None
    comparisons: tuple[DedupResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "best_match_index": self.best_match_index,
            "best_match": self.best_match.as_dict() if self.best_match else None,
            "comparisons": [item.as_dict() for item in self.comparisons],
        }


DEFAULT_THRESHOLDS = DedupThresholds()
DEFAULT_WEIGHTS = SimilarityWeights()


def canonicalize_text(value: object) -> str:
    """Return normalized, case-folded Unicode text with canonical spacing.

    NFKC makes compatibility forms comparable, while ``casefold`` supports
    Cyrillic and other Unicode scripts without an English-only lowercasing rule.
    Punctuation and underscores act as token separators.
    """

    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(_TOKEN_RE.findall(normalized))


def canonical_tokens(value: object) -> tuple[str, ...]:
    """Return the ordered canonical tokens for a text value."""

    canonical = canonicalize_text(value)
    return tuple(canonical.split()) if canonical else ()


def char_ngrams(value: object, n: int = 3) -> frozenset[str]:
    """Return character n-grams of canonical text.

    For a non-empty string shorter than ``n``, the complete canonical string is
    returned as one gram.  This keeps short titles comparable.
    """

    if n < 1:
        raise ValueError("n must be at least 1")
    text = canonicalize_text(value)
    if not text:
        return frozenset()
    if len(text) < n:
        return frozenset((text,))
    return frozenset(text[index : index + n] for index in range(len(text) - n + 1))


def jaccard_similarity(left: Iterable[str], right: Iterable[str]) -> float:
    """Return set Jaccard similarity in the inclusive range 0..1."""

    left_set = frozenset(left)
    right_set = frozenset(right)
    if not left_set and not right_set:
        return 1.0
    union = left_set | right_set
    return len(left_set & right_set) / len(union)


def canonicalize_idea(card: Mapping[str, Any] | object) -> CanonicalIdea:
    """Build canonical title/hook/message tokens and exact source URL set."""

    def field(name: str, default: Any = "") -> Any:
        if isinstance(card, Mapping):
            return card.get(name, default)
        return getattr(card, name, default)

    def canonical_field(name: str) -> CanonicalField:
        value = field(name)
        if not value and name == "title":
            value = field("title_ru")
        if not value and name == "message":
            value = field("viewer_promise")
        if name == "hook" and isinstance(value, Mapping):
            value = value.get("text", "")
        text = canonicalize_text(value)
        return CanonicalField(text=text, tokens=tuple(text.split()) if text else ())

    source_urls: set[str] = set()
    candidates = field("source_candidates", None)
    if candidates is None:
        candidates = field("sources", ())
    if candidates is None or isinstance(candidates, (str, bytes)):
        candidates = (candidates,) if candidates else ()
    if isinstance(candidates, Sequence) or isinstance(candidates, Iterable):
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                url = candidate.get("url")
            elif isinstance(candidate, str):
                url = candidate
            else:
                url = getattr(candidate, "url", None)
            if url is not None and str(url).strip():
                # This is intentionally an exact collision check after trimming
                # surrounding whitespace.  Paths and queries can be case-sensitive,
                # so broader URL canonicalization could create false positives.
                source_urls.add(str(url).strip())

    return CanonicalIdea(
        title=canonical_field("title"),
        hook=canonical_field("hook"),
        message=canonical_field("message"),
        source_urls=frozenset(source_urls),
    )


def _field_similarity(
    left: CanonicalField,
    right: CanonicalField,
    *,
    ngram_size: int,
    weights: SimilarityWeights,
) -> FieldSimilarity:
    # Missing optional data must not make two incomplete cards look identical.
    if not left.text or not right.text:
        return FieldSimilarity(0.0, 0.0, 0.0)

    token_score = jaccard_similarity(left.tokens, right.tokens)
    char_score = jaccard_similarity(
        char_ngrams(left.text, ngram_size), char_ngrams(right.text, ngram_size)
    )
    metric_total = weights.token + weights.char_ngram
    combined = (
        token_score * weights.token + char_score * weights.char_ngram
    ) / metric_total
    return FieldSimilarity(
        token_jaccard=round(token_score, 12),
        char_ngram_jaccard=round(char_score, 12),
        combined=round(combined, 12),
    )


def compare_idea_cards(
    left: Mapping[str, Any] | object,
    right: Mapping[str, Any] | object,
    *,
    thresholds: DedupThresholds = DEFAULT_THRESHOLDS,
    weights: SimilarityWeights = DEFAULT_WEIGHTS,
    ngram_size: int = 3,
) -> DedupResult:
    """Compare two IdeaCards and return a deterministic editorial decision.

    An exact source URL collision is a blocking signal even when the text is
    unrelated.  Otherwise the configured score thresholds select ``allow``,
    ``review``, or ``block``.
    """

    if ngram_size < 1:
        raise ValueError("ngram_size must be at least 1")

    left_idea = canonicalize_idea(left)
    right_idea = canonicalize_idea(right)
    title = _field_similarity(
        left_idea.title, right_idea.title, ngram_size=ngram_size, weights=weights
    )
    hook = _field_similarity(
        left_idea.hook, right_idea.hook, ngram_size=ngram_size, weights=weights
    )
    message = _field_similarity(
        left_idea.message, right_idea.message, ngram_size=ngram_size, weights=weights
    )

    field_total = weights.title + weights.hook + weights.message
    raw_score = (
        title.combined * weights.title
        + hook.combined * weights.hook
        + message.combined * weights.message
    ) / field_total
    score = round(raw_score, 12)
    shared_urls = tuple(sorted(left_idea.source_urls & right_idea.source_urls))

    if shared_urls:
        decision: Decision = "block"
        reason = "exact_source_url_collision"
    elif score >= thresholds.block:
        decision = "block"
        reason = "similarity_at_or_above_block_threshold"
    elif score >= thresholds.review:
        decision = "review"
        reason = "similarity_at_or_above_review_threshold"
    else:
        decision = "allow"
        reason = "similarity_below_review_threshold"

    return DedupResult(
        decision=decision,
        score=score,
        reason=reason,
        shared_source_urls=shared_urls,
        title=title,
        hook=hook,
        message=message,
    )


def similarity_score(
    left: Mapping[str, Any] | object,
    right: Mapping[str, Any] | object,
    *,
    weights: SimilarityWeights = DEFAULT_WEIGHTS,
    ngram_size: int = 3,
) -> float:
    """Convenience wrapper returning only the deterministic similarity score."""

    return compare_idea_cards(
        left,
        right,
        weights=weights,
        ngram_size=ngram_size,
    ).score


def evaluate_candidate(
    candidate: Mapping[str, Any] | object,
    existing_cards: Iterable[Mapping[str, Any] | object],
    *,
    thresholds: DedupThresholds = DEFAULT_THRESHOLDS,
    weights: SimilarityWeights = DEFAULT_WEIGHTS,
    ngram_size: int = 3,
) -> DedupEvaluation:
    """Evaluate one candidate against a collection and return the strictest match."""

    comparisons = tuple(
        compare_idea_cards(
            candidate,
            existing,
            thresholds=thresholds,
            weights=weights,
            ngram_size=ngram_size,
        )
        for existing in existing_cards
    )
    if not comparisons:
        return DedupEvaluation("allow", None, None, comparisons)

    priority = {"allow": 0, "review": 1, "block": 2}
    best_index, best_match = max(
        enumerate(comparisons),
        key=lambda item: (
            priority[item[1].decision],
            item[1].source_url_collision,
            item[1].score,
            -item[0],
        ),
    )
    return DedupEvaluation(
        decision=best_match.decision,
        best_match_index=best_index,
        best_match=best_match,
        comparisons=comparisons,
    )


__all__ = [
    "CanonicalField",
    "CanonicalIdea",
    "DEFAULT_THRESHOLDS",
    "DEFAULT_WEIGHTS",
    "Decision",
    "DedupEvaluation",
    "DedupResult",
    "DedupThresholds",
    "FieldSimilarity",
    "SimilarityWeights",
    "canonical_tokens",
    "canonicalize_idea",
    "canonicalize_text",
    "char_ngrams",
    "compare_idea_cards",
    "evaluate_candidate",
    "jaccard_similarity",
    "similarity_score",
]
