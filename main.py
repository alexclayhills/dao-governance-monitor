"""Main entry point for the DAO Forum Governance Monitor.

Orchestrates the monitoring loop:
1. Poll each configured forum for new topics
2. Analyze topics for governance/security keywords
3. Send Slack notifications for triggered posts
4. Track state to prevent duplicate notifications

Can run as a one-shot check or continuous scheduler.
"""

import asyncio
import json
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import load_config
from .forums.registry import create_forum
from .monitoring.analyzer import ContentAnalyzer
from .monitoring.state_manager import StateManager
from .notifications.slack import SlackNotifier
from .utils.http_client import RateLimitedClient
from .utils.logger import get_logger, setup_logging

logger = get_logger("main")


async def monitor_cycle(
    forums,
    analyzer: ContentAnalyzer,
    state: StateManager,
    slack: SlackNotifier,
    fetch_details: bool = True,
):
    """Run a single monitoring cycle across all forums.

    Args:
        forums: List of initialized forum instances.
        analyzer: Content analyzer with compiled keyword patterns.
        state: State manager for duplicate prevention.
        slack: Slack notifier for sending alerts.
        fetch_details: If True, fetch full post body for better analysis.
    """
    total_posts = 0
    total_triggered = 0

    for forum in forums:
        try:
            logger.info("checking_forum", forum=forum.name)
            posts = await forum.fetch_latest_posts(since_minutes=30)
            total_posts += len(posts)

            for post in posts:
                # Optionally fetch full body for better keyword matching
                if fetch_details and not post.body:
                    detailed = await forum.fetch_topic_details(post.topic_id)
                    if detailed:
                        post = detailed

                # Analyze the post
                result = analyzer.analyze(post)

                if not result.triggered:
                    # Record as seen but don't notify
                    state.mark_seen(
                        post_id=post.post_id,
                        forum_name=post.forum_name,
                        title=post.title,
                        url=post.url,
                        score=result.score,
                    )
                    continue

                # Check if we should notify (avoid duplicates)
                if not state.should_notify(post.post_id, result.score):
                    logger.debug(
                        "skip_duplicate",
                        post_id=post.post_id,
                        title=post.title[:60],
                    )
                    continue

                # Send Slack notification
                try:
                    keywords_json = json.dumps(
                        [m.matched_text for m in result.matches]
                    )
                    response = await slack.send_alert(result)

                    state.mark_notified(
                        post_id=post.post_id,
                        forum_name=post.forum_name,
                        title=post.title,
                        url=post.url,
                        score=result.score,
                        keywords=keywords_json,
                        slack_response=response,
                    )
                    total_triggered += 1

                    logger.info(
                        "alert_sent",
                        forum=post.forum_name,
                        title=post.title[:60],
                        score=result.score,
                    )

                except Exception as e:
                    logger.error(
                        "slack_send_error",
                        post_id=post.post_id,
                        error=str(e),
                    )

        except Exception as e:
            logger.error(
                "forum_error",
                forum=forum.name,
                error=str(e),
            )
            # Try to notify about the error (don't fail if this also errors)
            try:
                await slack.send_error(forum.name, str(e))
            except Exception:
                pass

    stats = state.get_stats()
    logger.info(
        "cycle_complete",
        posts_checked=total_posts,
        alerts_sent=total_triggered,
        total_seen=stats["total_posts_seen"],
        total_notified=stats["total_posts_notified"],
    )


