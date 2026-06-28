"""Social media downloader for TikTok, X/Twitter and Threads.

Downloads videos and photos from public TikTok, X/Twitter and Threads posts using yt-dlp.
Threads uses a custom HTTP scraper (yt-dlp has no Threads extractor) with browser cookies.
Re-encodes video to H.264/AAC+faststart for Telegram playback (same as Instagram).

Public API:
    detect_platform(text) -> tuple[str, str] | None
        Returns (url, topic_name) or None.
        topic_name is one of: "🎬 TikTok", "🐦 X / Twitter", "🧵 Threads"

    SocialDownloader.download(url) -> list[MediaFile]
    SocialDownloader.cleanup(files) -> None
"""
from __future__ import annotations

import asyncio
import html as html_module
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

from claudebots.services.insta_downloader import InstagramDownloader, MediaFile

logger = logging.getLogger(__name__)

# TikTok: https://www.tiktok.com/@user/video/ID, https://vm.tiktok.com/ID
_TIKTOK_RE = re.compile(
    r"https?://(?:www\.|vm\.)?tiktok\.com/(?:@[^/]+/video/\d+|[A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)

# X / Twitter: https://twitter.com/user/status/ID, https://x.com/user/status/ID
_TWITTER_RE = re.compile(
    r"https?://(?:www\.)?(?:twitter|x)\.com/\S+/status/\d+/?",
    re.IGNORECASE,
)

# Threads: https://www.threads.net/@user/post/CODE, https://threads.com/...
_THREADS_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/@[^/\s]+/post/[A-Za-z0-9_-]+/?",
    re.IGNORECASE,
)

# Meta CDN domains that host Threads media
_META_CDN_RE = re.compile(
    r"https://(?:scontent|video|static)\.[a-z0-9-]+\."
    r"(?:cdninstagram|fbcdn|facebook)\.(?:com|net)/",
    re.IGNORECASE,
)

_SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.3 Safari/605.1.15"
)


def detect_platform(text: str) -> tuple[str, str] | None:
    """Return (url, topic_name) for the first TikTok, X/Twitter or Threads URL in text, or None."""
    m = _TIKTOK_RE.search(text)
    if m:
        return m.group(0), "🎬 TikTok"
    m = _TWITTER_RE.search(text)
    if m:
        return m.group(0), "🐦 X / Twitter"
    m = _THREADS_RE.search(text)
    if m:
        return m.group(0), "🧵 Threads"
    return None


