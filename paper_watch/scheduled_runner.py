from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "state.json"
RUN_STATUS_PATH = ROOT / "data" / "run_status.json"
TIMEZONE = ZoneInfo("Asia/Tokyo")


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default
    return value if isinstance(value, dict) else default


def _save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _slot_start(now: datetime) -> datetime:
    local = now.astimezone(TIMEZONE)
    return local.replace(hour=(local.hour // 3) * 3, minute=0, second=0, microsecond=0)


def _posted_keys() -> set[str]:
    state = _load_json(STATE_PATH, {"posted": {}})
    posted = state.get("posted", {})
    return set(posted) if isinstance(posted, dict) else set()


def _prune_slots(slots: dict, now: datetime) -> None:
    cutoff = now.astimezone(TIMEZONE) - timedelta(days=14)
    for key in list(slots):
        try:
            stamp = datetime.fromisoformat(key)
        except ValueError:
            slots.pop(key, None)
            continue
        if stamp < cutoff:
            slots.pop(key, None)


def main() -> int:
    started_at = datetime.now(timezone.utc).replace(microsecond=0)
    slot = _slot_start(started_at)
    slot_key = slot.isoformat()

    status = _load_json(RUN_STATUS_PATH, {"slots": {}})
    slots = status.setdefault("slots", {})
    if not isinstance(slots, dict):
        slots = {}
        status["slots"] = slots

    previous = slots.get(slot_key, {})
    if isinstance(previous, dict) and previous.get("status") == "success":
        print(f"Paper Watch slot {slot_key} already completed successfully; skipping duplicate trigger.")
        return 0

    before = _posted_keys()
    slots[slot_key] = {
        "status": "running",
        "scheduled_slot": slot_key,
        "started_at": started_at.isoformat(),
    }
    _prune_slots(slots, started_at)
    _save_json(RUN_STATUS_PATH, status)

    completed = subprocess.run(
        [sys.executable, "-m", "paper_watch.main"],
        cwd=ROOT,
        check=False,
    )

    finished_at = datetime.now(timezone.utc).replace(microsecond=0)
    after = _posted_keys()
    posted_count = len(after - before)
    slots[slot_key] = {
        "status": "success" if completed.returncode == 0 else "failed",
        "scheduled_slot": slot_key,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "posted_count": posted_count,
        "return_code": completed.returncode,
    }
    status["latest_slot"] = slot_key
    status["latest_status"] = slots[slot_key]
    _prune_slots(slots, finished_at)
    _save_json(RUN_STATUS_PATH, status)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