async def run_continuous(config_path: str = "config.yaml"):
    """Run the monitor continuously with scheduled polling."""
    load_dotenv()
    setup_logging(level="INFO")

    logger.info("starting_dao_monitor", config=config_path)
    config = load_config(config_path)

    # Initialize components
    http_client = RateLimitedClient(
        max_retries=config.monitoring.max_retries,
        timeout=config.monitoring.timeout,
    )

    forums = []
    for forum_config in config.forums:
        if not forum_config.enabled:
            logger.info("forum_disabled", forum=forum_config.name)
            continue
        forum = create_forum(forum_config, http_client)
        forums.append(forum)
        logger.info("forum_registered", forum=forum_config.name, url=forum_config.url)

    analyzer = ContentAnalyzer(
        keywords=config.keywords,
        threshold=config.monitoring.detection_threshold,
    )

    state = StateManager(db_path=config.database_path)
    slack = SlackNotifier(config.slack)

    # Load any user-added keywords from database
    db_keywords = state.list_keywords()
    for kw in db_keywords:
        try:
            analyzer.add_keyword(kw.group, kw.keyword_text)
        except ValueError:
            logger.warning("invalid_db_keyword", pattern=kw.keyword_text)
    if db_keywords:
        logger.info("loaded_db_keywords", count=len(db_keywords))

    # Start interactive Slack bot if configured
    slack_bot = None
    if config.slack.bot_token and config.slack.app_token and config.slack.signing_secret:
        from .notifications.slack_bot import SlackBot
        slack_bot = SlackBot(
            bot_token=config.slack.bot_token,
            app_token=config.slack.app_token,
            signing_secret=config.slack.signing_secret,
            analyzer=analyzer,
            state=state,
            config=config,
            http_client=http_client,
        )
        slack_bot.start()
        logger.info("interactive_bot_started")
    else:
        logger.info("interactive_bot_skipped", reason="bot_token/app_token/signing_secret not configured")

    # Graceful shutdown
    shutdown = asyncio.Event()

    def handle_signal(*_):
        logger.info("shutdown_signal_received")
        shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Send test notification on startup
    try:
        await slack.send_test()
        logger.info("test_notification_sent")
    except Exception as e:
        logger.error("test_notification_failed", error=str(e))

    # Main loop
    logger.info(
        "monitor_running",
        forums=len(forums),
        interval=config.monitoring.poll_interval,
    )

    while not shutdown.is_set():
        await monitor_cycle(forums, analyzer, state, slack)

        # Wait for next cycle or shutdown
        try:
            await asyncio.wait_for(
                shutdown.wait(),
                timeout=config.monitoring.poll_interval,
            )
        except asyncio.TimeoutError:
            pass  # Normal - just means it's time for the next cycle

    # Cleanup
    if slack_bot:
        slack_bot.stop()
    await http_client.close()
    logger.info("monitor_stopped")


async def run_once(config_path: str = "config.yaml"):
    """Run a single monitoring cycle (useful for testing or cron jobs)."""
    load_dotenv()
    setup_logging(level="INFO")

    config = load_config(config_path)

    http_client = RateLimitedClient(
        max_retries=config.monitoring.max_retries,
        timeout=config.monitoring.timeout,
    )

    forums = [
        create_forum(fc, http_client)
        for fc in config.forums
        if fc.enabled
    ]

    analyzer = ContentAnalyzer(
        keywords=config.keywords,
        threshold=config.monitoring.detection_threshold,
    )
    state = StateManager(db_path=config.database_path)
    slack = SlackNotifier(config.slack)

    await monitor_cycle(forums, analyzer, state, slack)
    await http_client.close()


def main():
    """CLI entry point."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "continuous"
    config_path = sys.argv[2] if len(sys.argv) > 2 else "config.yaml"

    if mode == "once":
        asyncio.run(run_once(config_path))
    elif mode == "continuous":
        asyncio.run(run_continuous(config_path))
    elif mode == "test":
        # Just send a test notification
        load_dotenv()
        setup_logging(level="INFO")
        config = load_config(config_path)
        slack = SlackNotifier(config.slack)
        asyncio.run(slack.send_test())
        print("Test notification sent!")
    else:
        print(f"Usage: python -m dao_monitor.main [continuous|once|test] [config.yaml]")
        sys.exit(1)


if __name__ == "__main__":
    main()
