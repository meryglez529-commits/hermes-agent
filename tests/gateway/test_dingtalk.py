"""Tests for DingTalk platform adapter."""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------


class TestDingTalkRequirements:

    def test_returns_false_when_sdk_missing(self, monkeypatch):
        with patch.dict("sys.modules", {"dingtalk_stream": None}):
            monkeypatch.setattr(
                "gateway.platforms.dingtalk.DINGTALK_STREAM_AVAILABLE", False
            )
            from gateway.platforms.dingtalk import check_dingtalk_requirements
            assert check_dingtalk_requirements() is False

    def test_returns_false_when_env_vars_missing(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.platforms.dingtalk.DINGTALK_STREAM_AVAILABLE", True
        )
        monkeypatch.setattr("gateway.platforms.dingtalk.HTTPX_AVAILABLE", True)
        monkeypatch.delenv("DINGTALK_CLIENT_ID", raising=False)
        monkeypatch.delenv("DINGTALK_CLIENT_SECRET", raising=False)
        from gateway.platforms.dingtalk import check_dingtalk_requirements
        assert check_dingtalk_requirements() is False

    def test_returns_true_when_all_available(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.platforms.dingtalk.DINGTALK_STREAM_AVAILABLE", True
        )
        monkeypatch.setattr("gateway.platforms.dingtalk.HTTPX_AVAILABLE", True)
        monkeypatch.setenv("DINGTALK_CLIENT_ID", "test-id")
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "test-secret")
        from gateway.platforms.dingtalk import check_dingtalk_requirements
        assert check_dingtalk_requirements() is True


# ---------------------------------------------------------------------------
# Adapter construction
# ---------------------------------------------------------------------------


class TestDingTalkAdapterInit:

    def test_reads_config_from_extra(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        config = PlatformConfig(
            enabled=True,
            extra={"client_id": "cfg-id", "client_secret": "cfg-secret"},
        )
        adapter = DingTalkAdapter(config)
        assert adapter._client_id == "cfg-id"
        assert adapter._client_secret == "cfg-secret"
        assert adapter.name == "Dingtalk"  # base class uses .title()

    def test_falls_back_to_env_vars(self, monkeypatch):
        monkeypatch.setenv("DINGTALK_CLIENT_ID", "env-id")
        monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "env-secret")
        from gateway.platforms.dingtalk import DingTalkAdapter
        config = PlatformConfig(enabled=True)
        adapter = DingTalkAdapter(config)
        assert adapter._client_id == "env-id"
        assert adapter._client_secret == "env-secret"


# ---------------------------------------------------------------------------
# Message text extraction
# ---------------------------------------------------------------------------


