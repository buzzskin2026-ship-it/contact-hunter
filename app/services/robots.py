from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx


@dataclass
class RobotsResult:
    allowed: bool
    crawl_delay: float | None = None


class RobotsCache:
    def __init__(self, client: httpx.AsyncClient, user_agent: str, enabled: bool = True) -> None:
        self.client = client
        self.user_agent = user_agent
        self.enabled = enabled
        self._cache: dict[str, RobotFileParser | None] = {}

    async def can_fetch(self, url: str) -> RobotsResult:
        if not self.enabled:
            return RobotsResult(True)
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._cache:
            robots_url = f"{origin}/robots.txt"
            parser = RobotFileParser()
            parser.set_url(robots_url)
            try:
                response = await self.client.get(robots_url, follow_redirects=False)
                if response.status_code >= 400:
                    self._cache[origin] = None
                else:
                    parser.parse(response.text.splitlines())
                    self._cache[origin] = parser
            except httpx.HTTPError:
                self._cache[origin] = None
        parser = self._cache[origin]
        if parser is None:
            return RobotsResult(True)
        return RobotsResult(
            allowed=parser.can_fetch(self.user_agent, url),
            crawl_delay=parser.crawl_delay(self.user_agent),
        )
