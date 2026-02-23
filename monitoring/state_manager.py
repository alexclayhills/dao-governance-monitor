"""SQLite-based state management for tracking seen posts and notifications.

Prevents duplicate notifications by recording which posts have been processed.
Uses SQLAlchemy for clean database interaction.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from ..utils.logger import get_logger

logger = get_logger("state_manager")

Base = declarative_base()


class SeenPost(Base):
    __tablename__ = "seen_posts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(String(255), unique=True, index=True, nullable=False)
    forum_name = Column(String(100), index=True, nullable=False)
    title = Column(Text)
    url = Column(Text)
    detection_score = Column(Float, default=0.0)
    first_seen_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    notified_at = Column(DateTime, nullable=True)
    keywords_matched = Column(Text, nullable=True)


class NotificationLog(Base):
    __tablename__ = "notification_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(String(255), index=True, nullable=False)
    forum_name = Column(String(100), nullable=False)
    sent_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    slack_response = Column(Text, nullable=True)
    score = Column(Float)


class KeywordConfig(Base):
    __tablename__ = "keyword_configs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    group = Column(String(100), nullable=False)
    keyword_text = Column(Text, nullable=False)
    is_regex = Column(Boolean, default=False)
    added_by = Column(String(100), default="")
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    active = Column(Boolean, default=True)


class ForumUserConfig(Base):
    __tablename__ = "forum_configs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    url = Column(Text, nullable=False)
    forum_type = Column(String(50), default="discourse")
    enabled = Column(Boolean, default=True)
    added_by = Column(String(100), default="")
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class StateManager:
    """Manages persistent state in SQLite to track processed posts.

    Key responsibilities:
    - Track which posts we've already seen
    - Prevent duplicate Slack notifications
    - Log all notification activity
    """

    def __init__(self, db_path: str = "dao_monitor.db"):
        """Initialize the state manager with a SQLite database.

        Creates the database and tables if they don't exist.
        """
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        logger.info("state_manager_initialized", db_path=db_path)

    def _get_session(self) -> Session:
        return self._session_factory()

    def should_notify(self, post_id: str, score: float) -> bool:
        """Check if we should send a notification for this post.

        Returns True if:
        - We haven't seen this post before, OR
        - We've seen it but haven't notified about it yet AND score is above threshold

        Returns False if we've already sent a notification for this post.
        """
        with self._get_session() as session:
            existing = (
                session.query(SeenPost).filter_by(post_id=post_id).first()
            )

            if existing is None:
                # New post - yes, notify
                return True

            if existing.notified_at is not None:
                # Already notified
                return False

            # Seen but not yet notified - always notify
            return True

    def mark_seen(
        self,
        post_id: str,
        forum_name: str,
        title: str,
        url: str,
        score: float,
    ):
        """Record that we've seen a post (without notification)."""
        with self._get_session() as session:
            existing = (
                session.query(SeenPost).filter_by(post_id=post_id).first()
            )
            if existing:
                existing.detection_score = max(
                    score, existing.detection_score or 0
                )
                session.commit()
                return

            post = SeenPost(
                post_id=post_id,
                forum_name=forum_name,
                title=title,
                url=url,
                detection_score=score,
            )
            session.add(post)
            session.commit()

    def mark_notified(
        self,
        post_id: str,
        forum_name: str,
        title: str,
        url: str,
        score: float,
        keywords: str = "",
        slack_response: str = "",
    ):
        """Record that we've sent a notification for this post."""
        now = datetime.now(timezone.utc)

        with self._get_session() as session:
            # Update or create the seen post record
            existing = (
                session.query(SeenPost).filter_by(post_id=post_id).first()
            )
            if existing:
                existing.notified_at = now
                existing.detection_score = score
                existing.keywords_matched = keywords
            else:
                post = SeenPost(
                    post_id=post_id,
                    forum_name=forum_name,
                    title=title,
                    url=url,
                    detection_score=score,
                    notified_at=now,
                    keywords_matched=keywords,
                )
                session.add(post)

            # Log the notification
            log_entry = NotificationLog(
                post_id=post_id,
                forum_name=forum_name,
                score=score,
                slack_response=slack_response,
            )
            session.add(log_entry)
            session.commit()

        logger.info(
            "notification_recorded",
            post_id=post_id,
            forum=forum_name,
            score=score,
        )

    def get_stats(self) -> dict:
        with self._get_session() as session:
            total_seen = session.query(SeenPost).count()
            total_notified = (
                session.query(SeenPost)
                .filter(SeenPost.notified_at.isnot(None))
                .count()
            )
            total_notifications = session.query(NotificationLog).count()
            return {
                "total_posts_seen": total_seen,
                "total_posts_notified": total_notified,
                "total_notifications_sent": total_notifications,
            }

    # ── Keyword Management ────────────────────────────────────────

    def add_user_keyword(self, group, pattern, added_by=""):
        with self._get_session() as session:
            kw = KeywordConfig(group=group, keyword_text=pattern, added_by=added_by)
            session.add(kw)
            session.commit()
            logger.info("user_keyword_added", group=group, pattern=pattern)

    def remove_user_keyword(self, keyword_id):
        with self._get_session() as session:
            kw = session.query(KeywordConfig).filter_by(id=keyword_id).first()
            if kw:
                session.delete(kw)
                session.commit()
                logger.info("user_keyword_removed", id=keyword_id)

    def list_keywords(self, group=None):
        with self._get_session() as session:
            query = session.query(KeywordConfig).filter_by(active=True)
            if group:
                query = query.filter_by(group=group)
            results = query.all()
            session.expunge_all()
            return results

    # ── Forum Management ──────────────────────────────────────────

    def add_user_forum(self, name, url, forum_type="discourse", added_by=""):
        with self._get_session() as session:
            forum = ForumUserConfig(name=name, url=url, forum_type=forum_type, added_by=added_by)
            session.add(forum)
            session.commit()
            logger.info("user_forum_added", name=name, url=url)

    def remove_user_forum(self, forum_id):
        with self._get_session() as session:
            forum = session.query(ForumUserConfig).filter_by(id=forum_id).first()
            if forum:
                session.delete(forum)
                session.commit()
                logger.info("user_forum_removed", id=forum_id, name=forum.name)

    def list_user_forums(self):
        with self._get_session() as session:
            results = session.query(ForumUserConfig).filter_by(enabled=True).all()
            session.expunge_all()
            return results
