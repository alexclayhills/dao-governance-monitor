"""Data models for the DAO Forum Monitor."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ForumConfig(BaseModel):
    name: str
    url: str
    type: str = "discourse"
    enabled: bool = True
    categories: list[str] = Field(default_factory=list)


class KeywordGroup(BaseModel):
    name: str
    patterns: list[str]
    title_weight: float = 2.0
    body_weight: float = 1.0


class MonitoringConfig(BaseModel):
    poll_interval: int = 300
    max_retries: int = 3
    timeout: int = 30
    detection_threshold: float = 1.5


class SlackConfig(BaseModel):
    webhook_url: str
    channel: Optional[str] = None
    username: str = "DAO Governance Monitor"
    icon_emoji: str = ":bell:"


class AppConfig(BaseModel):
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    slack: SlackConfig
    forums: list[ForumConfig]
    keywords: dict[str, list[str]]
    database_path: str = "dao_monitor.db"


class ForumPost(BaseModel):
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
    group: str
    pattern: str
    location: str
    matched_text: str


class DetectionResult(BaseModel):
    post: ForumPost
    triggered: bool
    score: float
    matches: list[KeywordMatch] = Field(default_factory=list)
