"""Slack Block Kit message formatting for governance alerts."""

from ..models import DetectionResult


def format_alert(result: DetectionResult) -> dict:
    """Build a Slack Block Kit message from a detection result.

    Creates a rich notification with:
    - Header with alert type
    - Forum name and linked topic title
    - Author, category, matched keywords, confidence score
    - Preview of the post body
    - Direct link button to the forum discussion
    """
    post = result.post

    # Group matched keywords by group name
    keyword_groups = {}
    for match in result.matches:
        if match.group not in keyword_groups:
            keyword_groups[match.group] = []
        keyword_groups[match.group].append(match.matched_text)

    keywords_text = ", ".join(
        f"*{group}*: {', '.join(set(words))}"
        for group, words in keyword_groups.items()
    )
    if not keywords_text:
        keywords_text = "N/A"

    # Truncate body for preview
    preview = post.body[:300]
    if len(post.body) > 300:
        preview += "..."

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "DAO Governance Alert",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{post.forum_name.upper()}*  |  <{post.url}|{post.title}>",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Author*\n{post.author}"},
                {"type": "mrkdwn", "text": f"*Category*\n{post.category or 'N/A'}"},
                {"type": "mrkdwn", "text": f"*Keywords*\n{keywords_text}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Confidence*\n{result.score:.1f}",
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Preview*\n{preview}",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "View Discussion",
                        "emoji": True,
                    },
                    "url": post.url,
                    "style": "primary",
                }
            ],
        },
    ]

    return {
        "text": f"DAO Alert: {post.title} ({post.forum_name})",  # fallback
        "blocks": blocks,
    }


def format_error_alert(forum_name: str, error: str) -> dict:
    """Format an error notification for monitoring failures."""
    return {
        "text": f"DAO Monitor Error: {forum_name}",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "DAO Monitor - Error",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Forum*: {forum_name}\n"
                        f"*Error*: {error[:500]}\n\n"
                        "The monitor will retry on the next cycle."
                    ),
                },
            },
        ],
    }
