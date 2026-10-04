from __future__ import annotations

import pytest

from nyanpasu.memory_search import bm25_scores, search_excerpt, tokenize, topic_terms


def test_tokenization_normalizes_unicode_and_preserves_code_identifiers():
    assert tokenize("Ｐｙｔｈｏｎ python CP 285 scale_down，扩容超时") == (
        "python",
        "python",
        "cp",
        "285",
        "scale_down",
        "scale",
        "down",
        "扩容",
        "容超",
        "超时",
    )
    assert tokenize("检查CP与scale_down扩容") == ("检查", "cp", "与", "scale_down", "scale", "down", "扩容")
    assert tokenize("Café CAFE\u0301 __init__ __") == ("café", "café", "__init__", "init")


def test_automatic_topics_filter_generic_words_without_filtering_explicit_search():
    text = "Review docs feat current code zero token CP DP 285"
    assert topic_terms(text) == {"zero", "token", "cp", "dp", "285"}
    assert {"review", "docs", "feat"} <= set(tokenize(text))


@pytest.mark.parametrize(
    "query,body,extension",
    [
        ("src/runtime/checkpoint.py", "Dashboard typography in web.py should use consistent spacing.", "py"),
        ("docs/en/runtime.md", "Chinese translation lives in docs/zh/dashboard.md", "md"),
    ],
)
def test_shared_file_extensions_and_path_scaffolding_do_not_make_topics_relevant(query, body, extension):
    assert not topic_terms(query).intersection(topic_terms(body))
    assert "runtime" in topic_terms(query)
    assert extension in tokenize(query)
    assert bm25_scores({extension}, [[(body, 1.0)]])[0] > 0


def test_rare_terms_rank_ahead_of_common_terms_and_partial_matches_remain_available():
    documents = [
        [("routing checkpoint", 1.0)],
        [("routing display", 1.0)],
        [("routing button", 1.0)],
        [("visual styling", 1.0)],
    ]
    scores = bm25_scores(tokenize("routing checkpoint"), documents)
    assert scores[0] > scores[1] == scores[2] > scores[3] == 0
    assert bm25_scores(tokenize("checkpoint"), documents)[0] > bm25_scores(tokenize("routing"), documents)[0]
    assert bm25_scores(tokenize("routing routing checkpoint"), documents) == scores


def test_term_frequency_saturates_and_long_unrelated_text_lowers_relevance():
    scores = bm25_scores({"checkpoint"}, [[("checkpoint " * count, 1.0)] for count in (1, 2, 20)])
    assert 0 < scores[0] < scores[1] < scores[2] < scores[0] * 3
    focused, padded = bm25_scores(
        {"checkpoint"},
        [[("checkpoint", 1.0)], [("checkpoint " + "unrelated " * 100, 1.0)]],
    )
    assert focused > padded > 0


def test_field_weights_change_relevance_without_changing_document_length():
    scores = bm25_scores(
        {"python"},
        [[("Python", 4.0), ("workflow", 1.0)], [("workflow", 4.0), ("Python", 1.0)]],
    )
    assert scores[0] > scores[1] > 0


def test_word_boundaries_and_chinese_pairs_match_without_substrings():
    assert bm25_scores({"python"}, [[("CPython", 1.0)]]) == [0.0]
    chinese, identifier, unrelated = bm25_scores(
        tokenize("扩容超时 scale down"),
        [[("扩容流程中排查超时", 1.0)], [("inspect scale_down", 1.0)], [("dashboard", 1.0)]],
    )
    assert chinese > 0 and identifier > 0 and unrelated == 0


def test_prefix_search_matches_word_variants_and_ranks_fields_without_internal_substrings():
    documents = [
        [("Fixtures", 4.0), ("workflow", 1.0)],
        [("workflow", 4.0), ("Fixtures", 1.0)],
        [("prefixtures only", 1.0)],
    ]
    assert bm25_scores({"fixture"}, documents) == [0.0, 0.0, 0.0]
    title, body, unrelated = bm25_scores({"fixture"}, documents, prefix=True)
    assert title > body > unrelated == 0
    assert bm25_scores({"python"}, [[("CPython", 1.0)]], prefix=True) == [0.0]
    variants = [[("fixture fixture", 1.0)], [("fixture fixtures", 1.0)], [("fixtures fixtures", 1.0)]]
    assert len(set(bm25_scores({"fixture"}, variants, prefix=True))) == 1
    assert bm25_scores({"", "fixture"}, documents, prefix=True) == [title, body, unrelated]
    text = "Earlier unrelated details. " * 100 + "Fixtures restore the worker state. " * 5
    assert "Fixtures restore the worker state." in search_excerpt(text, "fixture")


def test_search_excerpt_finds_late_evidence_and_falls_back_to_the_beginning():
    text = "Earlier unrelated details. " * 100 + "Checkpoint routing verifies the receipt. " * 5
    excerpt = search_excerpt(text, "checkpoint routing")
    assert "Checkpoint routing verifies the receipt." in excerpt
    assert len(excerpt) <= 800
    assert search_excerpt(text, "missing") == text[:800]
    assert search_excerpt(text, "") == text[:800]
    assert search_excerpt("Short evidence", "evidence") == "Short evidence"


@pytest.mark.parametrize(
    "query,documents,expected",
    [
        ((), [[("python", 1.0)]], [0.0]),
        (("python",), [], []),
        (("python",), [[("", 1.0)], []], [0.0, 0.0]),
    ],
)
def test_empty_queries_and_documents_have_zero_scores(query, documents, expected):
    assert bm25_scores(query, documents) == expected
