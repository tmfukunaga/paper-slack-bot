from trending_watch.main import (
    Candidate,
    _in_scope,
    _plumx_count_types,
    build_blocks,
    plumx_attention_metrics,
    rank_candidates,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.text = ""

    def json(self):
        return self.payload


def scope_config():
    return {
        "scope": {
            "dedicated_journals": ["Organic Letters"],
            "broad_journals": ["Journal of the American Chemical Society"],
            "title_keywords": ["catalytic", "total synthesis"],
            "excluded_title_keywords": ["correction"],
        }
    }


def test_dedicated_journal_is_in_scope_without_title_keyword():
    assert _in_scope("A New Transformation", "Organic Letters", scope_config())


def test_broad_journal_requires_organic_synthesis_title_keyword():
    config = scope_config()
    assert _in_scope(
        "Catalytic Enantioselective Transformation",
        "Journal of the American Chemical Society",
        config,
    )
    assert not _in_scope(
        "Battery Electrolyte Interfaces",
        "Journal of the American Chemical Society",
        config,
    )


def plumx_payload():
    return {
        "count_categories": [
            {"name": "capture", "total": 9, "count_types": [
                {"name": "READER_COUNT", "total": 7},
                {"name": "EXPORTS_SAVES", "total": 2},
            ]},
            {"name": "mention", "total": 8, "count_types": [
                {"name": "NEWS_COUNT", "total": 3},
                {"name": "ALL_BLOG_COUNT", "total": 2},
                {"name": "REFERENCE_COUNT", "total": 3},
            ]},
            {"name": "socialMedia", "total": 10, "count_types": [
                {"name": "TWEET_COUNT", "total": 6},
                {"name": "FACEBOOK_COUNT", "total": 4},
            ]},
            {"name": "citation", "total": 9999, "count_types": [
                {"name": "CITED_BY_COUNT", "total": 9999},
            ]},
            {"name": "usage", "total": 8888, "count_types": [
                {"name": "DOWNLOAD_COUNT", "total": 8888},
            ]},
        ]
    }


def test_plumx_parser_uses_official_total_fields():
    counts = _plumx_count_types(plumx_payload())
    assert counts["READER_COUNT"] == 7
    assert counts["NEWS_COUNT"] == 3
    assert counts["TWEET_COUNT"] == 6


def test_plumx_attention_excludes_citations_usage_and_other_captures(monkeypatch):
    monkeypatch.setattr(
        "trending_watch.main.elsevier_get",
        lambda *args, **kwargs: plumx_payload(),
    )
    assert plumx_attention_metrics("api-key", "10.1/example") == (7, 5, 10)


def test_rank_candidates_gives_readers_mentions_and_social_equal_weight():
    candidates = [
        Candidate("10.1/a", "A", [], "Organic Letters", "2026-07-18", "", reader_count=10),
        Candidate("10.1/b", "B", [], "Organic Letters", "2026-07-19", "", mention_count=1),
        Candidate("10.1/c", "C", [], "Organic Letters", "2026-07-19", "", social_count=1000),
        Candidate("10.1/d", "D", [], "Organic Letters", "2026-07-17", ""),
    ]
    ranked = rank_candidates(candidates, minimum_total_attention=1)
    assert {item.attention_score for item in ranked} == {1 / 3}
    assert [item.doi for item in ranked] == ["10.1/c", "10.1/a", "10.1/b"]
    assert "10.1/d" not in [item.doi for item in ranked]


def test_blocks_prefix_title_with_hot_rank():
    candidate = Candidate("10.1/a", "Title", [], "Organic Letters", "2026-07-19", "")
    blocks = build_blocks(candidate, "要約です。", rank=2)
    assert "[Hot-2] Title" in blocks[0]["text"]["text"]
