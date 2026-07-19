from trending_watch.main import (
    Candidate,
    _in_scope,
    build_blocks,
    mendeley_access_token,
    mendeley_reader_count,
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


def test_mendeley_access_token_uses_client_credentials(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return FakeResponse({"access_token": "token-value"})

    monkeypatch.setattr("trending_watch.main.requests.post", fake_post)
    assert mendeley_access_token("123", "secret") == "token-value"
    assert captured["auth"] == ("123", "secret")
    assert captured["data"]["grant_type"] == "client_credentials"


def test_mendeley_reader_count_returns_highest_catalog_match(monkeypatch):
    monkeypatch.setattr(
        "trending_watch.main.requests.get",
        lambda *args, **kwargs: FakeResponse(
            [{"reader_count": 4}, {"reader_count": 7}]
        ),
    )
    assert mendeley_reader_count("token", "10.1/example") == 7


def test_rank_candidates_uses_reader_count_then_newer_date():
    candidates = [
        Candidate("10.1/a", "A", [], "Organic Letters", "2026-07-18", "", reader_count=3),
        Candidate("10.1/b", "B", [], "Organic Letters", "2026-07-19", "", reader_count=3),
        Candidate("10.1/c", "C", [], "Organic Letters", "2026-07-19", "", reader_count=0),
        Candidate("10.1/d", "D", [], "Organic Letters", "2026-07-17", "", reader_count=8),
    ]
    ranked = rank_candidates(candidates, minimum_reader_count=1)
    assert [item.doi for item in ranked] == ["10.1/d", "10.1/b", "10.1/a"]


def test_blocks_prefix_title_with_hot_rank():
    candidate = Candidate("10.1/a", "Title", [], "Organic Letters", "2026-07-19", "")
    blocks = build_blocks(candidate, "要約です。", rank=2)
    assert "[Hot-2] Title" in blocks[0]["text"]["text"]
