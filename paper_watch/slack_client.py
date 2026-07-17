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


def _paper_url(paper: Paper) -> str:
    """
    Return a fully visible HTTPS URL.

    A visible URL is used instead of a Block Kit URL button so that Slack
    does not treat the link as an interactive app action.
    """
    url = (paper.landing_page_url or "").strip()

    if url.startswith("https://") or url.startswith("http://"):
        return url

    doi = (paper.doi or "").strip()

    if doi:
        return f"https://doi.org/{doi}"

    return ""


def build_paper_blocks(paper: Paper) -> list[dict[str, Any]]:
    tag_line = " ".join(paper.tags) or "なし"
    summary = paper.summary_japanese or "Summary was unavailable."

    summary_label = (
        "要約"
        if paper.summary_language == "ja"
        else "Summary (English fallback)"
    )

    paper_url = _paper_url(paper)

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{escape_slack_mrkdwn(paper.title)}*\n"
                    f"{_author_line(paper.authors)}\n"
                    f"{escape_slack_mrkdwn(paper.journal or '雑誌名不明')} | "
                    f"{escape_slack_mrkdwn(paper.publication_date or '公開日不明')}\n"
                    f"*Score: {paper.score}, "
                    f"Matched keywords: {tag_line}*"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{summary_label}*\n"
                    f"{escape_slack_mrkdwn(summary)}"
                ),
            },
        },
    ]

    if paper_url:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    # URLをリンク文字列で隠さず、そのまま表示する。
                    "text": f"*論文リンク*\n{paper_url}",
                },
            }
        )

    blocks.append({"type": "divider"})

    return blocks


def post_paper(
    client: WebClient,
    channel_id: str,
    paper: Paper,
) -> str:
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            text=(
                f"{paper.title} — "
                f"{paper.summary_japanese or 'Summary was unavailable.'}"
            ),
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
        timestamp = post_paper(
            client=client,
            channel_id=channel_id,
            paper=paper,
        )

        posted.append((paper, timestamp))

        if index + 1 < len(papers):
            time.sleep(pause_seconds)

    return posted
