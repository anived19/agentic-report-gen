from tools.ticker_resolver import _STATIC_MAP, resolve_entity, resolve_ticker


def test_static_map_presence():
    assert _STATIC_MAP.get("reliance industries") == ("RELIANCE.NS", "Reliance Industries")
    assert _STATIC_MAP.get("tcs") == ("TCS.NS", "Tata Consultancy Services")
    assert _STATIC_MAP.get("apple") == ("AAPL", "Apple")


def test_resolve_entity_static_name():
    res = resolve_entity("tcs")
    assert len(res) == 1
    assert res[0]["ticker"] == "TCS.NS"
    assert res[0]["name"] == "Tata Consultancy Services"


def test_resolve_ticker_unresolved():
    res = resolve_ticker("NonExistentCompanyXYZ123456789")
    assert res.resolved_ticker is None
    assert res.confidence == 0.0
    assert res.method == "unresolved"
