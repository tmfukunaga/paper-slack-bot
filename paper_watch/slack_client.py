from __future__ import annotations

import time
from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .models import Paper
from .text_utils import escape_slack_mrkdwn


def _author_line(authors: list[str]) -> str:
    if not authors:
        return "著者情報なし"
    if len(authors) <= 8:
        return escape_slack_mrkdwn(", ".join(authors))
    return escape_slack_mrkdwn(", ".join(authors[:8]) + ", et al.")


def build_paper_blocks(paper: Paper) -> list[dict[str, Any]]:
    tag_line = " ".join(paper.tags) or "なし"
    summary = paper.summary_japanese or "Summary was unavailable."
    summary_label = "要約" if paper.summary_language == "ja" else "Summary (English fallback)"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{escape_slack_mrkdwn(paper.title)}*\n"
                    f"{_author_line(paper.authors)}\n"
                    f"{escape_slack_mrkdwn(paper.journal or '雑誌名不明')} | "
                    f"{escape_slack_mrkdwn(paper.publication_date or '公開日不明')}\n"
                    f"*Score: {paper.score}, Matched keywords: {tag_line}*"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{summary_label}*\n{escape_slack_mrkdwn(summary)}",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "論文を開く"},
                    "url": paper.landing_page_url,
                    "action_id": "open_paper",
                }
            ],
        },
        {"type": "divider"},
    ]


def post_paper(client: WebClient, channel_id: str, paper: Paper) -> str:
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text=f"{paper.title} — {paper.summary_japanese}",
            blocks=build_paper_blocks(paper),
            unfurl_links=False,
            unfurl_media=False,
        )
    except SlackApiError as exc:
        error = exc.response.get("error")
        raise RuntimeError(
            f"Slack rejected the paper message for {paper.doi}: {error}"
        ) from exc
    return str(response.get("ts") or "")


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
