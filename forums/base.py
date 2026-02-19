"""Abstract base class for forum implementations."""

from abc import ABC, abstractmethod

from ..models import ForumConfig, ForumPost


class BaseForum(ABC):
    """Base class that all forum implementations must extend.

    Each forum type (Discourse, custom, etc.) implements fetch_latest_posts
    to return normalized ForumPost objects.
    """

    def __init__(self, config: ForumConfig, http_client):
        self.config = config
        self.name = config.name
        self.base_url = config.url.rstrip("/")
        self.http = http_client

    @abstractmethod
    async def fetch_latest_posts(self, since_minutes: int = 30) -> list[ForumPost]:
        """Fetch recent posts from the forum.

        Args:
            since_minutes: Only return posts from the last N minutes.

        Returns:
            List of normalized ForumPost objects.
        """
        pass

    @abstractmethod
    async def fetch_topic_details(self, topic_id: str) -> ForumPost | None:
        """Fetch full details for a specific topic.

        Args:
            topic_id: The forum's topic identifier.

        Returns:
            ForumPost with full body text, or None if not found.
        """
        pass

    def __repr__(self):
        return f"{self.__class__.__name__}(name={self.name!r}, url={self.base_url!r})"
