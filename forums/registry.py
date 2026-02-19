"""Forum type registry - maps forum types to their implementations."""

from ..models import ForumConfig
from ..utils.http_client import RateLimitedClient
from .base import BaseForum
from .discourse import DiscourseForum

# Register forum types here. To add a new forum type:
# 1. Create a new class extending BaseForum
# 2. Add it to this mapping
FORUM_TYPES: dict[str, type[BaseForum]] = {
    "discourse": DiscourseForum,
}


def create_forum(config: ForumConfig, http_client: RateLimitedClient) -> BaseForum:
    """Factory function to create a forum instance from config.

    Args:
        config: Forum configuration with type, URL, etc.
        http_client: Shared HTTP client for rate limiting.

    Returns:
        An initialized forum instance.

    Raises:
        ValueError: If the forum type is not registered.
    """
    forum_class = FORUM_TYPES.get(config.type)
    if forum_class is None:
        available = ", ".join(FORUM_TYPES.keys())
        raise ValueError(
            f"Unknown forum type '{config.type}' for forum '{config.name}'. "
            f"Available types: {available}"
        )

    return forum_class(config, http_client)
