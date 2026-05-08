"""Tests for SoulStore LLM gateway default headers."""

from agent.soulstore_gateway import soulstore_default_headers


def test_soulstore_requires_instance_id(monkeypatch):
    monkeypatch.delenv("SOULSTORE_INSTANCE_ID", raising=False)
    assert soulstore_default_headers("https://soulstore.example.com/api/v1") is None


def test_soulstore_matching_base_url(monkeypatch):
    monkeypatch.setenv("SOULSTORE_INSTANCE_ID", "60347c0e-7de2-496d-acf2-b054d8e62bd2")
    monkeypatch.setenv("SOULSTORE_BASE_URL", "https://soulstore.ciqtek.com/api/v1/llm-gateway/v1")
    monkeypatch.setenv("SOULSTORE_SOURCE_TYPE", "openclaw")
    monkeypatch.setenv("SOULSTORE_KEY", "sk-test-key")

    base = "https://soulstore.ciqtek.com/api/v1/llm-gateway/v1"
    headers = soulstore_default_headers(base)

    assert headers == {
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-test-key",
        "X-SoulStore-Source-Type": "openclaw",
        "X-SoulStore-Instance-Id": "60347c0e-7de2-496d-acf2-b054d8e62bd2",
    }


def test_soulstore_heuristic_when_no_env_base(monkeypatch):
    monkeypatch.setenv("SOULSTORE_INSTANCE_ID", "id-1")
    monkeypatch.delenv("SOULSTORE_BASE_URL", raising=False)
    monkeypatch.delenv("SOULSTORE_KEY", raising=False)

    headers = soulstore_default_headers("https://soulstore.ciqtek.com/api/v1/llm-gateway/v1")

    assert headers is not None
    assert headers["X-SoulStore-Instance-Id"] == "id-1"
    assert headers["X-SoulStore-Source-Type"] == "openclaw"
    assert headers["Content-Type"] == "application/json"
    assert "Authorization" not in headers


def test_soulstore_mismatch_base_url_returns_none(monkeypatch):
    monkeypatch.setenv("SOULSTORE_INSTANCE_ID", "id")
    monkeypatch.setenv("SOULSTORE_BASE_URL", "https://a.example.com/v1")
    assert soulstore_default_headers("https://b.example.com/v1") is None


def test_soulstore_default_source_type(monkeypatch):
    monkeypatch.setenv("SOULSTORE_INSTANCE_ID", "x")
    monkeypatch.delenv("SOULSTORE_BASE_URL", raising=False)
    monkeypatch.delenv("SOULSTORE_SOURCE_TYPE", raising=False)
    monkeypatch.delenv("SOULSTORE_KEY", raising=False)

    headers = soulstore_default_headers("https://foo.soulstore.bar/v1")

    assert headers["X-SoulStore-Source-Type"] == "openclaw"
    assert headers["Content-Type"] == "application/json"
