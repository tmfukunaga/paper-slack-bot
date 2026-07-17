from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from slack_sdk import WebClient

from .ai_summary import OpenAISummarizer, SummaryError
from .config_validation import validate_config
from .crossref_client import fetch_abstract
from .models import Paper
from .openalex_client import fetch_candidates
from .scoring import apply_score, score_paper
from .selection import (
    can_post_conditional,
    meets_posting_floor,
    preview_selection,
    split_candidates,
)
from .slack_client import post_paper
from .state import load_state, prune_state, save_state

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "data" / "state.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find relevant papers, summarize them in Japanese, and post to Slack."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call OpenAI or Slack; print selected papers before summarization.",
    )
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


def enrich_and_score_candidates(
    candidates: list[Paper],
    *,
    state: dict,
    config: dict,
    contact_email: str,
) -> list[Paper]:
    """Enrich missing abstracts, then perform the final score calculation."""
    minimum_keyword_score = int(config["posting"]["minimum_keyword_score"])
    scored: list[Paper] = []

    for paper in candidates:
        if paper.key in state["posted"]:
            continue

        result = score_paper(paper, config)
        apply_score(paper, result, config)

        # A missing OpenAlex abstract can hide positive or exclusion matches.
        # Only enrich papers that already have some relevant keyword evidence.
        if not paper.abstract_original.strip() and paper.keyword_score >= minimum_keyword_score:
            paper.abstract_original = fetch_abstract(paper.doi, contact_email)
            result = score_paper(paper, config)
            apply_score(paper, result, config)

        if meets_posting_floor(paper, config):
            scored.append(paper)
        else:
            logging.info(
                "Rejected score=%s keyword=%s journal=%s exclusion=%s DOI=%s",
                paper.score,
                paper.keyword_score,
                paper.journal_score,
                paper.exclusion_penalty,
                paper.doi,
            )

    return scored


def post_one(
    paper: Paper,
    *,
    summarizer: OpenAISummarizer,
    slack: WebClient,
    channel_id: str,
    state: dict,
    state_path: Path,
    today: str,
    used_today: int,
    posted_count: int,
) -> bool:
    """Summarize and post one paper; return True only after a successful post."""
    if not paper.abstract_original.strip():
        logging.warning(
            "Skipping DOI=%s because no Abstract was available; it will be retried later.",
            paper.doi,
        )
        return False

    try:
        summary = summarizer.summarize(
            paper.title,
            paper.abstract_original,
            paper.doi,
        )
    except SummaryError as exc:
        logging.error(
            "Summary failed completely for DOI=%s; paper was not posted and will be retried: %s",
            paper.doi,
            exc,
        )
        return False

    paper.summary_japanese = summary.text
    paper.summary_model = summary.model
    paper.summary_character_count = summary.character_count
    paper.summary_language = summary.language
    paper.summary_used_fallback = summary.used_fallback

    timestamp = post_paper(slack, channel_id, paper)

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    state["posted"][paper.key] = {
        "posted_at": now,
        "slack_ts": timestamp,
        "slack_channel_id": channel_id,
        "title": paper.title,
        "doi": paper.doi,
        "score": paper.score,
        "summary_model": paper.summary_model,
        "summary_character_count": paper.summary_character_count,
        "summary_japanese": paper.summary_japanese,
        "summary_language": paper.summary_language,
        "summary_used_fallback": paper.summary_used_fallback,
    }
    state["daily_counts"][today] = used_today + posted_count + 1
    state["pending"] = []

    # Save after every success so a later failure cannot cause duplicate posts.
    save_state(state_path, state)

    logging.info(
        "Posted score=%s DOI=%s Slack ts=%s summary_chars=%s model=%s language=%s fallback=%s",
        paper.score,
        paper.doi,
        timestamp,
        paper.summary_character_count,
        paper.summary_model,
        paper.summary_language,
        paper.summary_used_fallback,
    )
    return True


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with args.config.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    validate_config(config)

    openalex_key = require_env("OPENALEX_API_KEY", dry_run=args.dry_run)
    if args.dry_run and not openalex_key:
        raise RuntimeError(
            "OPENALEX_API_KEY is required for --dry-run because retrieval is live."
        )

    contact_email = os.getenv("CONTACT_EMAIL", "").strip()
    state = load_state(args.state)
    prune_state(state)
    candidates = fetch_candidates(openalex_key, config)
    scored = enrich_and_score_candidates(
        candidates,
        state=state,
        config=config,
        contact_email=contact_email,
    )
    guaranteed, conditional = split_candidates(scored, config)

    today = current_local_date(config)
    used_today = int(state["daily_counts"].get(today, 0))

    logging.info(
        "Selection pool: guaranteed=%s conditional=%s used_today=%s",
        len(guaranteed),
        len(conditional),
        used_today,
    )

    if args.dry_run:
        selected = preview_selection(
            guaranteed,
            conditional,
            successful_before_run_today=used_today,
            config=config,
        )
        print(
            json.dumps(
                [paper.to_dict() for paper in selected],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    openai_key = require_env("OPENAI_API_KEY")
    slack_token = require_env("SLACK_BOT_TOKEN")
    channel_id = require_env("SLACK_CHANNEL_ID")

    summarizer = OpenAISummarizer(openai_key, config)
    slack = WebClient(token=slack_token)
    pause_seconds = float(config["runtime"]["slack_pause_seconds"])
    posted_count = 0

    # Score >= guaranteed_score is never suppressed by the soft run/day targets.
    for paper in guaranteed:
        if post_one(
            paper,
            summarizer=summarizer,
            slack=slack,
            channel_id=channel_id,
            state=state,
            state_path=args.state,
            today=today,
            used_today=used_today,
            posted_count=posted_count,
        ):
            posted_count += 1
            time.sleep(pause_seconds)

    # Lower-score papers fill the run only while both soft targets remain.
    for paper in conditional:
        if not can_post_conditional(
            successful_this_run=posted_count,
            successful_before_run_today=used_today,
            config=config,
        ):
            break

        if post_one(
            paper,
            summarizer=summarizer,
            slack=slack,
            channel_id=channel_id,
            state=state,
            state_path=args.state,
            today=today,
            used_today=used_today,
            posted_count=posted_count,
        ):
            posted_count += 1
            time.sleep(pause_seconds)

    if posted_count == 0:
        logging.info("No papers posted. used_today=%s", used_today)

    state["pending"] = []
    save_state(args.state, state)


if __name__ == "__main__":
    main()
