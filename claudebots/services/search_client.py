"""Lightweight Exa web-search client for panel topic enrichment.

Optional — when EXA_API_KEY is not set, all methods return empty results silently.
Used by the panel router to inject real-world context before panel discussions.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_EXA_BASE = "https://api.exa.ai"


class SearchResult:
    __slots__ = ("title", "url", "snippet")

    def __init__(self, title: str, url: str, snippet: str) -> None:
        self.title = title
        self.url = url
        self.snippet = snippet

    def __repr__(self) -> str:
        return f"SearchResult(title={self.title!r})"


class SearchClient:
    """Thin async Exa search wrapper.

    Parameters
    ----------
    api_key:
        Exa API key. Pass ``None`` to create a disabled (no-op) client.
    timeout:
        HTTP request timeout in seconds.
    """

    def __init__(self, api_key: str | None, timeout: float = 15.0) -> None:
        self._enabled = bool(api_key)
        if api_key:
            self._client = httpx.AsyncClient(
                base_url=_EXA_BASE,
                headers={
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        else:
            self._client = None  # type: ignore[assignment]

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def search(self, query: str, num_results: int = 3) -> list[SearchResult]:
        """Search the web and return top results.

        Returns an empty list if the client is disabled or the request fails.
        """
        if not self._enabled:
            return []
        try:
            payload: dict[str, Any] = {
                "query": query,
                "numResults": num_results,
                "type": "neural",
                "contents": {
                    "text": {"maxCharacters": 400},
                },
            }
            resp = await self._client.post("/search", json=payload)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("results", []):
                snippet = ""
                if item.get("text"):
                    snippet = str(item["text"])[:400].strip()
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=snippet,
                ))
            return results
        except Exception as e:
            logger.warning("SearchClient.search(%r) failed: %s", query[:60], e)
            return []

    def format_results(self, results: list[SearchResult]) -> str:
        """Format search results as a compact text block for injection into prompts."""
        if not results:
            return ""
        lines = ["🔍 Актуальные данные из сети:"]
        for r in results:
            lines.append(f"• {r.title}")
            if r.snippet:
                lines.append(f"  {r.snippet[:200]}")
        return "\n".join(lines)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
