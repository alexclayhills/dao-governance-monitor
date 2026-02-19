"""Discourse forum API client.

Discourse exposes public JSON endpoints that we use to fetch topics and posts
without authentication. This works for all major DAO forums.

Key endpoints:
  /latest.json      - Latest topics across all categories
  /t/{id}/posts.json - Full topic with posts
  /categories.json   - Category listing
  /search.json       - Full-text search
"""

from datetime import datetime, timedelta, timezone
from html import unescape
import re

from ..models import ForumConfig, ForumPost
from ..utils.logger import get_logger
from .base import BaseForum

logger = get_logger("discourse")


def _strip_html(html_text: str) -> str:
    """Remove HTML tags and decode entities for plain text."""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class DiscourseForum(BaseForum):
    """Client for Discourse-based DAO forums.

    Uses the public JSON API endpoints that Discourse exposes by default.
    No authentication required for public forums.
    """

    def __init__(self, config: ForumConfig, http_client):
        super().__init__(config, http_client)
        self._categories: dict[int, str] = {}  # id -> name cache

    async def _load_categories(self):
        """Fetch and cache category names."""
        if self._categories:
            return

        try:
            data = await self.http.get(f"{self.base_url}/categories.json")
            categories = data.get("category_list", {}).get("categories", [])
            self._categories = {
                cat["id"]: cat["name"] for cat in categories
            }
            logger.info(
                "categories_loaded",
                forum=self.name,
                count=len(self._categories),
            )
        except Exception as e:
            logger.warning(
                "categories_load_failed",
                forum=self.name,
                error=str(e),
            )

    def _get_category_name(self, category_id: int) -> str:
        """Look up category name from cached mapping."""
        return self._categories.get(category_id, "Unknown")

    def _topic_to_post(self, topic: dict, body: str = "") -> ForumPost:
        """Convert a Discourse topic JSON object to our ForumPost model."""
        topic_id = str(topic.get("id", ""))
        slug = topic.get("slug", "")
        category_id = topic.get("category_id", 0)

        created_str = topic.get("created_at", "")
        try:
            created_at = datetime.fromisoformat(
                created_str.replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            created_at = datetime.now(timezone.utc)

        return ForumPost(
            forum_name=self.name,
            post_id=f"{self.name}_{topic_id}",
            topic_id=topic_id,
            title=topic.get("title", ""),
            body=body or topic.get("excerpt", "") or "",
            author=topic.get("last_poster_username", "unknown"),
            category=self._get_category_name(category_id),
            url=f"{self.base_url}/t/{slug}/{topic_id}",
            created_at=created_at,
            reply_count=topic.get("reply_count", 0),
            like_count=topic.get("like_count", 0),
        )

    async def fetch_latest_posts(self, since_minutes: int = 30) -> list[ForumPost]:
        """Fetch latest topics from the Discourse forum.

        Uses /latest.json which returns the most recently active topics.
        Filters to only return topics created or bumped within since_minutes.
        """
        await self._load_categories()

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        posts = []

        try:
            data = await self.http.get(
                f"{self.base_url}/latest.json",
                params={"order": "created", "ascending": "false"},
            )

            topics = data.get("topic_list", {}).get("topics", [])
            logger.info(
                "topics_fetched",
                forum=self.name,
                count=len(topics),
            )

            for topic in topics:
                post = self._topic_to_post(topic)

                # Filter by time
                if post.created_at.tzinfo is None:
                    post.created_at = post.created_at.replace(tzinfo=timezone.utc)
                if post.created_at < cutoff:
                    continue

                # Filter by category if configured
                if self.config.categories:
                    if post.category.lower() not in [
                        c.lower() for c in self.config.categories
                    ]:
                        continue

                posts.append(post)

        except Exception as e:
            logger.error(
                "fetch_failed",
                forum=self.name,
                error=str(e),
            )

        return posts

    async def fetch_topic_details(self, topic_id: str) -> ForumPost | None:
        """Fetch full topic details including the first post body.

        Uses /t/{topic_id}/posts.json to get the complete post content.
        """
        await self._load_categories()

        try:
            data = await self.http.get(
                f"{self.base_url}/t/{topic_id}/posts.json"
            )

            # Get the first post (original post)
            posts_stream = data.get("post_stream", {}).get("posts", [])
            body = ""
            if posts_stream:
                raw_body = posts_stream[0].get("cooked", "")
                body = _strip_html(raw_body)

            # We need the topic data too
            topic_data = await self.http.get(
                f"{self.base_url}/t/{topic_id}.json"
            )

            post = self._topic_to_post(topic_data, body=body)
            return post

        except Exception as e:
            logger.error(
                "topic_fetch_failed",
                forum=self.name,
                topic_id=topic_id,
                error=str(e),
            )
            return None
