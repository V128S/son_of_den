"""Shared download helpers used by insta_downloader and yt_downloader."""
from pathlib import Path


def find_downloaded(tmpdir: str, video_id: str) -> Path | None:
    """Find the file yt-dlp actually wrote (name may differ from the template)."""
    tmp = Path(tmpdir)
    candidates = sorted(tmp.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    if not candidates:
        return None
    for c in candidates:
        if video_id and video_id in c.name:
            return c
    return candidates[-1]
