"""Content analysis engine for detecting governance-related discussions.

Uses regex-based keyword matching with a weighted scoring system:
- Title matches: 2x weight (titles are strong signals)
- Body matches: 1x weight
- Priority category bonus: 1.5x multiplier

Posts scoring above the detection threshold trigger notifications.
"""

import re
from typing import Optional

from ..models import DetectionResult, ForumPost, KeywordMatch
from ..utils.logger import get_logger

logger = get_logger("analyzer")

# Default detection threshold
DEFAULT_THRESHOLD = 1.5

# Weight multipliers
TITLE_WEIGHT = 2.0
BODY_WEIGHT = 1.0
CATEGORY_MULTIPLIER = 1.5

# Categories that get a score bonus
PRIORITY_CATEGORIES = {
    "governance",
    "security",
    "security council",
    "proposals",
    "voting",
    "constitution",
    "treasury",
}


class ContentAnalyzer:
    """Analyzes forum posts for governance and security council keywords.

    Compiles regex patterns at initialization for fast matching.
    """

    def __init__(
        self,
        keywords: dict[str, list[str]],
        threshold: float = DEFAULT_THRESHOLD,
    ):
        """Initialize with keyword groups.

        Args:
            keywords: Dict mapping group names to lists of regex patterns.
                      e.g. {"governance": ["governance.*change", "proposal.*vote"]}
            threshold: Minimum score to trigger a notification.
        """
        self.threshold = threshold
        self._compiled: dict[str, list[re.Pattern]] = {}

        for group_name, patterns in keywords.items():
            compiled = []
            for pattern in patterns:
                try:
                    compiled.append(re.compile(pattern, re.IGNORECASE))
                except re.error as e:
                    logger.warning(
                        "invalid_pattern",
                        group=group_name,
                        pattern=pattern,
                        error=str(e),
                    )
            self._compiled[group_name] = compiled

        total_patterns = sum(len(p) for p in self._compiled.values())
        logger.info(
            "analyzer_initialized",
            groups=len(self._compiled),
            total_patterns=total_patterns,
            threshold=threshold,
        )

    def analyze(self, post: ForumPost) -> DetectionResult:
        """Analyze a single post for governance keywords.

        Returns a DetectionResult with score and matched keywords.
        The post is flagged as triggered if score >= threshold.
        """
        score = 0.0
        matches: list[KeywordMatch] = []

        for group_name, patterns in self._compiled.items():
            for pattern in patterns:
                # Check title (higher weight)
                title_match = pattern.search(post.title)
                if title_match:
                    score += TITLE_WEIGHT
                    matches.append(
                        KeywordMatch(
                            group=group_name,
                            pattern=pattern.pattern,
                            location="title",
                            matched_text=title_match.group(),
                        )
                    )

                # Check body
                body_match = pattern.search(post.body)
                if body_match:
                    score += BODY_WEIGHT
                    matches.append(
                        KeywordMatch(
                            group=group_name,
                            pattern=pattern.pattern,
                            location="body",
                            matched_text=body_match.group(),
                        )
                    )

        # Category bonus for governance-related categories
        if post.category.lower() in PRIORITY_CATEGORIES:
            score *= CATEGORY_MULTIPLIER

        triggered = score >= self.threshold

        if triggered:
            logger.info(
                "post_triggered",
                forum=post.forum_name,
                post_id=post.post_id,
                title=post.title[:80],
                score=score,
                match_count=len(matches),
            )

        return DetectionResult(
            post=post,
            triggered=triggered,
            score=score,
            matches=matches,
        )
