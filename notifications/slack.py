"""Slack webhook integration for sending governance alerts."""

import json

import aiohttp

from ..models import DetectionResult, SlackConfig
from ..utils.logger import get_logger
from .formatter import format_alert, format_error_alert

logger = get_logger("slack")


class SlackNotifier:
    """Sends notifications to Slack via incoming webhooks.

    Uses Slack Block Kit for rich message formatting.
    """

    def __init__(self, config: SlackConfig):
        self.webhook_url = config.webhook_url
        self.channel = config.channel
        self.username = config.username
        self.icon_emoji = config.icon_emoji

    async def send_alert(self, result: DetectionResult) -> str:
        """Send a governance alert to Slack.

        Args:
            result: The detection result with post details and matched keywords.

        Returns:
            Slack API response text.

        Raises:
            RuntimeError: If the Slack webhook returns an error.
        """
        message = format_alert(result)

        # Add optional overrides
        if self.channel:
            message["channel"] = self.channel
        message["username"] = self.username
        message["icon_emoji"] = self.icon_emoji

        return await self._send(message)

    async def send_error(self, forum_name: str, error: str) -> str:
        """Send an error notification to Slack."""
        message = format_error_alert(forum_name, error)
        if self.channel:
            message["channel"] = self.channel
        message["username"] = self.username
        message["icon_emoji"] = self.icon_emoji

        return await self._send(message)

    async def send_test(self) -> str:
        """Send a test message to verify the webhook is working."""
        message = {
            "text": "DAO Governance Monitor - Test Notification",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*DAO Governance Monitor* is connected and working!\n"
                            "You'll receive alerts here when governance changes "
                            "or security council discussions are detected."
                        ),
                    },
                }
            ],
            "username": self.username,
            "icon_emoji": self.icon_emoji,
        }
        if self.channel:
            message["channel"] = self.channel

        return await self._send(message)

    async def _send(self, payload: dict) -> str:
        """Send a payload to the Slack webhook.

        Args:
            payload: The Slack message payload (blocks, text, etc.)

        Returns:
            Response text from Slack.

        Raises:
            RuntimeError: If the webhook returns a non-200 status.
        """
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.webhook_url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            ) as response:
                text = await response.text()

                if response.status != 200:
                    logger.error(
                        "slack_send_failed",
                        status=response.status,
                        response=text[:200],
                    )
                    raise RuntimeError(
                        f"Slack webhook error (HTTP {response.status}): {text}"
                    )

                logger.info("slack_message_sent")
                return text
