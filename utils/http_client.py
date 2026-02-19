"""Rate-limited async HTTP client with retry logic."""

import asyncio
from typing import Optional

import aiohttp

from .logger import get_logger

logger = get_logger("http_client")


class RateLimitedClient:
    """Async HTTP client with rate limiting and exponential backoff.

    Ensures we don't overwhelm forum APIs with requests.
    """

    def __init__(
        self,
        requests_per_minute: int = 30,
        max_retries: int = 3,
        timeout: int = 30,
    ):
        self.min_interval = 60.0 / requests_per_minute
        self.max_retries = max_retries
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._last_request_time: float = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "User-Agent": "DAOGovernanceMonitor/1.0 (governance monitoring bot)",
                    "Accept": "application/json",
                },
            )
        return self._session

    async def _rate_limit(self):
        """Enforce minimum interval between requests."""
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_request_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_request_time = asyncio.get_event_loop().time()

    async def get(self, url: str, params: dict = None) -> dict:
        """Make a GET request with rate limiting and retries.

        Returns parsed JSON response.
        Raises after max_retries attempts.
        """
        session = await self._get_session()

        for attempt in range(self.max_retries):
            await self._rate_limit()

            try:
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        logger.debug("request_success", url=url, status=200)
                        return data

                    if response.status == 429:
                        # Rate limited - wait longer
                        retry_after = int(
                            response.headers.get("Retry-After", 60)
                        )
                        logger.warning(
                            "rate_limited",
                            url=url,
                            retry_after=retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if response.status >= 500:
                        # Server error - retry with backoff
                        wait = 2**attempt * 5
                        logger.warning(
                            "server_error",
                            url=url,
                            status=response.status,
                            retry_in=wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                    # Client error - don't retry
                    text = await response.text()
                    logger.error(
                        "client_error",
                        url=url,
                        status=response.status,
                        body=text[:200],
                    )
                    raise aiohttp.ClientResponseError(
                        response.request_info,
                        response.history,
                        status=response.status,
                        message=f"HTTP {response.status}: {text[:200]}",
                    )

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                wait = 2**attempt * 5
                logger.warning(
                    "request_failed",
                    url=url,
                    error=str(e),
                    attempt=attempt + 1,
                    retry_in=wait,
                )
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(wait)

        raise RuntimeError(f"Max retries ({self.max_retries}) exceeded for {url}")

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
