from paper_watch.state import load_state


def test_legacy_pending_queue_is_discarded(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        '{"posted": {}, "daily_counts": {}, "pending": [{"doi": "10.0/old"}]}',
        encoding="utf-8",
    )
    state = load_state(path)
    assert "pending" not in state
