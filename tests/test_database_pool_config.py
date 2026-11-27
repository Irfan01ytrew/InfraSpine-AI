from app.db import session


def test_create_engine_uses_explicit_pool_budget(monkeypatch):
    captured = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(session, "create_async_engine", fake_create_async_engine)
    result = session.create_engine("postgresql+asyncpg://synthetic/test", pool_size=1, max_overflow=0)

    assert result is not None
    assert captured["pool_size"] == 1
    assert captured["max_overflow"] == 0
    assert captured["pool_pre_ping"] is True
