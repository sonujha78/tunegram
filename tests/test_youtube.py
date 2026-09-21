from tunegram.sources.youtube import format_duration, is_url


def test_format_duration():
    assert format_duration(0) == "0:00"
    assert format_duration(65) == "1:05"
    assert format_duration(3661) == "1:01:01"


def test_is_url():
    assert is_url("https://youtu.be/abc")
    assert is_url("  HTTP://example.com")
    assert not is_url("tum hi ho")
