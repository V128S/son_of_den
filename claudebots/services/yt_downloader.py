"""YouTube audio downloader microservice.

Downloads the best-quality audio stream from public YouTube videos using yt-dlp.
Playlists and Shorts are intentionally not supported.

Public API:
    detect_url(text)             -> str | None      — extract first YouTube video URL
    YTDownloader.download_audio(url) -> AudioFile | None
    YTDownloader.cleanup(file)   -> None
"""
from __future__ import annotations

import asyncio
import logging
import os
import re as _re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp  # noqa: F401 — imported at module level so tests can patch it

logger = logging.getLogger(__name__)

# Telegram Bot API upload limit for audio/documents
_TG_AUDIO_LIMIT = 50 * 1024 * 1024  # 50 MB

# Regex: matches /watch?v=... and youtu.be/... but NOT /shorts/, /playlist?, /channel/

_YT_RE = _re.compile(
    r"(?:https?://)?(?:www\.)?(?:"
    r"youtube\.com/watch\?(?:[^#\s]*&)*v=([A-Za-z0-9_-]{4,12})[^\s]*"
    r"|youtu\.be/([A-Za-z0-9_-]{4,12})[^\s]*"
    r")",
    _re.IGNORECASE,
)


def detect_url(text: str) -> str | None:
    """Return the first YouTube video URL found in *text*, or None.

    Shorts (/shorts/), playlists (/playlist?) and channel pages are ignored.
    """
    for m in _YT_RE.finditer(text):
        return m.group(0)
    return None


@dataclass
class AudioFile:
    path: Path
    title: str
    duration_s: int  # seconds; 0 if unknown
    size_bytes: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.path.exists():
            self.size_bytes = self.path.stat().st_size

    @property
    def send_as_audio(self) -> bool:
        """True if small enough to send via send_audio; False → send_document."""
        return self.size_bytes <= _TG_AUDIO_LIMIT


class YTDownloader:
    """Downloads YouTube audio to a temporary directory using yt-dlp."""

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def download_audio(self, url: str) -> AudioFile | None:
        """Download the best-quality audio from a YouTube video URL.

        Returns an AudioFile on success, None on any error or timeout.
        """
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._download_sync, url),
                timeout=self.timeout,
            )
            return result
        except TimeoutError:
            logger.warning("YouTube download timed out: %s", url)
            return None
        except Exception as e:
            logger.warning("YouTube download failed: %s — %s", url, e)
            return None

    @staticmethod
    def cleanup(file: AudioFile | None) -> None:
        """Remove a downloaded audio file and its parent temp directory (if empty)."""
        if file is None:
            return
        try:
            if file.path.exists():
                file.path.unlink()
        except Exception as e:
            logger.debug("cleanup: %s — %s", file.path, e)
        # Remove parent dir if it is now empty
        try:
            parent = file.path.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal sync download (runs in thread pool)
    # ------------------------------------------------------------------

    def _download_sync(self, url: str) -> AudioFile | None:
        tmpdir = tempfile.mkdtemp(prefix="yt_audio_")

        ydl_opts: dict = {
            # Best audio without re-encoding: prefer m4a (AAC), fall back to any best
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": False,
            "noplaylist": True,   # single video only — reject playlists
            "writethumbnail": False,
            "writeinfojson": False,
            # No postprocessors → no re-encoding, native quality preserved
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            logger.warning("yt-dlp extract_info failed for %s: %s", url, e)
            return None

        if info is None:
            return None

        video_id = info.get("id", "")
        title = (info.get("title") or info.get("alt_title") or url)[:200]
        duration_s = int(info.get("duration") or 0)

        downloaded = self._find_downloaded(tmpdir, video_id)
        if downloaded is None:
            logger.warning("No audio file found in tmpdir after yt-dlp download")
            return None

        return AudioFile(path=downloaded, title=title, duration_s=duration_s)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_downloaded(tmpdir: str, video_id: str) -> Path | None:
        """Find the file yt-dlp actually wrote (name may differ from template)."""
        tmp = Path(tmpdir)
        candidates = sorted(
            tmp.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0
        )
        if not candidates:
            return None
        # Prefer file with video_id in the name
        for c in candidates:
            if video_id and video_id in c.name:
                return c
        return candidates[-1]