class SocialDownloader:
    """Downloads TikTok / X/Twitter / Threads media to a temp directory."""

    def __init__(self, timeout: float = 90.0, cookies_browser: str = "") -> None:
        self.timeout = timeout
        self.cookies_browser = cookies_browser
        # Reuse InstagramDownloader's re-encode + classify helpers
        self._helper = InstagramDownloader.__new__(InstagramDownloader)

    async def download(self, url: str) -> list[MediaFile]:
        """Download all media from a TikTok, X/Twitter or Threads URL.

        Returns a list of MediaFile objects. Returns [] on any error.
        """
        loop = asyncio.get_running_loop()
        try:
            files = await asyncio.wait_for(
                loop.run_in_executor(None, self._download_sync, url),
                timeout=self.timeout,
            )
            return files
        except TimeoutError:
            logger.warning("Social download timed out: %s", url)
            return []
        except Exception as e:
            logger.warning("Social download failed: %s — %s", url, e)
            return []

    @staticmethod
    def cleanup(files: list[MediaFile]) -> None:
        """Remove downloaded files and their temp directory."""
        InstagramDownloader.cleanup(files)

    # ------------------------------------------------------------------
    # Internal dispatch
    # ------------------------------------------------------------------

    def _download_sync(self, url: str) -> list[MediaFile]:
        if "threads." in url:
            return self._download_threads_sync(url)
        return self._download_yt_dlp_sync(url)

    # ------------------------------------------------------------------
    # yt-dlp path (TikTok / X/Twitter)
    # ------------------------------------------------------------------

    def _download_yt_dlp_sync(self, url: str) -> list[MediaFile]:
        import yt_dlp

        tmpdir = tempfile.mkdtemp(prefix="social_")
        ydl_opts: dict = {
            "outtmpl": os.path.join(tmpdir, "%(autonumber)s_%(id)s.%(ext)s"),
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "noplaylist": True,
            "writethumbnail": False,
            "writeinfojson": False,
        }
        if self.cookies_browser:
            ydl_opts["cookiesfrombrowser"] = (self.cookies_browser, None, None, None)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            logger.warning("yt-dlp extract_info failed: %s", e)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return []

        if info is None:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return []

        entries = info.get("entries") or [info]
        result: list[MediaFile] = []

        for entry in entries:
            if not entry:
                continue
            ext = (entry.get("ext") or "").lower()
            downloaded = InstagramDownloader._find_downloaded(tmpdir, entry.get("id", ""))
            if not downloaded:
                continue

            if downloaded.suffix.lower() in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
                downloaded = InstagramDownloader._reencode_for_telegram(downloaded)

            ftype = InstagramDownloader._classify(downloaded, ext)
            caption = (entry.get("title") or entry.get("description") or "")[:200]
            result.append(MediaFile(path=downloaded, media_type=ftype, caption=caption))

        return result

    # ------------------------------------------------------------------
    # Custom Threads path
    # ------------------------------------------------------------------

    def _download_threads_sync(self, url: str) -> list[MediaFile]:
        """Fetch a Threads post page with browser cookies and extract media via OG tags."""
        import httpx
        import yt_dlp

        tmpdir = tempfile.mkdtemp(prefix="threads_")

        # Build cookie jar from browser via yt-dlp internals
        cookies: dict[str, str] = {}
        if self.cookies_browser:
            try:
                ydl_opts: dict = {
                    "quiet": True,
                    "cookiesfrombrowser": (self.cookies_browser, None, None, None),
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    for cookie in ydl.cookiejar:
                        domain = getattr(cookie, "domain", "") or ""
                        if any(d in domain for d in ("threads", "instagram", "facebook")):
                            cookies[cookie.name] = cookie.value
            except Exception as e:
                logger.warning("Threads: failed to extract browser cookies — %s", e)

        headers = {
            "User-Agent": _SAFARI_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }

        try:
            with httpx.Client(
                headers=headers,
                cookies=cookies,
                follow_redirects=True,
                timeout=30.0,
            ) as client:
                resp = client.get(url)
            if resp.status_code != 200:
                logger.warning("Threads page returned HTTP %s for %s", resp.status_code, url)
                shutil.rmtree(tmpdir, ignore_errors=True)
                return []
            page_html = resp.text
        except Exception as e:
            logger.warning("Threads page fetch failed: %s", e)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return []

        # Extract media URLs from OG meta tags (handles both attribute orders)
        video_url = _og_content(page_html, "video:url") or _og_content(page_html, "video")
        image_url = _og_content(page_html, "image")

        if not video_url and not image_url:
            logger.warning(
                "Threads: no OG media tags found in page (may need cookies or post is private)"
            )
            shutil.rmtree(tmpdir, ignore_errors=True)
            return []

        result: list[MediaFile] = []

        if video_url and _META_CDN_RE.match(video_url):
            path = self._fetch_media(video_url, tmpdir, "video.mp4", cookies, headers)
            if path:
                if path.suffix.lower() in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
                    path = InstagramDownloader._reencode_for_telegram(path)
                result.append(MediaFile(path=path, media_type="video"))

        if not result and image_url and _META_CDN_RE.match(image_url):
            path = self._fetch_media(image_url, tmpdir, "image.jpg", cookies, headers)
            if path:
                ftype = InstagramDownloader._classify(path, "")
                result.append(MediaFile(path=path, media_type=ftype))

        if not result:
            shutil.rmtree(tmpdir, ignore_errors=True)

        return result

    @staticmethod
    def _fetch_media(
        url: str,
        tmpdir: str,
        filename: str,
        cookies: dict[str, str],
        headers: dict[str, str],
    ) -> Path | None:
        import httpx

        dest = Path(tmpdir) / filename
        try:
            with httpx.Client(
                headers=headers, cookies=cookies, follow_redirects=True, timeout=60.0
            ) as client:
                with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        logger.warning("Threads media HTTP %s: %s", resp.status_code, url[:80])
                        return None
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            fh.write(chunk)
            return dest
        except Exception as e:
            logger.warning("Threads media download failed: %s", e)
            return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _og_content(html: str, prop: str) -> str | None:
    """Return content of <meta property="og:{prop}" ...> or None."""
    escaped = re.escape(prop)
    for pattern in (
        rf'<meta[^>]+property=["\']og:{escaped}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{escaped}["\']',
    ):
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return html_module.unescape(m.group(1))
    return None
