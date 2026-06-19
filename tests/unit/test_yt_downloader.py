"""Unit tests for yt_downloader service."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from claudebots.services.yt_downloader import (
    AudioFile,
    YTDownloader,
    detect_url,
)

# ---------------------------------------------------------------------------
# detect_url — URL detection
# ---------------------------------------------------------------------------

class TestDetectUrl:
    def test_detects_watch_url(self):
        text = "Слушай вот это: https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert detect_url(text) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_detects_short_youtu_be(self):
        text = "https://youtu.be/dQw4w9WgXcQ"
        assert detect_url(text) == "https://youtu.be/dQw4w9WgXcQ"

    def test_detects_without_www(self):
        # Protocol required; bare domain without https:// is NOT matched (prevents false positives)
        text = "Посмотри: https://youtube.com/watch?v=dQw4w9WgXcQ"
        result = detect_url(text)
        assert result is not None
        assert "dQw4w9WgXcQ" in result

    def test_does_not_match_without_protocol(self):
        text = "youtube.com/watch?v=dQw4w9WgXcQ"
        assert detect_url(text) is None

    def test_does_not_match_fake_domain(self):
        text = "https://notyoutube.com/watch?v=dQw4w9WgXcQ"
        assert detect_url(text) is None

    def test_ignores_shorts(self):
        text = "https://www.youtube.com/shorts/dQw4w9WgXcQ"
        assert detect_url(text) is None

    def test_ignores_playlist(self):
        text = "https://www.youtube.com/playlist?list=PLxxx"
        assert detect_url(text) is None

    def test_ignores_channel(self):
        text = "https://www.youtube.com/channel/UCxxx"
        assert detect_url(text) is None

    def test_returns_none_for_plain_text(self):
        assert detect_url("привет как дела") is None

    def test_returns_first_url_when_multiple(self):
        text = (
            "https://youtu.be/dQw4w9WgXcQ и ещё "
            "https://www.youtube.com/watch?v=9bZkp7q19f0"
        )
        result = detect_url(text)
        assert result == "https://youtu.be/dQw4w9WgXcQ"

    def test_youtu_be_with_timestamp(self):
        text = "https://youtu.be/dQw4w9WgXcQ?t=42"
        result = detect_url(text)
        assert result is not None
        assert "dQw4w9WgXcQ" in result

    def test_watch_url_with_extra_params(self):
        text = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxxx&index=3"
        result = detect_url(text)
        assert result is not None
        assert "dQw4w9WgXcQ" in result


# ---------------------------------------------------------------------------
# AudioFile dataclass
# ---------------------------------------------------------------------------

class TestAudioFile:
    def test_size_bytes_zero_for_missing_path(self, tmp_path):
        f = AudioFile(path=tmp_path / "nonexistent.m4a", title="Test", duration_s=120)
        assert f.size_bytes == 0

    def test_size_bytes_populated_for_existing_file(self, tmp_path):
        p = tmp_path / "audio.m4a"
        p.write_bytes(b"x" * 1024)
        f = AudioFile(path=p, title="Test", duration_s=60)
        assert f.size_bytes == 1024

    def test_send_as_audio_for_small_file(self, tmp_path):
        p = tmp_path / "small.m4a"
        p.write_bytes(b"x" * 100)
        f = AudioFile(path=p, title="T", duration_s=10)
        assert f.send_as_audio is True

    def test_send_as_document_for_large_file(self, tmp_path):
        p = tmp_path / "big.m4a"
        p.write_bytes(b"x")
        f = AudioFile(path=p, title="T", duration_s=10)
        object.__setattr__(f, "size_bytes", 51 * 1024 * 1024)
        assert f.send_as_audio is False


# ---------------------------------------------------------------------------
# YTDownloader.download_audio — async, uses thread pool
# ---------------------------------------------------------------------------

class TestYTDownloaderDownloadAudio:
    @pytest.fixture
    def downloader(self):
        return YTDownloader(timeout=30.0)

    async def test_returns_audio_file_on_success(self, downloader, tmp_path):
        fake_audio = tmp_path / "audio.m4a"
        fake_audio.write_bytes(b"audio data")

        fake_info = {
            "id": "dQw4w9WgXcQ",
            "title": "Rick Astley - Never Gonna Give You Up",
            "duration": 213,
            "ext": "m4a",
        }

        with patch("claudebots.services.yt_downloader.yt_dlp") as mock_ydl_module:
            mock_ydl = MagicMock()
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.return_value = fake_info
            mock_ydl_module.YoutubeDL.return_value = mock_ydl

            with patch.object(downloader, "_find_downloaded", return_value=fake_audio):
                result = await downloader.download_audio("https://youtu.be/dQw4w9WgXcQ")

        assert result is not None
        assert result.title == "Rick Astley - Never Gonna Give You Up"
        assert result.duration_s == 213

    async def test_returns_none_on_timeout(self, downloader):
        with patch.object(downloader, "_download_sync", side_effect=asyncio.TimeoutError):
            result = await downloader.download_audio("https://youtu.be/xxx")

        assert result is None

    async def test_returns_none_when_yt_dlp_returns_none(self, downloader):
        with patch("claudebots.services.yt_downloader.yt_dlp") as mock_ydl_module:
            mock_ydl = MagicMock()
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.return_value = None
            mock_ydl_module.YoutubeDL.return_value = mock_ydl

            result = await downloader.download_audio("https://youtu.be/xxx")

        assert result is None

    async def test_returns_none_on_exception(self, downloader):
        with patch("claudebots.services.yt_downloader.yt_dlp") as mock_ydl_module:
            mock_ydl_module.YoutubeDL.side_effect = RuntimeError("no network")
            result = await downloader.download_audio("https://youtu.be/xxx")

        assert result is None


# ---------------------------------------------------------------------------
# YTDownloader.cleanup
# ---------------------------------------------------------------------------

class TestYTDownloaderCleanup:
    def test_removes_file_and_dir(self, tmp_path):
        audio_file = tmp_path / "audio.m4a"
        audio_file.write_bytes(b"data")

        f = AudioFile(path=audio_file, title="T", duration_s=10)
        YTDownloader.cleanup(f)

        assert not audio_file.exists()

    def test_handles_already_missing_file(self, tmp_path):
        missing = tmp_path / "gone.m4a"
        f = AudioFile(path=missing, title="T", duration_s=0)
        YTDownloader.cleanup(f)  # should not raise


# ---------------------------------------------------------------------------
# detect_summary_cmd
# ---------------------------------------------------------------------------

class TestDetectSummaryCmd:
    def test_detects_rezume_prefix(self):
        from claudebots.services.yt_downloader import detect_summary_cmd
        result = detect_summary_cmd("резюме https://youtu.be/dQw4w9WgXcQ")
        assert result is not None
        assert "dQw4w9WgXcQ" in result

    def test_detects_kratko_prefix(self):
        from claudebots.services.yt_downloader import detect_summary_cmd
        result = detect_summary_cmd("кратко https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert result is not None

    def test_detects_summary_prefix(self):
        from claudebots.services.yt_downloader import detect_summary_cmd
        result = detect_summary_cmd("summary https://youtu.be/dQw4w9WgXcQ")
        assert result is not None

    def test_plain_yt_url_not_detected(self):
        from claudebots.services.yt_downloader import detect_summary_cmd
        result = detect_summary_cmd("https://youtu.be/dQw4w9WgXcQ")
        assert result is None

    def test_empty_text_returns_none(self):
        from claudebots.services.yt_downloader import detect_summary_cmd
        assert detect_summary_cmd("") is None


# ---------------------------------------------------------------------------
# YTDownloader._parse_vtt
# ---------------------------------------------------------------------------

class TestParseVtt:
    def test_strips_header_and_timestamps(self):
        raw = """WEBVTT
Kind: captions
Language: ru

00:00:00.000 --> 00:00:02.000
Привет мир

00:00:02.000 --> 00:00:04.000
как дела
"""
        result = YTDownloader._parse_vtt(raw)
        assert "Привет мир" in result
        assert "как дела" in result
        assert "WEBVTT" not in result
        assert "-->" not in result

    def test_deduplicates_lines(self):
        raw = """WEBVTT

00:00:00.000 --> 00:00:01.000
hello world

00:00:01.000 --> 00:00:02.000
hello world
"""
        result = YTDownloader._parse_vtt(raw)
        assert result.count("hello world") == 1
