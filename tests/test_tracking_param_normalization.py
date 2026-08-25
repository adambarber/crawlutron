"""Tracking-parameter normalization dedups URLs that differ only by UTM/tracking tags (#6)."""


def test_utm_params_stripped():
    from app.validation import normalize_public_http_url

    a = normalize_public_http_url("https://example.gov/page?utm_source=twitter&utm_medium=social")
    b = normalize_public_http_url("https://example.gov/page?utm_source=facebook&utm_medium=email")
    assert a == b == "https://example.gov/page"


def test_gclid_fbclid_stripped():
    from app.validation import normalize_public_http_url

    a = normalize_public_http_url("https://example.gov/p?gclid=ABC&id=5")
    b = normalize_public_http_url("https://example.gov/p?fbclid=XYZ&id=5")
    assert a == b == "https://example.gov/p?id=5"


def test_legitimate_params_preserved():
    from app.validation import normalize_public_http_url

    # 'ref' that is NOT a tracking ref, and other query keys, survive
    a = normalize_public_http_url("https://example.gov/p?category=news&page=2")
    b = normalize_public_http_url("https://example.gov/p?page=2&category=news")
    assert a == b == "https://example.gov/p?category=news&page=2"


def test_sorting_still_applied_after_strip():
    from app.validation import normalize_public_http_url

    a = normalize_public_http_url("https://example.gov/p?utm_source=x&z=1&a=2")
    assert a == "https://example.gov/p?a=2&z=1"
