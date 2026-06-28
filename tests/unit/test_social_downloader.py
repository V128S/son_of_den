from claudebots.services.insta_downloader import detect_url as detect_insta_url
from claudebots.services.social_downloader import detect_platform


def test_detects_tiktok_full_url():
    url = "https://www.tiktok.com/@someuser/video/1234567890"
    result = detect_platform(f"check this out {url} lol")
    assert result is not None
    assert result[0] == url
    assert result[1] == "🎬 TikTok"


def test_detects_tiktok_vm_url():
    url = "https://vm.tiktok.com/ABCDE123/"
    result = detect_platform(url)
    assert result is not None
    assert result[1] == "🎬 TikTok"


def test_detects_twitter_url():
    url = "https://twitter.com/user/status/9876543210"
    result = detect_platform(f"look: {url}")
    assert result is not None
    assert result[0] == url
    assert result[1] == "🐦 X / Twitter"


def test_detects_x_com_url():
    url = "https://x.com/elonmusk/status/9876543210"
    result = detect_platform(url)
    assert result is not None
    assert result[1] == "🐦 X / Twitter"


def test_returns_none_for_unrelated_text():
    assert detect_platform("just a normal message") is None
    assert detect_platform("https://youtube.com/watch?v=abc") is None
    assert detect_platform("https://instagram.com/reel/ABC/") is None


def test_tiktok_takes_priority_over_twitter():
    text = "https://vm.tiktok.com/ABC https://x.com/user/status/123"
    result = detect_platform(text)
    assert result is not None
    assert result[1] == "🎬 TikTok"


def test_detects_threads_net_url():
    url = "https://www.threads.net/@someuser/post/ABC123xyz"
    result = detect_platform(f"посмотри {url}")
    assert result is not None
    assert result[0] == url
    assert result[1] == "🧵 Threads"


def test_detects_threads_com_url():
    url = "https://threads.com/@user/post/XYZ789"
    result = detect_platform(url)
    assert result is not None
    assert result[1] == "🧵 Threads"


def test_threads_not_matched_by_instagram_detector():
    url = "https://www.threads.net/@user/post/ABC"
    assert detect_insta_url(url) is None  # Threads ≠ Instagram


# ── Instagram URL detection ────────────────────────────────────────────────────

def test_detects_instagram_post():
    assert detect_insta_url("https://www.instagram.com/p/ABC123/") is not None


def test_detects_instagram_reel():
    assert detect_insta_url("https://www.instagram.com/reel/XYZ789/") is not None


def test_detects_instagram_story():
    url = "https://www.instagram.com/stories/someuser/1234567890/"
    result = detect_insta_url(url)
    assert result is not None
    assert "/stories/" in result


def test_detects_instagram_story_no_id():
    url = "https://www.instagram.com/stories/someuser/"
    result = detect_insta_url(url)
    assert result is not None


def test_detects_instagram_story_highlight():
    url = "https://www.instagram.com/stories/highlights/12345678/"
    result = detect_insta_url(url)
    assert result is not None


def test_instagram_detector_ignores_threads():
    assert detect_insta_url("https://www.threads.net/@user/post/ABC") is None


def test_instagram_detector_ignores_youtube():
    assert detect_insta_url("https://www.youtube.com/watch?v=abc") is None
