from datetime import datetime, timedelta, timezone

from paper_watch.models import Paper
from paper_watch.state import retain_recent_pending


def make_paper(updated_date: str) -> Paper:
    return Paper(
        openalex_id="W1",
        doi="10.0000/test",
        title="Test paper",
        authors=["A. Author"],
        journal="Chemical Science",
        publication_date="2026-07-18",
        updated_date=updated_date,
        abstract_original="An abstract.",
        landing_page_url="https://doi.org/10.0000/test",
    )


def test_pending_candidates_are_retained_for_seven_days():
    now = datetime.now(timezone.utc)
    recent = make_paper((now - timedelta(days=6)).isoformat())
    stale = make_paper((now - timedelta(days=8)).isoformat())
    assert retain_recent_pending([recent, stale], 7) == [recent]
