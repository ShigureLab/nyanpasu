from __future__ import annotations

import math
import re
import unicodedata
from bisect import bisect_left
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

_WORDS = re.compile(r"[\u3400-\u9fff]+|[^\W\u3400-\u9fff]+")
_STOP_WORDS = frozenset(
    "a about after again all also and any are before can change changes check code context current do does "
    "for from have how into its memory new not now only please previous pull read repo repository request "
    "review run should some summary task test tests that the their them then there these this those through "
    "use using was were what when where which while will with would your "
    "chore doc docs documentation feat feature fix refactor "
    # Bare file extensions and source/doc path scaffolding are not topics.
    "py md js ts jsx tsx rst json yaml yml toml txt src en zh "
    "一下 如何 检查 看看 这个 这些 当前 任务 代码 修改 问题 需要 是否".split()
)


def tokenize(text: str) -> tuple[str, ...]:
    """Keep word frequencies, identifier components, and adjacent Chinese pairs."""
    terms = []
    for word in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if "\u3400" <= word[0] <= "\u9fff":
            terms.extend([word] if len(word) == 1 else (word[index : index + 2] for index in range(len(word) - 1)))
        elif word.strip("_"):
            terms.append(word)
            if "_" in word:
                terms.extend(part for part in word.split("_") if part)
    return tuple(terms)


def topic_terms(text: str) -> frozenset[str]:
    """Discard generic task vocabulary when selecting automatic summaries."""
    return frozenset(tokenize(text)) - _STOP_WORDS


def search_excerpt(text: str, query: str, limit: int = 800) -> str:
    """Return a bounded passage ranked with the same prefix search as recall."""
    terms = frozenset(tokenize(query))
    if len(text) <= limit or not terms:
        return text[:limit]
    passages = [text[start : start + limit] for start in range(0, len(text), max(1, limit // 2))]
    scores = bm25_scores(terms, [[(passage, 1.0)] for passage in passages], prefix=True)
    return passages[max(range(len(passages)), key=scores.__getitem__)]


def bm25_scores(
    query: Collection[str], documents: Sequence[Sequence[tuple[str, float]]], *, prefix: bool = False
) -> list[float]:
    """Score an authorized corpus using weighted field frequencies and raw lengths.

    Each document contains (text, weight) fields. Callers supply normalized query
    terms and filter eligibility before scoring; any matching query term counts.
    Prefix mode aggregates word variants into each query term before scoring.
    """
    terms = frozenset(term for term in query if term)
    if not terms or not documents:
        return [0.0] * len(documents)

    frequencies = []
    lengths = []
    for fields in documents:
        frequency: Counter[str] = Counter()
        length = 0
        for text, weight in fields:
            tokens = tokenize(text)
            length += len(tokens)
            frequency.update(
                {term: count * weight for term, count in Counter(tokens).items() if prefix or term in terms}
            )
        frequencies.append(frequency)
        lengths.append(length)

    if prefix:
        words = sorted({word for counts in frequencies for word in counts})
        matching_queries: dict[str, list[str]] = {}
        for term in sorted(terms):
            index = bisect_left(words, term)
            while index < len(words) and words[index].startswith(term):
                matching_queries.setdefault(words[index], []).append(term)
                index += 1
        expanded = []
        for counts in frequencies:
            frequency = Counter()
            for word, count in counts.items():
                for term in matching_queries.get(word, ()):
                    frequency[term] += count
            expanded.append(frequency)
        frequencies = expanded

    average_length = sum(lengths) / len(documents)
    if not average_length:
        return [0.0] * len(documents)
    document_frequencies = Counter(term for counts in frequencies for term in counts)
    inverse_frequencies = {
        term: math.log1p((len(documents) - count + 0.5) / (count + 0.5)) for term, count in document_frequencies.items()
    }
    k1, b = 1.2, 0.75
    return [
        sum(
            inverse_frequencies[term] * frequency * (k1 + 1) / (frequency + k1 * (1 - b + b * length / average_length))
            for term, frequency in counts.items()
        )
        for counts, length in zip(frequencies, lengths, strict=True)
    ]
