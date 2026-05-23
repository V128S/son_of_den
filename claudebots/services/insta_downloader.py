"""Instagram media downloader microservice.

Downloads photos, videos and Reels from public Instagram posts using yt-dlp.
Stories are not supported (require Instagram login).

Public API:
    detect_url(text)       -> str | None   — extract first Instagram URL from text
    InstagramDownloader.download(url)      -> list[MediaFile]
    InstagramDownloader.cleanup(files)     -> None
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches public post / reel / tv / igtv URLs
_INSTA_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|reels|tv|igtv)/([A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)

# Telegram Bot API upload limits
_TG_VIDEO_LIMIT = 50 * 1024 * 1024   # 50 MB — above this → send as document
_TG_PHOTO_LIMIT = 10 * 1024 * 1024   # 10 MB — above this → send as document


def detect_url(text: str) -> str | None:
    """Return the first Instagram post/reel URL found in text, or None."""
    m = _INSTA_RE.search(text)
    return m.group(0) if m else None


@dataclass
class MediaFile:
    path: Path
    media_type: str          # "photo" | "video" | "document"
    caption: str = ""
    size_bytes: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.path.exists():
            self.size_bytes = self.path.stat().st_size


class InstagramDownloader:
    """Downloads Instagram media to a temporary directory using yt-dlp."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def download(self, url: str) -> list[MediaFile]:
        """Download all media from a public Instagram URL.

        Returns a list of MediaFile objects (may be multiple for carousels).
        Returns an empty list on any error.
        """
        loop = asyncio.get_running_loop()
        try:
            files = await asyncio.wait_for(
                loop.run_in_executor(None, self._download_sync, url),
                timeout=self.timeout,
            )
            return files
        except asyncio.TimeoutError:
            logger.warning("Instagram download timed out: %s", url)
            return []
        except Exception as e:
            logger.warning("Instagram download failed: %s — %s", url, e)
            return []

    @staticmethod
    def cleanup(files: list[MediaFile]) -> None:
        """Remove downloaded files and their parent temp directory."""
        dirs: set[Path] = set()
        for f in files:
            try:
                if f.path.exists():
                    f.path.unlink()
                    dirs.add(f.path.parent)
            except Exception as e:
                logger.debug("cleanup: %s — %s", f.path, e)
        for d in dirs:
            try:
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal sync download (runs in thread pool)
    # ------------------------------------------------------------------

    def _download_sync(self, url: str) -> list[MediaFile]:
        import yt_dlp  # imported here to keep startup fast

        tmpdir = tempfile.mkdtemp(prefix="insta_")

        ydl_opts = {
            "outtmpl": os.path.join(tmpdir, "%(autonumber)s_%(id)s.%(ext)s"),
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "noplaylist": False,      # allow carousel (multi-image posts)
            "writethumbnail": False,
            "writeinfojson": False,
            # Avoid Instagram bot detection
            "sleep_interval": 1,
            "max_sleep_interval": 3,
        }

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

        # Flatten: handle both single item and playlist (carousel)
        entries = info.get("entries") or [info]
        result: list[MediaFile] = []

        for entry in entries:
            if not entry:
                continue
            ext = (entry.get("ext") or "").lower()
            # Find the actually downloaded file
            downloaded = self._find_downloaded(tmpdir, entry.get("id", ""))
            if not downloaded:
                continue

            # Re-encode video files to H.264/AAC with faststart for Telegram playback
            if downloaded.suffix.lower() in (".mp4", ".mov", ".m4v", ".webm", ".mkv"):
                downloaded = self._reencode_for_telegram(downloaded)

            ftype = self._classify(downloaded, ext)
            caption = (entry.get("title") or entry.get("description") or "")[:200]
            result.append(MediaFile(path=downloaded, media_type=ftype, caption=caption))

        return result

    @staticmethod
    def _reencode_for_telegram(src_path: Path) -> Path:
        """Re-encode video to H.264/AAC with faststart moov atom for Telegram playback.

        Returns the re-encoded path (replaces the original). Falls back to the
        original file if ffmpeg is unavailable or encoding fails.
        """
        if not shutil.which("ffmpeg"):
            logger.debug("ffmpeg not found — skipping re-encode")
            return src_path

        dst_path = src_path.with_name(src_path.stem + "_tg.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src_path),
            # Bake SAR into pixel dimensions so Telegram displays correctly.
            # trunc(iw*sar/2)*2 → display width rounded down to even number.
            # Without this, anamorphic Instagram videos appear squished.
            # Round dimensions to even (H.264 requirement) and fix SAR to 1:1.
            # Do NOT apply iw*sar scaling — Instagram uses square pixels (SAR=1:1);
            # a broken/undefined SAR in the container would make iw*sar go to 0
            # and squish the video. We keep raw pixel dimensions intact.
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(dst_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            src_path.unlink(missing_ok=True)
            logger.debug("Re-encoded %s → %s", src_path.name, dst_path.name)
            return dst_path
        except subprocess.CalledProcessError as e:
            logger.warning(
                "ffmpeg re-encode failed (stderr: %s) — using original",
                e.stderr[-500:].decode(errors="replace") if e.stderr else "",
            )
            dst_path.unlink(missing_ok=True)
            return src_path
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg re-encode timed out — using original")
            dst_path.unlink(missing_ok=True)
            return src_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_downloaded(tmpdir: str, video_id: str) -> Path | None:
        """Find the file that yt-dlp actually wrote (name may differ from template)."""
        tmp = Path(tmpdir)
        candidates = sorted(tmp.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0)
        if not candidates:
            return None
        # Prefer file with video_id in name if possible
        for c in candidates:
            if video_id and video_id in c.name:
                return c
        return candidates[-1]  # most recently written

    @staticmethod
    def _classify(path: Path, ext: str) -> str:
        """Classify file as photo / video / document based on extension and size."""
        img_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        vid_exts = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

        suffix = path.suffix.lower() or f".{ext}"
        size = path.stat().st_size if path.exists() else 0

        if suffix in img_exts:
            return "photo" if size <= _TG_PHOTO_LIMIT else "document"
        if suffix in vid_exts:
            return "video" if size <= _TG_VIDEO_LIMIT else "document"
        return "document"
