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
