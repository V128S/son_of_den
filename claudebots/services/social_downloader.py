"""Social media downloader for TikTok, X/Twitter and Threads.

Downloads videos and photos from public TikTok, X/Twitter and Threads posts using yt-dlp.
Re-encodes video to H.264/AAC+faststart for Telegram playback (same as Instagram).
Threads downloads require browser cookies (ig_cookies_browser / IG_COOKIES_BROWSER).

Public API:
    detect_platform(text) -> tuple[str, str] | None
        Returns (url, topic_name) or None.
        topic_name is one of: "🎬 TikTok", "🐦 X / Twitter", "🧵 Threads"

    SocialDownloader.download(url) -> list[MediaFile]
    SocialDownloader.cleanup(files) -> None
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile

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

# Threads: https://www.threads.net/@user/post/CODE, https://threads.net/...
_THREADS_RE = re.compile(
    r"https?://(?:www\.)?threads\.(?:net|com)/@[^/\s]+/post/[A-Za-z0-9_-]+/?",
    re.IGNORECASE,
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
    """Downloads TikTok / X/Twitter / Threads media to a temp directory using yt-dlp."""

    def __init__(self, timeout: float = 90.0, cookies_browser: str = "") -> None:
        self.timeout = timeout
        self.cookies_browser = cookies_browser
        # Reuse InstagramDownloader's re-encode + classify helpers
        self._helper = InstagramDownloader.__new__(InstagramDownloader)

    async def download(self, url: str) -> list[MediaFile]:
        """Download all media from a TikTok or X/Twitter URL.

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

    def _download_sync(self, url: str) -> list[MediaFile]:
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
            # Threads requires a logged-in session; pass browser cookies to yt-dlp
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
