from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from slack_sdk import WebClient

from .article_image import ArticleImageFetcher
from .config_validation import validate_config
from .crossref_client import fetch_abstract
from .models import Paper
from .openalex_client import fetch_candidates
from .scoring import apply_score, score_paper
from .slack_client import post_batch
from .state import load_state, prune_state, save_state

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "data" / "state.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find relevant papers and post them to Slack."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call Slack; print selected papers.",
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


def eligible(paper: Paper, config: dict) -> bool:
    posting = config["posting"]
    return (
        paper.score >= int(posting["minimum_total_score"])
        and paper.keyword_score >= int(posting["minimum_keyword_score"])
    )


def sort_key(paper: Paper) -> tuple[int, str, str]:
    return paper.score, paper.publication_date, paper.title


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

    state = load_state(args.state)
    prune_state(state)

    candidates = fetch_candidates(openalex_key, config)

    scored: list[Paper] = []
    for paper in candidates:
        result = score_paper(paper, config)
        apply_score(paper, result, config)

        if paper.key in state["posted"]:
            continue

        if eligible(paper, config):
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

    scored.sort(key=sort_key, reverse=True)

    today = current_local_date(config)
    used_today = int(state["daily_counts"].get(today, 0))
    daily_target = int(config["runtime"].get("daily_target", 30))
    projected_today = used_today + len(scored)

    if projected_today > daily_target:
        logging.info(
            "Daily target is informational only: target=%s projected=%s; "
            "posting all eligible papers.",
            daily_target,
            projected_today,
        )

    if args.dry_run:
        print(
            json.dumps(
                [paper.to_dict() for paper in scored],
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    contact_email = os.getenv("CONTACT_EMAIL", "").strip()
    selected: list[Paper] = []

    for paper in scored:
        if not paper.abstract_original:
            paper.abstract_original = fetch_abstract(paper.doi, contact_email)

            # The abstract can add both positive and exclusion matches.
            result = score_paper(paper, config)
            apply_score(paper, result, config)

        if not eligible(paper, config):
            logging.info(
                "Dropped after abstract enrichment score=%s DOI=%s",
                paper.score,
                paper.doi,
            )
            continue

        selected.append(paper)

    if selected:
        slack_token = require_env("SLACK_BOT_TOKEN")
        channel_id = require_env("SLACK_CHANNEL_ID")
        client = WebClient(token=slack_token)

        # Image paths are temporary, so retrieval and Slack upload occur in
        # the same context.
        with ArticleImageFetcher(config) as image_fetcher:
            for paper in selected:
                image = image_fetcher.fetch(
                    paper.landing_page_url,
                    paper.doi,
                    paper.key,
                    title=paper.title,
                    journal=paper.journal,
                    publication_date=paper.publication_date,
                )
                paper.article_image_path = str(image.path)
                paper.article_image_method = image.method

                if not Path(paper.article_image_path).is_file():
                    raise RuntimeError(
                        f"Required article image was not created for {paper.doi}"
                    )

            posted = post_batch(
                client,
                channel_id,
                selected,
                float(config["runtime"]["slack_pause_seconds"]),
            )

        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        for paper, timestamp in posted:
            state["posted"][paper.key] = {
                "posted_at": now,
                "slack_ts": timestamp,
                "slack_channel_id": channel_id,
                "title": paper.title,
                "doi": paper.doi,
                "score": paper.score,
                "article_image_method": paper.article_image_method,
            }
            logging.info(
                "Posted score=%s DOI=%s Slack ts=%s image=%s",
                paper.score,
                paper.doi,
                timestamp,
                paper.article_image_method or "none",
            )

        state["daily_counts"][today] = used_today + len(posted)
    else:
        logging.info("No papers selected. used_today=%s", used_today)

    state["pending"] = []
    save_state(args.state, state)


if __name__ == "__main__":
    main()
