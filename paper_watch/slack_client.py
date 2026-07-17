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


def build_header_blocks(paper: Paper) -> list[dict[str, Any]]:
    """Build the text blocks displayed immediately before the image."""
    tag_line = " ".join(paper.tags) or "なし"

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
                    f"*Score: {paper.score} = keyword {paper.keyword_score} "
                    f"+ journal {paper.journal_score} "
                    f"- exclusion {paper.exclusion_penalty} "
                    f"| Journal tier: {paper.journal_tier}*"
                ),
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Matched keywords*\n{tag_line}",
            },
        },
    ]


def build_tail_blocks(paper: Paper) -> list[dict[str, Any]]:
    """Build the blocks displayed after the image."""
    blocks: list[dict[str, Any]] = [
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
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Abstract*",
            },
        },
    ]

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

    # The only divider is at the end, making paper boundaries obvious.
    blocks.append({"type": "divider"})

    if len(blocks) > 50:
        LOGGER.warning(
            "Block count %s exceeds Slack limit; trimming abstract blocks",
            len(blocks),
        )
        blocks = blocks[:49] + [{"type": "divider"}]

    return blocks


def _post_header(client: WebClient, channel_id: str, paper: Paper) -> str:
    response = client.chat_postMessage(
        channel=channel_id,
        text=f"{paper.title} — {paper.journal}",
        blocks=build_header_blocks(paper),
        unfurl_links=False,
        unfurl_media=False,
    )
    return str(response.get("ts") or "")


def upload_article_image(
    client: WebClient,
    channel_id: str,
    paper: Paper,
) -> str:
    """Upload and share the required image directly in the target channel."""
    if not paper.article_image_path:
        raise RuntimeError(f"No article image was prepared for {paper.doi}")

    path = Path(paper.article_image_path)
    if not path.is_file():
        raise RuntimeError(f"Article image file does not exist: {path}")

    try:
        response = client.files_upload_v2(
            channel=channel_id,
            file=str(path),
            filename=path.name,
            title=f"Article image — {paper.title}"[:255],
            alt_txt=f"Representative image for {paper.title}"[:2000],
        )
    except SlackApiError as exc:
        error = exc.response.get("error")
        raise RuntimeError(
            f"Slack image upload failed for {paper.doi}: {error}. "
            "Confirm that the Slack App has files:write, was reinstalled, "
            "and the bot is a member of the channel."
        ) from exc

    file_id = _extract_file_id(response)
    if not file_id:
        raise RuntimeError(
            f"Slack image upload returned no file ID for {paper.doi}"
        )
    return file_id


def _post_tail(client: WebClient, channel_id: str, paper: Paper) -> str:
    response = client.chat_postMessage(
        channel=channel_id,
        text=f"論文を開く: {paper.landing_page_url}",
        blocks=build_tail_blocks(paper),
        unfurl_links=False,
        unfurl_media=False,
    )
    return str(response.get("ts") or "")


def _cleanup_partial_post(
    client: WebClient,
    channel_id: str,
    header_ts: str,
    file_id: str,
) -> None:
    """Best-effort cleanup so a failed paper is not left half-posted."""
    if file_id:
        try:
            client.files_delete(file=file_id)
        except Exception as exc:  # cleanup must not mask the original failure
            LOGGER.warning("Could not delete partial Slack file %s: %s", file_id, exc)

    if header_ts:
        try:
            client.chat_delete(channel=channel_id, ts=header_ts)
        except Exception as exc:  # cleanup must not mask the original failure
            LOGGER.warning(
                "Could not delete partial Slack header %s: %s",
                header_ts,
                exc,
            )


def post_paper(client: WebClient, channel_id: str, paper: Paper) -> str:
    """Post header -> image file -> link/abstract/divider, in that order."""
    header_ts = ""
    file_id = ""

    try:
        header_ts = _post_header(client, channel_id, paper)
        file_id = upload_article_image(client, channel_id, paper)
        return _post_tail(client, channel_id, paper)
    except SlackApiError as exc:
        _cleanup_partial_post(client, channel_id, header_ts, file_id)
        error = exc.response.get("error")
        raise RuntimeError(
            f"Slack rejected the message sequence for {paper.doi}: {error}"
        ) from exc
    except Exception:
        _cleanup_partial_post(client, channel_id, header_ts, file_id)
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
