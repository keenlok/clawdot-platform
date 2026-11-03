from chatcore_browser_renderer.blocking import detect_block


def test_short_challenge_markers_are_detected() -> None:
    for marker in (
        "verify you are human",
        "are you a robot",
        "captcha",
        "just a moment",
        "attention required",
        "access denied",
        "request unsuccessful",
        "pardon our interruption",
        "unusual traffic",
        "checking your browser",
        "enable javascript and cookies to continue",
    ):
        assert detect_block(200, "", f"Page says: {marker}", [])


def test_challenge_frame_is_detected_even_with_long_body() -> None:
    assert detect_block(200, "", "x" * 2000, ["https://challenges.cloudflare.com/abc"])


def test_known_challenge_frames_are_detected() -> None:
    assert detect_block(200, "", "normal page", ["https://www.google.com/recaptcha/api2/anchor"])
    assert detect_block(200, "", "normal page", ["https://www.google.com/recaptcha/api2/bframe"])
    assert detect_block(200, "", "normal page", ["https://hcaptcha.com/captcha/v1/challenge"])
    assert detect_block(
        200, "", "normal page", ["https://challenges.cloudflare.com/cdn-cgi/challenge"]
    )
    assert detect_block(200, "", "normal page", ["https://awswaf.com/challenge/"])
    assert detect_block(200, "", "normal page", ["https://captcha-delivery.com/captcha/"])


def test_passive_bootstrap_scripts_do_not_match_challenge_frames() -> None:
    scripts = ["https://www.google.com/recaptcha/api.js", "https://hcaptcha.com/1/api.js"]
    assert not detect_block(200, "Article", "useful content " * 200, scripts)


def test_passive_bootstrap_scripts_with_short_body_need_block_marker() -> None:
    scripts = ["https://www.google.com/recaptcha/api.js", "https://hcaptcha.com/1/api.js"]
    assert not detect_block(200, "Article", "ordinary short content", scripts)
    assert detect_block(200, "Access denied", "verify you are human", scripts)


def test_arbitrary_recaptcha_path_and_main_document_are_not_challenges() -> None:
    assert not detect_block(200, "", "normal page", ["https://example.com/recaptcha-history"])
    assert not detect_block(200, "", "normal page", [])


def test_article_mentioning_captcha_is_not_blocked() -> None:
    assert not detect_block(200, "A history of captcha", "captcha " + "content " * 300, [])


def test_plain_not_found_is_not_a_block() -> None:
    assert not detect_block(404, "Not Found", "missing", [])
