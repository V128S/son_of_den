"""YouTube audio downloader microservice.

Downloads the best-quality audio stream from public YouTube videos using yt-dlp.
Playlists and Shorts are intentionally not supported.

Public API:
    detect_url(text)             -> str | None      — extract first YouTube video URL
    detect_summary_cmd(text)     -> str | None      — extract YT URL from summary commands
    YTDownloader.download_audio(url) -> AudioFile | None
    YTDownloader.fetch_transcript(url) -> str | None  — auto-subs as plain text
    YTDownloader.cleanup(file)   -> None
"""
from __future__ import annotations

import asyncio
import logging
import os
import re as _re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp  # noqa: F401 — imported at module level so tests can patch it

logger = logging.getLogger(__name__)

# Telegram Bot API upload limit for audio/documents
_TG_AUDIO_LIMIT = 50 * 1024 * 1024  # 50 MB

# Regex: matches watch?v=... and youtu.be/... URLs.
# Requires https?:// to avoid false-positives on e.g. "notyoutube.com".
# Explicitly rejects /shorts/, /playlist?, /channel/ paths.
_YT_RE = _re.compile(
    r"https?://(?:www\.)?(?:"
    r"youtube\.com/watch\?(?:[^#\s]*&)*v=([A-Za-z0-9_-]{11})[^\s]*"
    r"|youtu\.be/([A-Za-z0-9_-]{11})[^\s]*"
    r")",
    _re.IGNORECASE,
)


def detect_url(text: str) -> str | None:
    """Return the first YouTube video URL found in *text*, or None.

    Shorts (/shorts/), playlists (/playlist?) and channel pages are ignored.
    Requires https?:// prefix — bare domain strings are not matched.
    """
    for m in _YT_RE.finditer(text):
        return m.group(0)
    return None


# Matches summary-request prefix: "резюме", "кратко", "саммари", "summary", "/summary"
_SUMMARY_PREFIX_RE = _re.compile(
    r"^(?:резюме|кратко|саммари|summary|/summary)\s+",
    _re.IGNORECASE,
)


def detect_summary_cmd(text: str) -> str | None:
    """Return YT URL if text is a summary request like 'резюме https://...', else None."""
    if not _SUMMARY_PREFIX_RE.match(text.strip()):
        return None
    stripped = _SUMMARY_PREFIX_RE.sub("", text.strip(), count=1)
    return detect_url(stripped)


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

    async def fetch_transcript(self, url: str) -> str | None:
        """Fetch auto-generated or manual subtitles for a YouTube video.

        Returns plain text (stripped of VTT markup) or None if unavailable.
        """
        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._fetch_subtitles_sync, url),
                timeout=30.0,
            )
            return result
        except asyncio.TimeoutError:
            logger.debug("Subtitle fetch timed out: %s", url)
            return None
        except Exception as e:
            logger.debug("Subtitle fetch failed: %s — %s", url, e)
            return None

    def _fetch_subtitles_sync(self, url: str) -> str | None:
        """Sync subtitle download; runs in thread pool."""
        tmpdir = tempfile.mkdtemp(prefix="yt_subs_")
        ydl_opts = {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["ru", "ru-RU", "en"],
            "subtitlesformat": "vtt",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "outtmpl": os.path.join(tmpdir, "%(id)s"),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            logger.debug("yt-dlp subtitle download failed: %s", e)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None

        # Find any .vtt file
        tmp = Path(tmpdir)
        vtt_files = list(tmp.glob("*.vtt"))
        if not vtt_files:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None

        # Prefer Russian, then any
        chosen = next((f for f in vtt_files if ".ru" in f.name), vtt_files[0])
        try:
            raw = chosen.read_text(encoding="utf-8", errors="replace")
            text = self._parse_vtt(raw)
        except Exception:
            text = None
        shutil.rmtree(tmpdir, ignore_errors=True)
        return text if text else None

    @staticmethod
    def _parse_vtt(raw: str) -> str:
        """Strip VTT header/timestamps and HTML tags, deduplicate lines."""
        import re as _re
        lines = raw.splitlines()
        seen: set[str] = set()
        result: list[str] = []
        for line in lines:
            # Skip header, timing lines, and WEBVTT marker
            if line.startswith("WEBVTT") or " --> " in line or not line.strip():
                continue
            # Strip HTML tags
            clean = _re.sub(r"<[^>]+>", "", line).strip()
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
        return " ".join(result)

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
        except asyncio.TimeoutError:
            logger.warning("YouTube download timed out: %s", url)
            return None
        except Exception as e:
            logger.warning("YouTube download failed: %s — %s", url, e)
            return None

    @staticmethod
    def cleanup(file: AudioFile | None) -> None:
        """Remove a downloaded audio file and its entire parent temp directory."""
        if file is None:
            return
        try:
            shutil.rmtree(file.path.parent, ignore_errors=True)
        except Exception as e:
            logger.debug("cleanup: %s — %s", file.path.parent, e)

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
            "noplaylist": True,  # single video only — reject playlists
            "writethumbnail": False,
            "writeinfojson": False,
            # No postprocessors → no re-encoding, native quality preserved
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            logger.warning("yt-dlp extract_info failed for %s: %s", url, e)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None

        if info is None:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return None

        video_id = info.get("id", "")
        title = (info.get("title") or info.get("alt_title") or url)[:200]
        duration_s = int(info.get("duration") or 0)

        downloaded = self._find_downloaded(tmpdir, video_id)
        if downloaded is None:
            logger.warning("No audio file found in tmpdir after yt-dlp download")
            shutil.rmtree(tmpdir, ignore_errors=True)
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
