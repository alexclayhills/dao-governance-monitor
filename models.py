"""Data models for the DAO Forum Monitor."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ForumConfig(BaseModel):
    """Configuration for a single forum to monitor."""

    name: str
    url: str
    type: str = "discourse"
    enabled: bool = True
    categories: list[str] = Field(default_factory=list)


class KeywordGroup(BaseModel):
    """A named group of keyword patterns."""

    name: str
    patterns: list[str]
    title_weight: float = 2.0
    body_weight: float = 1.0


class MonitoringConfig(BaseModel):
    """Top-level monitoring configuration."""

    poll_interval: int = 300  # seconds
    max_retries: int = 3
    timeout: int = 30
    detection_threshold: float = 1.5


class SlackConfig(BaseModel):
    """Slack notification configuration."""

    webhook_url: Optional[str] = None
    bot_token: Optional[str] = None
    signing_secret: Optional[str] = None
    app_token: Optional[str] = None
    channel: Optional[str] = None
    username: str = "DAO Governance Monitor"
    icon_emoji: str = ":bell:"

    @field_validator("webhook_url", "bot_token", "signing_secret", "app_token", mode="before")
    @classmethod
    def empty_str_to_none(cls, v):
        if v == "":
            return None
        return v


class AppConfig(BaseModel):
    """Complete application configuration."""

    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    slack: SlackConfig
    forums: list[ForumConfig]
    keywords: dict[str, list[str]]
    database_path: str = "dao_monitor.db"


class ForumPost(BaseModel):
    """A normalized post from any forum."""

    forum_name: str
    post_id: str
    topic_id: str
    title: str
    body: str
    author: str
    category: str = ""
    url: str
    created_at: datetime
    reply_count: int = 0
    like_count: int = 0


class KeywordMatch(BaseModel):
    """A single keyword match result."""

    group: str
    pattern: str
    location: str  # "title" or "body"
    matched_text: str


class DetectionResult(BaseModel):
    """Result of analyzing a post for governance keywords."""

    post: ForumPost
    triggered: bool
    score: float
    matches: list[KeywordMatch] = Field(default_factory=list)
