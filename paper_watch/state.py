from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .models import Paper


DEFAULT_STATE = {"posted": {}, "daily_counts": {}, "pending": []}


def load_state(path: Path) -> dict:
    if not path.exists():
        return DEFAULT_STATE.copy()
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    for key, default in DEFAULT_STATE.items():
        data.setdefault(key, default.copy() if isinstance(default, dict) else list(default))
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def prune_state(state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=180)
    posted = {}
    for key, record in state.get("posted", {}).items():
        timestamp = record.get("posted_at", "") if isinstance(record, dict) else str(record)
        try:
            when = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= cutoff:
            posted[key] = record
    state["posted"] = posted

    daily_cutoff = date.today() - timedelta(days=31)
    state["daily_counts"] = {
        day: count
        for day, count in state.get("daily_counts", {}).items()
        if _safe_date(day) and _safe_date(day) >= daily_cutoff
    }


def _safe_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def pending_papers(state: dict) -> list[Paper]:
    papers = []
    for item in state.get("pending", []):
        try:
            papers.append(Paper.from_dict(item))
        except TypeError:
            continue
    return papers
