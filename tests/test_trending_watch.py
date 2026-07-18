from trending_watch.main import Candidate, _walk_metrics, score_candidate


def test_walk_metrics_collects_categories():
    payload = {
        "categories": [
            {"name": "Mentions", "metrics": [{"name": "News", "count": 3}]},
            {"name": "Social Media", "metrics": [{"name": "Reddit", "count": 4}]},
            {"name": "Captures", "metrics": [{"name": "Readers", "count": 7}]},
        ]
    }
    metrics = _walk_metrics(payload)
    assert metrics["mentions"] == 3
    assert metrics["socialMedia"] == 4
    assert metrics["captures"] == 7


def test_score_uses_daily_delta_when_baseline_exists():
    candidate = Candidate("10.1/test", "Title", [], "Journal", "2099-01-01", "")
    candidate.metrics = {"mentions": 5, "socialMedia": 4, "captures": 9, "usage": 0, "citations": 0}
    previous = {"snapshots": {"10.1/test": {"metrics": {"mentions": 4, "socialMedia": 1, "captures": 9, "usage": 0, "citations": 0}}}}
    config = {"ranking": {"weights": {"mentions": 5, "socialMedia": 4, "captures": 2, "usage": 1, "citations": 1}, "daily_age_penalty": 0}}
    score_candidate(candidate, previous, config)
    assert candidate.deltas["mentions"] == 1
    assert candidate.deltas["socialMedia"] == 3
    assert candidate.deltas["captures"] == 0
    assert candidate.score > 0

