from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from slack_sdk import WebClient

from .crossref_client import fetch_abstract
from .graphical_abstract import find_graphical_abstract
from .models import Paper
from .openalex_client import fetch_candidates
from .scoring import apply_score, score_paper
from .slack_client import build_blocks, post_batch
from .state import load_state, prune_state, save_state
from .translator import translate_abstract

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "data" / "state.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find relevant papers and post them to Slack.")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Slack or OpenAI; print selected papers.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    return parser.parse_args()


def require_env(name: str, *, dry_run: bool = False) -> str:
    value = os.getenv(name, "").strip()
    if not value and not dry_run:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def current_local_date(config: dict) -> str:
    return datetime.now(ZoneInfo(config["timezone"])).date().isoformat()


def merge_candidates(*groups: list[Paper]) -> list[Paper]:
    merged: dict[str, Paper] = {}
    for group in groups:
        for paper in group:
            existing = merged.get(paper.key)
            if existing is None or (not existing.abstract_original and paper.abstract_original):
                merged[paper.key] = paper
    return list(merged.values())


def eligible(paper: Paper, config: dict) -> bool:
    has_positive = bool(
        paper.matched_core_title
        or paper.matched_core_abstract
        or paper.matched_strong_title
        or paper.matched_strong_abstract
    )
    return has_positive and (
        paper.score >= int(config["runtime"]["post_threshold"])
        or bool(paper.matched_core_title)
    )


def sort_key(paper: Paper) -> tuple[int, str, str]:
    return paper.score, paper.publication_date, paper.title


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with args.config.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)

    openalex_key = require_env("OPENALEX_API_KEY", dry_run=args.dry_run)
    if args.dry_run and not openalex_key:
        raise RuntimeError("OPENALEX_API_KEY is still required for --dry-run because candidate retrieval is live.")

    state = load_state(args.state)
    prune_state(state)
    fetched = fetch_candidates(openalex_key, config)
    candidates = fetched

    scored: list[Paper] = []
    for paper in candidates:
        result = score_paper(paper, config)
        apply_score(paper, result)
        if paper.key not in state["posted"] and eligible(paper, config):
            scored.append(paper)

    scored.sort(key=sort_key, reverse=True)
    today = current_local_date(config)
    used_today = int(state["daily_counts"].get(today, 0))
    selected = scored
    daily_target = int(config["runtime"].get("daily_target", 30))
    projected_today = used_today + len(selected)
    if projected_today > daily_target:
        logging.info(
            "Daily target is informational only: target=%s projected=%s; posting all eligible papers.",
            daily_target,
            projected_today,
        )

    if args.dry_run:
        print(json.dumps([paper.to_dict() for paper in selected], ensure_ascii=False, indent=2))
        return

    contact_email = os.getenv("CONTACT_EMAIL", "").strip()
    enriched_selected: list[Paper] = []
    for paper in selected:
        if not paper.abstract_original:
            paper.abstract_original = fetch_abstract(paper.doi, contact_email)
            result = score_paper(paper, config)
            apply_score(paper, result)
        if not eligible(paper, config):
            logging.info("Dropped after abstract enrichment score=%s DOI=%s", paper.score, paper.doi)
            continue
        paper.abstract_ja = translate_abstract(paper.abstract_original, config)
        paper.graphical_abstract_url = find_graphical_abstract(paper.landing_page_url, config)
        enriched_selected.append(paper)
    selected = enriched_selected

    if selected:
        slack_token = require_env("SLACK_BOT_TOKEN")
        channel_id = require_env("SLACK_CHANNEL_ID")
        client = WebClient(token=slack_token)
        posted = post_batch(
            client,
            channel_id,
            selected,
            float(config["runtime"]["slack_pause_seconds"]),
        )
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        channel_id = os.environ["SLACK_CHANNEL_ID"]
        for paper, timestamp in posted:
            state["posted"][paper.key] = {
                "posted_at": now,
                "slack_ts": timestamp,
                "slack_channel_id": channel_id,
                "title": paper.title,
                "doi": paper.doi,
                "score": paper.score,
            }
            logging.info("Posted score=%s DOI=%s Slack ts=%s", paper.score, paper.doi, timestamp)
        state["daily_counts"][today] = used_today + len(posted)
    else:
        logging.info("No papers selected. used_today=%s", used_today)

    state["pending"] = []
    save_state(args.state, state)


if __name__ == "__main__":
    main()
