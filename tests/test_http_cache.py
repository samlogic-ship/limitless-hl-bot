"""Shared Limitless HTTP cache: dedup within TTL + stale-serve on upstream 429."""
import limitless_hl.http_cache as hc


class FakeResp:
    def __init__(self, data): self._data = data
    def raise_for_status(self): pass
    def json(self): return self._data


class FakeSession:
    def __init__(self, data, fail=False):
        self.data = data; self.fail = fail; self.calls = 0
    def get(self, url, params=None, timeout=15):
        self.calls += 1
        if self.fail:
            raise RuntimeError("429 Too Many Requests")
        return FakeResp({"page": params.get("page"), "data": self.data})


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "CACHE_DIR", tmp_path / "c")


def test_dedup_within_ttl(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    s = FakeSession("x")
    url = "https://api.limitless.exchange/markets/active"
    a = hc.cached_get_json(s, url, {"page": 1}, ttl=60)
    b = hc.cached_get_json(s, url, {"page": 1}, ttl=60)
    assert a == b
    assert s.calls == 1   # second call served from cache, no upstream hit


def test_distinct_params_not_shared(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    s = FakeSession("x")
    url = "https://api.limitless.exchange/markets/active"
    hc.cached_get_json(s, url, {"page": 1}, ttl=60)
    hc.cached_get_json(s, url, {"page": 2}, ttl=60)
    assert s.calls == 2   # different page = different cache key


def test_stale_served_on_upstream_failure(tmp_path, monkeypatch):
    import os, time
    _isolate(tmp_path, monkeypatch)
    url = "https://api.limitless.exchange/markets/active"
    good = FakeSession("fresh")
    first = hc.cached_get_json(good, url, {"page": 1}, ttl=60)
    # backdate the cache file: ttl=10 is now expired, but ttl*STALE_MAX_FACTOR(=80) still covers it
    path = hc._cache_path(url, {"page": 1})
    old = time.time() - 30
    os.utime(path, (old, old))
    bad = FakeSession("never", fail=True)
    out = hc.cached_get_json(bad, url, {"page": 1}, ttl=10)
    assert out == first
    assert bad.calls == 1   # tried upstream, failed, fell back to stale disk copy


def test_raises_when_no_cache_and_upstream_fails(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    bad = FakeSession("never", fail=True)
    try:
        hc.cached_get_json(bad, "https://x/markets/active", {"page": 99}, ttl=60)
        assert False, "should have raised"
    except RuntimeError:
        pass
