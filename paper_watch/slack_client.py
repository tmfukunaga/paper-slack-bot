from __future__ import annotations

import logging
import time

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .models import Paper
from .text_utils import escape_slack_mrkdwn, split_for_slack

LOGGER = logging.getLogger(__name__)


def _author_line(authors: list[str]) -> str:
    if not authors:
        return "著者情報なし"
    if len(authors) <= 8:
        return escape_slack_mrkdwn(", ".join(authors))
    return escape_slack_mrkdwn(", ".join(authors[:8]) + ", et al.")


def build_blocks(paper: Paper) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{escape_slack_mrkdwn(paper.title)}*\n"
                    f"{_author_line(paper.authors)}\n"
                    f"{escape_slack_mrkdwn(paper.journal or '雑誌名不明')} | "
                    f"{escape_slack_mrkdwn(paper.publication_date or '公開日不明')}\n"
                    f"*Score: {paper.score} = keyword {paper.keyword_score} "
                    f"+ journal {paper.journal_score} "
                    f"- exclusion {paper.exclusion_penalty} "
                    f"| Journal tier: {paper.journal_tier}*"
                ),
            },
        }
    ]

    if paper.graphical_abstract_url:
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "image",
                    "image_url": paper.graphical_abstract_url,
                    "alt_text": f"Graphical abstract for {paper.title}"[:2000],
                    "title": {
                        "type": "plain_text",
                        "text": "Graphical Abstract",
                    },
                },
            ]
        )

    blocks.extend(
        [
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Abstract original*",
                },
            },
        ]
    )

    original = paper.abstract_original or "取得できませんでした。"
    for chunk in split_for_slack(original):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": escape_slack_mrkdwn(chunk),
                },
            }
        )

    tag_line = " ".join(paper.tags) or "なし"
    blocks.extend(
        [
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Matched keywords*\n{tag_line}",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "論文を開く",
                        },
                        "url": paper.landing_page_url,
                        "action_id": "open_paper",
                    }
                ],
            },
        ]
    )

    if len(blocks) > 50:
        LOGGER.warning(
            "Block count %s exceeds Slack limit; trimming abstract blocks",
            len(blocks),
        )
        blocks = blocks[:47] + blocks[-3:]

    return blocks


def post_paper(client: WebClient, channel_id: str, paper: Paper) -> str:
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text=f"{paper.title} — {paper.journal}",
            blocks=build_blocks(paper),
            unfurl_links=False,
            unfurl_media=False,
        )
        return response.get("ts", "")
    except SlackApiError as exc:
        LOGGER.error("Slack post failed: %s", exc.response.get("error"))
        raise


def post_batch(
    client: WebClient,
    channel_id: str,
    papers: list[Paper],
    pause_seconds: float,
) -> list[tuple[Paper, str]]:
    posted: list[tuple[Paper, str]] = []

    for index, paper in enumerate(papers):
        timestamp = post_paper(client, channel_id, paper)
        posted.append((paper, timestamp))

        if index + 1 < len(papers):
            time.sleep(pause_seconds)

    return posted
