"""One-time historical scan - looks back 2 weeks."""
import asyncio
from dotenv import load_dotenv
from dao_monitor.config import load_config
from dao_monitor.forums.registry import create_forum
from dao_monitor.monitoring.analyzer import ContentAnalyzer
from dao_monitor.monitoring.state_manager import StateManager
from dao_monitor.notifications.slack import SlackNotifier
from dao_monitor.utils.http_client import RateLimitedClient
from dao_monitor.utils.logger import setup_logging, get_logger

logger = get_logger("history_scan")

async def scan():
    load_dotenv()
    setup_logging(level="INFO")
    config = load_config("dao_monitor/config.yaml")

    http_client = RateLimitedClient(max_retries=config.monitoring.max_retries, timeout=config.monitoring.timeout)
    forums = [create_forum(fc, http_client) for fc in config.forums if fc.enabled]
    analyzer = ContentAnalyzer(keywords=config.keywords, threshold=config.monitoring.detection_threshold)
    state = StateManager(db_path="dao_monitor_history.db")
    slack = SlackNotifier(config.slack)

    TWO_WEEKS = 14 * 24 * 60  # 20160 minutes

    for forum in forums:
        try:
            logger.info("scanning_forum", forum=forum.name)
            posts = await forum.fetch_latest_posts(since_minutes=TWO_WEEKS)
            logger.info("posts_found", forum=forum.name, count=len(posts))

            for post in posts:
                try:
                    detailed = await forum.fetch_topic_details(post.topic_id)
                    if detailed:
                        post = detailed
                except Exception:
                    pass

                result = analyzer.analyze(post)
                if result.triggered and state.should_notify(post.post_id, result.score):
                    await slack.send_alert(result)
                    state.mark_notified(
                        post_id=post.post_id,
                        forum_name=post.forum_name,
                        title=post.title,
                        url=post.url,
                        score=result.score,
                        keywords=str([m.matched_text for m in result.matches]),
                        slack_response="sent",
                    )
                    logger.info("alert_sent", forum=post.forum_name, title=post.title[:60], score=result.score)
        except Exception as e:
            logger.error("forum_error", forum=forum.name, error=str(e))

    await http_client.close()
    print("Historical scan complete!")

asyncio.run(scan())
