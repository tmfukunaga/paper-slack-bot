from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

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


def _extract_file_id(response: Any) -> str:
    files = response.get("files") or []
    if files and isinstance(files[0], dict):
        return str(files[0].get("id") or "")
    file_data = response.get("file") or {}
    if isinstance(file_data, dict):
        return str(file_data.get("id") or "")
    return ""


def upload_article_image(client: WebClient, paper: Paper) -> str:
    """Upload the required article image and return its Slack file ID."""
    if not paper.article_image_path:
        raise RuntimeError(f"No article image was prepared for {paper.doi}")

    path = Path(paper.article_image_path)
    if not path.is_file():
        raise RuntimeError(f"Article image file does not exist: {path}")

    try:
        response = client.files_upload_v2(
            file=str(path),
            filename=path.name,
            title=f"Article image — {paper.title}"[:255],
            alt_txt=f"Representative image for {paper.title}"[:2000],
        )
    except SlackApiError as exc:
        error = exc.response.get("error")
        raise RuntimeError(
            f"Slack image upload failed for {paper.doi}: {error}. "
            "Confirm that the Slack App has files:write and was reinstalled."
        ) from exc

    file_id = _extract_file_id(response)
    if not file_id:
        raise RuntimeError(
            f"Slack image upload returned no file ID for {paper.doi}"
        )
    return file_id


def build_blocks(paper: Paper, slack_file_id: str) -> list[dict[str, Any]]:
    if not slack_file_id:
        raise ValueError("slack_file_id is required: every paper must have an image")

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
                    f"*Score: {paper.score} = keyword {paper.keyword_score} "
                    f"+ journal {paper.journal_score} "
                    f"- exclusion {paper.exclusion_penalty} "
                    f"| Journal tier: {paper.journal_tier}*"
                ),
            },
        }
    ]

    # Requested order: keywords -> image -> paper link -> abstract.
    tag_line = " ".join(paper.tags) or "なし"
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Matched keywords*\n{tag_line}",
            },
        }
    )

    blocks.append(
        {
            "type": "image",
            "slack_file": {"id": slack_file_id},
            "alt_text": f"Representative image for {paper.title}"[:2000],
        }
    )

    blocks.append(
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
        }
    )

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Abstract*",
            },
        }
    )

    abstract = paper.abstract_original or "取得できませんでした。"
    for chunk in split_for_slack(abstract):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": escape_slack_mrkdwn(chunk),
                },
            }
        )

    # The only divider is at the end, making the boundary between papers clear.
    blocks.append({"type": "divider"})

    if len(blocks) > 50:
        LOGGER.warning(
            "Block count %s exceeds Slack limit; trimming abstract blocks",
            len(blocks),
        )
        blocks = blocks[:49] + [{"type": "divider"}]

    return blocks


def _post_blocks(
    client: WebClient,
    channel_id: str,
    paper: Paper,
    slack_file_id: str,
) -> str:
    response = client.chat_postMessage(
        channel=channel_id,
        text=f"{paper.title} — {paper.journal}",
        blocks=build_blocks(paper, slack_file_id),
        unfurl_links=False,
        unfurl_media=False,
    )
    return response.get("ts", "")


def post_paper(client: WebClient, channel_id: str, paper: Paper) -> str:
    """Post a paper only after its required image is uploaded successfully."""
    slack_file_id = upload_article_image(client, paper)
    try:
        return _post_blocks(client, channel_id, paper, slack_file_id)
    except SlackApiError as exc:
        error = exc.response.get("error")
        raise RuntimeError(
            f"Slack rejected the image-bearing message for {paper.doi}: {error}"
        ) from exc


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
