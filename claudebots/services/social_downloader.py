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

# Meta CDN domains that host Threads/Instagram media.
# Matches scontent.fdnk3-2.fna.fbcdn.net, scontent.cdninstagram.com, etc.
# Uses [\w.-]+ to handle arbitrary subdomain chains before the CDN root.
_META_CDN_RE = re.compile(
    r"https://(?:scontent|video|static)[\w.-]+\.(?:cdninstagram|fbcdn|facebook)\.(?:com|net)/",
    re.IGNORECASE,
)

# Twitterbot UA triggers Threads server-side rendering of OG tags (including media URLs).
# Without it Threads returns a JS-only SPA shell with no extractable media.
_TWITTERBOT_UA = "Twitterbot/1.0"

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

    def __init__(
        self,
        timeout: float = 90.0,
        cookies_browser: str = "",
        cookies_file: str = "",
    ) -> None:
        self.timeout = timeout
        self.cookies_browser = cookies_browser
        self.cookies_file = cookies_file
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
            # Cap at 720p so files stay under Telegram's 50 MB Bot API limit.
            # Twitter/X can serve 4K at 100+ MB which times out and can't be uploaded.
            "format": (
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
                "/best[height<=720][ext=mp4]"
                "/best[height<=720]"
                "/best[ext=mp4]/best"
            ),
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "noplaylist": True,
            "writethumbnail": False,
            "writeinfojson": False,
        }
        if self.cookies_file:
            ydl_opts["cookiefile"] = self.cookies_file
        elif self.cookies_browser:
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
                # If still over Telegram's 50 MB limit, re-encode with target bitrate
                if downloaded.stat().st_size > InstagramDownloader._TG_MAX_BYTES:
                    logger.warning("File %d MB > 49 MB — re-encoding with size target", downloaded.stat().st_size // 1024 // 1024)
                    downloaded = InstagramDownloader._reencode_for_telegram(downloaded, target_bytes=InstagramDownloader._TG_MAX_BYTES)
                # If still too large after targeted re-encode, skip
                if downloaded.stat().st_size > InstagramDownloader._TG_MAX_BYTES:
                    logger.warning("Skipping %s — still %d MB after re-encode", downloaded.name, downloaded.stat().st_size // 1024 // 1024)
                    downloaded.unlink(missing_ok=True)
                    continue

            ftype = InstagramDownloader._classify(downloaded, ext)
            caption = (entry.get("title") or entry.get("description") or "")[:200]
            result.append(MediaFile(path=downloaded, media_type=ftype, caption=caption))

        return result

    # ------------------------------------------------------------------
    # Custom Threads path
    # ------------------------------------------------------------------

    def _download_threads_sync(self, url: str) -> list[MediaFile]:
        """Fetch a Threads post page and extract media from OG tags or __NEXT_DATA__ JSON."""
        import httpx

        tmpdir = tempfile.mkdtemp(prefix="threads_")
        cookies = self._load_cookies()

        # Twitterbot UA triggers Threads SSR with OG media tags; Safari UA returns JS-only shell
        headers = {
            "User-Agent": _TWITTERBOT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
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
                logger.warning("Threads page HTTP %s for %s", resp.status_code, url)
                shutil.rmtree(tmpdir, ignore_errors=True)
                return []
            page_html = resp.text
        except Exception as e:
            logger.warning("Threads page fetch failed: %s", e)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return []

        logger.debug("Threads page fetched (%d chars), cookies=%d", len(page_html), len(cookies))

        # Strategy 1: OG meta tags (present when authenticated or for some public posts)
        video_url = _og_content(page_html, "video:url") or _og_content(page_html, "video")
        image_url = _og_content(page_html, "image")

        # Strategy 2: __NEXT_DATA__ JSON blob (Next.js SSR with embedded post data)
        if not video_url and not image_url:
            video_url, image_url = _extract_next_data_media(page_html)

        # Strategy 3: Playwright headless browser (captures actual video CDN requests)
        # Used when OG/JSON scraping finds no video — Threads only serves video URLs via JS.
        if not video_url:
            pw_video_url = self._download_threads_playwright_sync(url, tmpdir)
            if pw_video_url:
                return pw_video_url

        if not video_url and not image_url:
            logger.warning(
                "Threads: no media found — cookies loaded=%s. "
                "Set IG_COOKIES_FILE to a Netscape cookies.txt exported from browser.",
                bool(cookies),
            )
            logger.debug("Threads HTML snippet: %s", page_html[:1500])
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

    def _download_threads_playwright_sync(
        self, url: str, tmpdir: str
    ) -> list[MediaFile] | None:
        """Use a headless Chromium browser to capture the CDN video URL for a Threads post.

        Returns a list of MediaFile on success, None if playwright is unavailable or
        no video URL was intercepted (caller should fall through to image fallback).
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.debug("Threads playwright fallback: playwright not installed")
            return None

        cookies = self._load_cookies()
        if not cookies:
            logger.debug("Threads playwright fallback: no cookies — skipping")
            return None

        logger.debug("Threads playwright fallback: launching Chromium for %s", url)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    )
                )

                # Inject auth cookies at context level (supports HttpOnly sessionid)
                ctx.add_cookies([
                    {
                        "name": name,
                        "value": value,
                        "domain": ".threads.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": name == "sessionid",
                    }
                    for name, value in cookies.items()
                ])

                page = ctx.new_page()

                # Intercept CDN video requests before they fire
                video_url: list[str] = []

                def _on_request(req: object) -> None:
                    u = getattr(req, "url", "")
                    if ".mp4" in u and ("fbcdn.net" in u or "cdninstagram.com" in u):
                        if not video_url:
                            video_url.append(u)

                page.on("request", _on_request)

                try:
                    page.goto(url, wait_until="networkidle", timeout=25000)
                except Exception as e:
                    logger.debug("Threads playwright: goto timeout/error — %s", e)

                # Fallback: read src directly from <video> element
                if not video_url:
                    try:
                        src = page.evaluate(
                            "() => { const v = document.querySelector('video'); "
                            "return v ? (v.src || v.currentSrc) : null; }"
                        )
                        if src and "fbcdn.net" in src:
                            video_url.append(src)
                    except Exception:
                        pass

                browser.close()
        except Exception as e:
            logger.warning("Threads playwright fallback failed: %s", e)
            return None

        if not video_url:
            logger.debug("Threads playwright: no video URL intercepted")
            return None

        cdn_url = video_url[0]
        logger.info("Threads playwright: intercepted video URL %s…", cdn_url[:80])

        path = self._fetch_media(cdn_url, tmpdir, "video.mp4", {}, {})
        if not path:
            return None

        path = InstagramDownloader._reencode_for_telegram(path)
        return [MediaFile(path=path, media_type="video")]

    def _load_cookies(self) -> dict[str, str]:
        """Load Meta/Threads cookies from file (preferred) or browser."""
        cookies: dict[str, str] = {}

        # Cookies file takes precedence — avoids macOS TCC restrictions on browser keychain
        if self.cookies_file:
            try:
                import http.cookiejar
                jar = http.cookiejar.MozillaCookieJar(self.cookies_file)
                jar.load(ignore_discard=True, ignore_expires=True)
                for c in jar:
                    domain = getattr(c, "domain", "") or ""
                    if any(d in domain for d in ("threads", "instagram", "facebook")):
                        cookies[c.name] = c.value
                logger.debug("Threads: loaded %d cookies from %s", len(cookies), self.cookies_file)
                return cookies
            except Exception as e:
                logger.warning("Threads: failed to load cookies file %s — %s", self.cookies_file, e)

        # Fallback: extract from browser via yt-dlp (may fail if TCC blocks keychain access)
        if self.cookies_browser:
            try:
                import yt_dlp
                ydl_opts: dict = {
                    "quiet": True,
                    "cookiesfrombrowser": (self.cookies_browser, None, None, None),
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    for cookie in ydl.cookiejar:
                        domain = getattr(cookie, "domain", "") or ""
                        if any(d in domain for d in ("threads", "instagram", "facebook")):
                            cookies[cookie.name] = cookie.value
                logger.debug("Threads: loaded %d cookies from browser", len(cookies))
            except Exception as e:
                logger.warning("Threads: browser cookie extraction failed — %s", e)

        return cookies

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


def _extract_next_data_media(html: str) -> tuple[str | None, str | None]:
    """Extract video_url / image_url from Next.js __NEXT_DATA__ JSON blob."""
    import json

    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([^<]+)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return None, None

    try:
        data = json.loads(m.group(1))
    except Exception:
        return None, None

    video_url: str | None = None
    image_url: str | None = None

    # Recursively walk JSON to find CDN media URLs
    def _walk(obj: object, depth: int = 0) -> None:
        nonlocal video_url, image_url
        if depth > 20:
            return
        if isinstance(obj, dict):
            for key, val in obj.items():
                if isinstance(val, str) and _META_CDN_RE.match(val):
                    lk = key.lower()
                    if "video" in lk and not video_url:
                        video_url = val
                    elif "image" in lk or "thumbnail" in lk or "cover" in lk:
                        if not image_url:
                            image_url = val
                _walk(val, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item, depth + 1)

    _walk(data)
    return video_url, image_url
