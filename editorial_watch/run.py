from __future__ import annotations

import logging

from editorial_watch import main as editorial_main
from paper_watch.ai_summary import SummaryError


_ORIGINAL_SUMMARIZE = editorial_main.OpenAISummarizer.summarize


def _summarize_japanese_only(
    self: editorial_main.OpenAISummarizer,
    title: str,
    abstract: str,
    doi: str = "",
):
    """Retry summary generation, but never allow an English Slack post."""
    last_language = "unknown"
    for attempt in range(1, 4):
        result = _ORIGINAL_SUMMARIZE(self, title, abstract, doi)
        if result.language == "ja":
            return result
        last_language = result.language
        logging.warning(
            "Editorial summary was %s instead of Japanese for DOI=%s; retrying (%s/3).",
            result.language,
            doi,
            attempt,
        )

    raise SummaryError(
        f"Japanese summary was not generated after 3 attempts "
        f"(last language={last_language})."
    )


def main() -> None:
    editorial_main.OpenAISummarizer.summarize = _summarize_japanese_only
    editorial_main.main()


if __name__ == "__main__":
    main()
