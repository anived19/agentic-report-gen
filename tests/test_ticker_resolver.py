from tools.ticker_resolver import _STATIC_MAP, resolve_ticker


def test_static_map_presence():
    assert _STATIC_MAP.get("reliance industries") == "RELIANCE.NS"
    assert _STATIC_MAP.get("tcs") == "TCS.NS"
    assert _STATIC_MAP.get("apple") == "AAPL"


def test_resolve_ticker_unresolved():
    res = resolve_ticker("NonExistentCompanyXYZ123456789")
    assert res.resolved_ticker is None
    assert res.confidence == 0.0
    assert res.method == "unresolved"
