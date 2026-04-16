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
        assert adapter._dedup.is_duplicate("msg-1") is False

    def test_second_same_message_is_duplicate(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._dedup.is_duplicate("msg-1")
        assert adapter._dedup.is_duplicate("msg-1") is True

    def test_different_messages_not_duplicate(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._dedup.is_duplicate("msg-1")
        assert adapter._dedup.is_duplicate("msg-2") is False

    def test_cache_cleanup_on_overflow(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        max_size = adapter._dedup._max_size
        # Fill beyond max
        for i in range(max_size + 10):
            adapter._dedup.is_duplicate(f"msg-{i}")
        # Cache should have been pruned
        assert len(adapter._dedup._seen) <= max_size + 10


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

    @pytest.mark.asyncio
    async def test_send_handles_business_errcode_in_200_response(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = '{"errcode":1234,"errmsg":"invalid"}'
        mock_response.json.return_value = {"errcode": 1234, "errmsg": "invalid"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send(
            "chat-123", "Hello!",
            metadata={"session_webhook": "https://example/webhook"}
        )
        assert result.success is False
        assert "errcode" in result.error

    @pytest.mark.asyncio
    async def test_send_accepts_sessionWebhook_metadata_alias(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        adapter = DingTalkAdapter(PlatformConfig(enabled=True))

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"
        mock_response.json = MagicMock(side_effect=ValueError("not json"))
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        adapter._http_client = mock_client

        result = await adapter.send(
            "chat-123", "Hello!",
            metadata={"sessionWebhook": "https://example/webhook-alias"}
        )
        assert result.success is True
        assert mock_client.post.call_args[0][0] == "https://example/webhook-alias"

    @pytest.mark.asyncio
    async def test_send_image_file_uploads_media_and_posts_file_message(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={"client_id": "robot-code"}))
        image_path = tmp_path / "photo.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        adapter._upload_media_file = AsyncMock(return_value="@media-file-1")
        adapter._send_payload = AsyncMock(return_value=MagicMock(success=True, message_id="img-1"))

        result = await adapter.send_image_file(
            "chat-123",
            str(image_path),
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is True
        adapter._upload_media_file.assert_awaited_once_with(str(image_path), media_type="file")
        adapter._send_payload.assert_awaited_once()
        call_args = adapter._send_payload.await_args
        assert call_args.args[0] == "chat-123"
        assert call_args.kwargs["metadata"] == {"session_webhook": "https://dingtalk.example/webhook"}
        payload = call_args.args[1]
        assert payload["msgtype"] == "file"
        assert payload["file"]["mediaId"] == "@media-file-1"
        assert payload["file"]["fileName"] == "photo.png"

    @pytest.mark.asyncio
    async def test_send_document_uploads_media_and_posts_file_message(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True, extra={"client_id": "robot-code"}))
        file_path = tmp_path / "report.pdf"
        file_path.write_bytes(b"%PDF-1.4\n%fake\n")

        adapter._upload_media_file = AsyncMock(return_value="@media-file-1")
        adapter._send_payload = AsyncMock(return_value=MagicMock(success=True, message_id="file-1"))

        result = await adapter.send_document(
            "chat-456",
            str(file_path),
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is True
        adapter._upload_media_file.assert_awaited_once_with(str(file_path), media_type="file")
        adapter._send_payload.assert_awaited_once()
        call_args = adapter._send_payload.await_args
        assert call_args.args[0] == "chat-456"
        assert call_args.kwargs["metadata"] == {"session_webhook": "https://dingtalk.example/webhook"}
        payload = call_args.args[1]
        assert payload["msgtype"] == "file"
        assert payload["file"]["mediaId"] == "@media-file-1"
        assert payload["file"]["fileName"] == "report.pdf"

    @pytest.mark.asyncio
    async def test_send_image_file_falls_back_to_markdown_when_native_send_fails(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        image_path = tmp_path / "fallback.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        adapter._upload_media_file = AsyncMock(side_effect=RuntimeError("upload failed"))
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="fallback-1"))

        result = await adapter.send_image_file(
            "chat-789",
            str(image_path),
            caption="fallback caption",
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is True
        adapter.send.assert_awaited_once()
        assert "fallback.png" in adapter.send.await_args.kwargs["content"]
        assert isinstance(result.raw_response, dict)
        assert result.raw_response.get("degraded") is True
        assert result.raw_response.get("native_kind") == "file"

    @pytest.mark.asyncio
    async def test_send_image_file_posts_single_file_payload(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        image_path = tmp_path / "single-file.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        adapter._upload_media_file = AsyncMock(return_value="@media-file-single")
        adapter._send_payload = AsyncMock(return_value=MagicMock(success=True, message_id="img-single-1"))

        result = await adapter.send_image_file(
            "chat-single",
            str(image_path),
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is True
        adapter._send_payload.assert_awaited_once()
        payload = adapter._send_payload.await_args.args[1]
        assert payload["msgtype"] == "file"
        assert payload["file"]["mediaId"] == "@media-file-single"
        assert payload["file"]["fileName"] == "single-file.png"

    @pytest.mark.asyncio
    async def test_send_document_returns_error_for_missing_file(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        result = await adapter.send_document(
            "chat-404",
            "/tmp/does-not-exist.pdf",
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_document_falls_back_to_markdown_marks_degraded(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        file_path = tmp_path / "fallback.pdf"
        file_path.write_bytes(b"%PDF-1.4\n%fake\n")

        adapter._upload_media_file = AsyncMock(side_effect=RuntimeError("upload failed"))
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="fallback-doc-1", raw_response=None))

        result = await adapter.send_document(
            "chat-doc-fallback",
            str(file_path),
            caption="fallback doc",
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is True
        adapter.send.assert_awaited_once()
        assert "fallback.pdf" in adapter.send.await_args.kwargs["content"]
        assert isinstance(result.raw_response, dict)
        assert result.raw_response.get("degraded") is True
        assert result.raw_response.get("native_kind") == "file"

    @pytest.mark.asyncio
    async def test_send_document_returns_partial_failure_when_caption_send_fails(self, tmp_path):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        file_path = tmp_path / "report2.pdf"
        file_path.write_bytes(b"%PDF-1.4\n%fake2\n")

        adapter._upload_media_file = AsyncMock(return_value="@media-file-2")
        adapter._send_payload = AsyncMock(return_value=MagicMock(success=True, message_id="file-2"))
        adapter.send = AsyncMock(return_value=MagicMock(success=False, error="caption failed"))

        result = await adapter.send_document(
            "chat-456",
            str(file_path),
            caption="caption text",
            metadata={"session_webhook": "https://dingtalk.example/webhook"},
        )

        assert result.success is False
        assert "caption failed" in (result.error or "")
        assert isinstance(result.raw_response, dict)
        assert result.raw_response.get("partial_success") is True

    @pytest.mark.asyncio
    async def test_send_image_file_logs_degraded_outcome(self, tmp_path, caplog):
        import logging
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        image_path = tmp_path / "fallback-log.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        adapter._upload_media_file = AsyncMock(side_effect=RuntimeError("upload failed"))
        adapter.send = AsyncMock(return_value=MagicMock(success=True, message_id="fallback-log-1", raw_response=None))

        with caplog.at_level(logging.INFO):
            await adapter.send_image_file(
                "chat-log-image",
                str(image_path),
                metadata={"session_webhook": "https://dingtalk.example/webhook"},
            )

        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "media_delivery platform=dingtalk kind=file outcome=degraded_markdown_fallback" in joined

    @pytest.mark.asyncio
    async def test_send_document_logs_partial_success_outcome(self, tmp_path, caplog):
        import logging
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        file_path = tmp_path / "report-log.pdf"
        file_path.write_bytes(b"%PDF-1.4\n%fake-log\n")

        adapter._upload_media_file = AsyncMock(return_value="@media-file-log")
        adapter._send_payload = AsyncMock(return_value=MagicMock(success=True, message_id="file-log-1"))
        adapter.send = AsyncMock(return_value=MagicMock(success=False, error="caption failed"))

        with caplog.at_level(logging.INFO):
            await adapter.send_document(
                "chat-log-file",
                str(file_path),
                caption="caption text",
                metadata={"session_webhook": "https://dingtalk.example/webhook"},
            )

        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "media_delivery platform=dingtalk kind=file outcome=partial_success_caption_failed" in joined


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
        adapter._dedup._seen["b"] = 1.0
        adapter._http_client = AsyncMock()
        adapter._stream_task = None

        await adapter.disconnect()
        assert len(adapter._session_webhooks) == 0
        assert len(adapter._dedup._seen) == 0
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
# Transport resilience
# ---------------------------------------------------------------------------


class TestTransportResilience:

    @pytest.mark.asyncio
    async def test_download_message_file_retries_after_timeout(self):
        import httpx
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._get_access_token = AsyncMock(return_value="tok")

        timeout = httpx.TimeoutException("timed out")
        ok_resp = MagicMock()
        ok_resp.content = b"%PDF-1.4\n"
        ok_resp.headers = {"content-type": "application/pdf"}
        ok_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[timeout, ok_resp])
        adapter._http_client = mock_client

        with patch("gateway.platforms.dingtalk.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await adapter._download_message_file("dl-retry-1")

        assert result == (b"%PDF-1.4\n", "application/pdf", None)
        assert mock_client.post.await_count == 2
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_upload_media_file_retries_after_timeout(self, tmp_path):
        import httpx
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._get_access_token = AsyncMock(return_value="tok")

        file_path = tmp_path / "retry.pdf"
        file_path.write_bytes(b"%PDF-1.4\n")

        timeout = httpx.TimeoutException("timed out")
        ok_resp = MagicMock()
        ok_resp.raise_for_status = MagicMock()
        ok_resp.json.return_value = {"errcode": 0, "media_id": "@retry-media"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[timeout, ok_resp])
        adapter._http_client = mock_client

        with patch("gateway.platforms.dingtalk.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await adapter._upload_media_file(str(file_path), media_type="file")

        assert result == "@retry-media"
        assert mock_client.post.await_count == 2
        mock_sleep.assert_awaited_once()


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
# File/document inbound handling
# ---------------------------------------------------------------------------


class TestFileMessageHandling:

    def test_build_inbound_envelope_prefers_raw_payload_for_file_message(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        raw_payload = {
            "msgtype": "file",
            "msgId": "msg-file-raw-1",
            "conversationId": "conv-file-raw-1",
            "conversationType": "1",
            "senderId": "user-file-raw-1",
            "senderNick": "Carol",
            "senderStaffId": "staff-file-raw-1",
            "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
            "createAt": 1776151837330,
            "content": {
                "fileName": "demo.pdf",
                "downloadCode": "file-code-raw-1",
            },
        }
        sdk_message = MagicMock()
        sdk_message.message_type = "text"
        sdk_message.text = None
        sdk_message.rich_text_content = None

        envelope = adapter._build_inbound_envelope(raw_payload, sdk_message=sdk_message)

        assert envelope.raw_msg_type == "file"
        assert envelope.message_id == "msg-file-raw-1"
        assert envelope.conversation_id == "conv-file-raw-1"
        assert envelope.sender_nick == "Carol"
        assert envelope.file_refs == [{
            "download_code": "file-code-raw-1",
            "filename": "demo.pdf",
            "raw_message_type": "file",
        }]

    @pytest.mark.asyncio
    async def test_process_inbound_envelope_dispatches_document_even_if_sdk_type_wrong(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        from gateway.platforms.base import MessageType

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._download_attachment = AsyncMock(return_value={
            "kind": "file",
            "local_path": "/tmp/raw-first.pdf",
            "filename": "raw-first.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 4321,
            "raw_message_type": "file",
        })

        received_events = []

        async def fake_handle(event):
            received_events.append(event)

        adapter.handle_message = fake_handle

        raw_payload = {
            "msgtype": "file",
            "msgId": "msg-file-raw-2",
            "conversationId": "conv-file-raw-2",
            "conversationType": "1",
            "senderId": "user-file-raw-2",
            "senderNick": "Dora",
            "senderStaffId": "staff-file-raw-2",
            "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=xyz",
            "createAt": 1776151837330,
            "content": {
                "fileName": "raw-first.pdf",
                "downloadCode": "file-code-raw-2",
            },
        }
        sdk_message = MagicMock()
        sdk_message.message_type = "text"
        sdk_message.text = None
        sdk_message.rich_text_content = None
        sdk_message.conversation_title = None

        envelope = adapter._build_inbound_envelope(raw_payload, sdk_message=sdk_message)

        await adapter._process_inbound_envelope(envelope)

        assert len(received_events) == 1
        event = received_events[0]
        assert event.message_type == MessageType.DOCUMENT
        assert event.raw_message == raw_payload
        assert event.media_urls == ["/tmp/raw-first.pdf"]
        assert event.media_types == ["application/pdf"]
        assert event.attachments == [{
            "kind": "file",
            "local_path": "/tmp/raw-first.pdf",
            "filename": "raw-first.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 4321,
            "raw_message_type": "file",
        }]

    @pytest.mark.asyncio
    async def test_process_inbound_envelope_logs_normalization_and_dispatch(self, caplog):
        import logging
        from gateway.platforms.dingtalk import DingTalkAdapter

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._download_attachment = AsyncMock(return_value={
            "kind": "file",
            "local_path": "/tmp/trace.pdf",
            "filename": "trace.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 99,
            "raw_message_type": "file",
        })
        adapter.handle_message = AsyncMock()

        raw_payload = {
            "msgtype": "file",
            "msgId": "msg-file-log-1",
            "conversationId": "conv-file-log-1",
            "conversationType": "1",
            "senderId": "user-file-log-1",
            "senderNick": "Eve",
            "senderStaffId": "staff-file-log-1",
            "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=log",
            "createAt": 1776151837330,
            "content": {
                "fileName": "trace.pdf",
                "downloadCode": "file-code-log-1",
            },
        }
        envelope = adapter._build_inbound_envelope(raw_payload, sdk_message=MagicMock())

        with caplog.at_level(logging.INFO):
            await adapter._process_inbound_envelope(envelope)

        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "Normalized inbound envelope" in joined
        assert "Resolved inbound attachments" in joined
        assert "Dispatching inbound event" in joined

    @pytest.mark.asyncio
    async def test_process_inbound_envelope_logs_attachment_failure_without_dropping_file_message(self, caplog):
        import logging
        from gateway.platforms.dingtalk import DingTalkAdapter
        from gateway.platforms.base import MessageType

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._download_attachment = AsyncMock(return_value=None)
        received_events = []

        async def fake_handle(event):
            received_events.append(event)

        adapter.handle_message = fake_handle

        raw_payload = {
            "msgtype": "file",
            "msgId": "msg-file-fail-1",
            "conversationId": "conv-file-fail-1",
            "conversationType": "1",
            "senderId": "user-file-fail-1",
            "senderNick": "Finn",
            "senderStaffId": "staff-file-fail-1",
            "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=fail",
            "createAt": 1776151837330,
            "content": {
                "fileName": "broken.pdf",
                "downloadCode": "file-code-fail-1",
            },
        }
        envelope = adapter._build_inbound_envelope(raw_payload, sdk_message=MagicMock())

        with caplog.at_level(logging.INFO):
            await adapter._process_inbound_envelope(envelope)

        assert len(received_events) == 1
        assert received_events[0].message_type == MessageType.DOCUMENT
        assert received_events[0].attachments == []
        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert "Attachment download failed" in joined

    def test_collect_download_codes_extracts_file_download_code_from_raw_dict(self):
        from gateway.platforms.dingtalk import DingTalkAdapter

        msg = MagicMock()
        msg.message_type = "file"
        msg.raw_dict = {"content": {"downloadCode": "file-code-1"}}

        assert DingTalkAdapter._collect_download_codes(msg) == ["file-code-1"]

    @pytest.mark.asyncio
    async def test_file_message_is_dispatched_as_document_with_attachment(self):
        from gateway.platforms.dingtalk import DingTalkAdapter
        from gateway.platforms.base import MessageType

        adapter = DingTalkAdapter(PlatformConfig(enabled=True))
        adapter._download_attachment = AsyncMock(return_value={
            "kind": "file",
            "local_path": "/tmp/demo.pdf",
            "filename": "demo.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1234,
            "raw_message_type": "file",
        })

        received_events = []

        async def fake_handle(event):
            received_events.append(event)

        adapter.handle_message = fake_handle

        msg = MagicMock()
        msg.message_id = "file-001"
        msg.message_type = "file"
        msg.text = None
        msg.rich_text_content = None
        msg.raw_dict = {"content": {"downloadCode": "file-code-1", "fileName": "demo.pdf"}}
        msg.conversation_id = "conv-file-1"
        msg.conversation_type = "1"
        msg.sender_id = "user-file-1"
        msg.sender_nick = "Carol"
        msg.sender_staff_id = ""
        msg.session_webhook = "https://hook.example/file-1"
        msg.conversation_title = None
        msg.create_at = None

        await adapter._on_message(msg)

        assert len(received_events) == 1
        event = received_events[0]
        assert event.message_type == MessageType.DOCUMENT
        assert event.media_urls == ["/tmp/demo.pdf"]
        assert event.media_types == ["application/pdf"]
        assert event.attachments == [{
            "kind": "file",
            "local_path": "/tmp/demo.pdf",
            "filename": "demo.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1234,
            "raw_message_type": "file",
        }]


# ---------------------------------------------------------------------------
# Platform enum
# ---------------------------------------------------------------------------


class TestPlatformEnum:

    def test_dingtalk_in_platform_enum(self):
        assert Platform.DINGTALK.value == "dingtalk"