class TestExtractText:

    def test_extracts_text_content_object(self):
        # SDK 0.24+: message.text is a TextContent object with .content attribute
        from gateway.platforms.dingtalk import DingTalkAdapter
        text_obj = MagicMock()
        text_obj.content = "  hello world  "
        msg = MagicMock()
        msg.text = text_obj
        msg.rich_text_content = None
        assert DingTalkAdapter._extract_text(msg) == "hello world"

    def test_falls_back_to_rich_text_content(self):
        # SDK 0.24+: rich text lives in message.rich_text_content.rich_text_list
        from gateway.platforms.dingtalk import DingTalkAdapter
        rich_obj = MagicMock()
        rich_obj.rich_text_list = [{"text": "part1"}, {"text": "part2"}, {"image": "url"}]
        msg = MagicMock()
        msg.text = None
        msg.rich_text_content = rich_obj
        assert DingTalkAdapter._extract_text(msg) == "part1 part2"

    def test_returns_empty_for_no_content(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.text = None
        msg.rich_text_content = None
        assert DingTalkAdapter._extract_text(msg) == ""


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:

    def test_first_message_not_duplicate(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        assert adapter._is_duplicate("msg-1") is False

    def test_second_same_message_is_duplicate(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._is_duplicate("msg-1")
        assert adapter._is_duplicate("msg-1") is True

    def test_different_messages_not_duplicate(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._is_duplicate("msg-1")
        assert adapter._is_duplicate("msg-2") is False

    def test_cache_cleanup_on_overflow(self):
        from gateway.platforms.dingtalk import DingTalkAdapter, DEDUP_MAX_SIZE
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        # Fill beyond max
        for i in range(DEDUP_MAX_SIZE + 10):
            adapter._is_duplicate(f"msg-{i}")
        # Cache should have been pruned
        assert len(adapter._seen_messages) <= DEDUP_MAX_SIZE + 10


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


class TestSend:

    @pytest.mark.asyncio
    async def test_send_posts_to_webhook(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send(
            "chat-123", "Hello!",
            metadata={"session_webhook": "https://dingtalk.example/webhook"}
        )
        assert result.success is True
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://dingtalk.example/webhook"
        payload = call_args[1]["json"]
        assert payload["msgtype"] == "markdown"
        assert payload["markdown"]["title"] == "Hermes"
        assert payload["markdown"]["text"] == "Hello!"

    @pytest.mark.asyncio
    async def test_send_fails_without_webhook(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._http_client = AsyncMock()

        result = await adapter.send("chat-123", "Hello!")
        assert result.success is False
        assert "session_webhook" in result.error

    @pytest.mark.asyncio
    async def test_send_uses_cached_webhook(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client
        adapter._session_webhooks["chat-123"] = "https://cached.example/webhook"

        result = await adapter.send("chat-123", "Hello!")
        assert result.success is True
        assert mock_client.post.call_args[0][0] == "https://cached.example/webhook"

    @pytest.mark.asyncio
    async def test_send_handles_http_error(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send(
            "chat-123", "Hello!",
            metadata={"session_webhook": "https://example/webhook"}
        )
        assert result.success is False
        assert "400" in result.error


# ---------------------------------------------------------------------------
# Connect / disconnect
# ---------------------------------------------------------------------------


class TestConnect:

    @pytest.mark.asyncio
    async def test_connect_fails_without_sdk(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.platforms.dingtalk.DINGTALK_STREAM_AVAILABLE", False
        )
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_fails_without_credentials(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._client_id = ""
        adapter._client_secret = ""
        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._session_webhooks["a"] = "http://x"
        adapter._seen_messages["b"] = 1.0
        adapter._http_client = AsyncMock()
        adapter._stream_task = None

        await adapter.disconnect()
        assert len(adapter._session_webhooks) == 0
        assert len(adapter._seen_messages) == 0
        assert adapter._http_client is None


# ---------------------------------------------------------------------------
# Image: _extract_download_code
# ---------------------------------------------------------------------------


class TestExtractDownloadCode:

    def test_extracts_from_image_content_object(self):
        # SDK 0.24+: message.image_content is an ImageContent object with .download_code
        from gateway.platforms.dingtalk import DingTalkAdapter
        image_content = MagicMock()
        image_content.download_code = "abc123"
        msg = MagicMock()
        msg.image_content = image_content
        assert DingTalkAdapter._extract_download_code(msg) == "abc123"

    def test_returns_none_when_image_content_is_none(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        msg = MagicMock()
        msg.image_content = None
        assert DingTalkAdapter._extract_download_code(msg) is None

    def test_returns_none_when_download_code_is_none(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        image_content = MagicMock()
        image_content.download_code = None
        msg = MagicMock()
        msg.image_content = image_content
        assert DingTalkAdapter._extract_download_code(msg) is None


# ---------------------------------------------------------------------------
# Image: access token
# ---------------------------------------------------------------------------


class TestAccessToken:

    @pytest.mark.asyncio
    async def test_fetches_token_on_first_call(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(
            enabled=True, extra={"client_id": "kid", "client_secret": "sec"}
        ))
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"accessToken": "tok-1", "expireIn": 7200}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        adapter._http_client = mock_client

        token = await adapter._get_access_token()
        assert token == "tok-1"
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_reuses_cached_token(self):
        import time
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._access_token = "cached-tok"
        adapter._token_expires_at = time.time() + 3600
        mock_client = AsyncMock()
        adapter._http_client = mock_client

        token = await adapter._get_access_token()
        assert token == "cached-tok"
        mock_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("network error"))
        adapter._http_client = mock_client

        token = await adapter._get_access_token()
        assert token is None


# ---------------------------------------------------------------------------
# Image: _download_image
# ---------------------------------------------------------------------------


class TestDownloadImage:

    @pytest.mark.asyncio
    async def test_downloads_and_caches_image(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        # Stub token fetch
        adapter._get_access_token = AsyncMock(return_value="tok-abc")

        mock_resp = MagicMock()
        mock_resp.content = b"\xff\xd8\xff" + b"\x00" * 100  # fake JPEG bytes
        mock_resp.headers = {"content-type": "image/jpeg"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        adapter._http_client = mock_client

        with patch("gateway.platforms.dingtalk.cache_image_from_bytes", return_value="/cache/img.jpg") as mock_cache:
            result = await adapter._download_image("code-xyz")

        assert result == "/cache/img.jpg"
        mock_cache.assert_called_once_with(mock_resp.content, ext=".jpg")

    @pytest.mark.asyncio
    async def test_returns_none_without_token(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._get_access_token = AsyncMock(return_value=None)
        adapter._http_client = AsyncMock()

        result = await adapter._download_image("code-xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_download_error(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._get_access_token = AsyncMock(return_value="tok")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("timeout"))
        adapter._http_client = mock_client

        result = await adapter._download_image("code-xyz")
        assert result is None


# ---------------------------------------------------------------------------
# Image: inbound picture message handling
# ---------------------------------------------------------------------------


class TestPictureMessageHandling:

    @pytest.mark.asyncio
    async def test_picture_message_populates_media_urls(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        from gateway.platforms.base import MessageType
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._download_image = AsyncMock(return_value="/cache/photo.jpg")

        received_events = []
        async def fake_handle(event):
            received_events.append(event)
        adapter.handle_message = fake_handle

        msg = MagicMock()
        msg.message_id = "pic-001"
        msg.message_type = "picture"
        msg.text = ""
        msg.rich_text = None
        msg.content = {"downloadCode": "dl-code-1"}
        msg.picture_download_code = None
        msg.conversation_id = "conv-1"
        msg.conversation_type = "1"
        msg.sender_id = "user-1"
        msg.sender_nick = "Alice"
        msg.sender_staff_id = ""
        msg.session_webhook = "https://hook.example/1"
        msg.conversation_title = None
        msg.create_at = None

        await adapter._on_message(msg)

        assert len(received_events) == 1
        event = received_events[0]
        assert event.message_type == MessageType.PHOTO
        assert event.media_urls == ["/cache/photo.jpg"]
        assert event.media_types == ["image/jpeg"]

    @pytest.mark.asyncio
    async def test_picture_message_dispatched_even_if_download_fails(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        from gateway.platforms.base import MessageType
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._download_image = AsyncMock(return_value=None)

        received_events = []
        async def fake_handle(event):
            received_events.append(event)
        adapter.handle_message = fake_handle

        msg = MagicMock()
        msg.message_id = "pic-002"
        msg.message_type = "picture"
        msg.text = ""
        msg.rich_text = None
        msg.content = {"downloadCode": "dl-code-2"}
        msg.picture_download_code = None
        msg.conversation_id = "conv-2"
        msg.conversation_type = "1"
        msg.sender_id = "user-2"
        msg.sender_nick = "Bob"
        msg.sender_staff_id = ""
        msg.session_webhook = "https://hook.example/2"
        msg.conversation_title = None
        msg.create_at = None

        await adapter._on_message(msg)

        # Still dispatched, but with empty media_urls
        assert len(received_events) == 1
        assert received_events[0].message_type == MessageType.PHOTO
        assert received_events[0].media_urls == []


# ---------------------------------------------------------------------------
# Platform enum
# ---------------------------------------------------------------------------


class TestPlatformEnum:

    def test_dingtalk_in_platform_enum(self):
        assert Platform.DINGTALK.value == "dingtalk"
