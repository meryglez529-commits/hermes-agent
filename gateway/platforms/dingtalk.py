"""
DingTalk platform adapter using Stream Mode.

Uses dingtalk-stream SDK for real-time message reception without webhooks.
Responses are sent via DingTalk's session webhook (markdown format).

Requires:
    pip install dingtalk-stream httpx
    DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET env vars

Configuration in config.yaml:
    platforms:
      dingtalk:
        enabled: true
        extra:
          client_id: "your-app-key"      # or DINGTALK_CLIENT_ID env var
          client_secret: "your-secret"   # or DINGTALK_CLIENT_SECRET env var
"""

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    import dingtalk_stream
    from dingtalk_stream import ChatbotHandler, ChatbotMessage
    DINGTALK_STREAM_AVAILABLE = True
except ImportError:
    DINGTALK_STREAM_AVAILABLE = False
    dingtalk_stream = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 20000
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

# DingTalk OpenAPI endpoints
_DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_DINGTALK_DOWNLOAD_URL = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
# Access token TTL is 7200s; refresh 5 minutes early
_TOKEN_REFRESH_BUFFER = 300


def check_dingtalk_requirements() -> bool:
    """Check if DingTalk dependencies are available and configured."""
    if not DINGTALK_STREAM_AVAILABLE or not HTTPX_AVAILABLE:
        return False
    if not os.getenv("DINGTALK_CLIENT_ID") or not os.getenv("DINGTALK_CLIENT_SECRET"):
        return False
    return True


class DingTalkAdapter(BasePlatformAdapter):
    """DingTalk chatbot adapter using Stream Mode.

    The dingtalk-stream SDK maintains a long-lived WebSocket connection.
    Incoming messages arrive via a ChatbotHandler callback. Replies are
    sent via the incoming message's session_webhook URL using httpx.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DINGTALK)

        extra = config.extra or {}
        self._client_id: str = extra.get("client_id") or os.getenv("DINGTALK_CLIENT_ID", "")
        self._client_secret: str = extra.get("client_secret") or os.getenv("DINGTALK_CLIENT_SECRET", "")

        self._stream_client: Any = None
        self._stream_task: Optional[asyncio.Task] = None
        self._http_client: Optional["httpx.AsyncClient"] = None

        # Message deduplication: msg_id -> timestamp
        self._seen_messages: Dict[str, float] = {}
        # Map chat_id -> session_webhook for reply routing
        self._session_webhooks: Dict[str, str] = {}

        # Access token cache: (token, expires_at)
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock: asyncio.Lock = asyncio.Lock()

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self) -> bool:
        """Connect to DingTalk via Stream Mode."""
        if not DINGTALK_STREAM_AVAILABLE:
            logger.warning("[%s] dingtalk-stream not installed. Run: pip install dingtalk-stream", self.name)
            return False
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx not installed. Run: pip install httpx", self.name)
            return False
        if not self._client_id or not self._client_secret:
            logger.warning("[%s] DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET required", self.name)
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0)

            credential = dingtalk_stream.Credential(self._client_id, self._client_secret)
            self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)

            handler = _IncomingHandler(self)
            self._stream_client.register_callback_handler(
                dingtalk_stream.ChatbotMessage.TOPIC, handler
            )

            self._stream_task = asyncio.create_task(self._run_stream())
            self._mark_connected()
            logger.info("[%s] Connected via Stream Mode", self.name)
            return True
        except Exception as e:
            logger.error("[%s] Failed to connect: %s", self.name, e)
            return False

    async def _run_stream(self) -> None:
        """Run the stream client with auto-reconnection."""
        backoff_idx = 0
        while self._running:
            try:
                logger.debug("[%s] Starting stream client...", self.name)
                await self._stream_client.start()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[%s] Stream client error: %s", self.name, e)

            if not self._running:
                return

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[%s] Reconnecting in %ds...", self.name, delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

    async def disconnect(self) -> None:
        """Disconnect from DingTalk."""
        self._running = False
        self._mark_disconnected()

        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._stream_client = None
        self._session_webhooks.clear()
        self._seen_messages.clear()
        self._access_token = None
        self._token_expires_at = 0.0
        logger.info("[%s] Disconnected", self.name)

    # -- Access token management --------------------------------------------

    async def _get_access_token(self) -> Optional[str]:
        """Return a valid access token, refreshing if necessary."""
        async with self._token_lock:
            now = time.time()
            if self._access_token and now < self._token_expires_at - _TOKEN_REFRESH_BUFFER:
                return self._access_token

            if not self._http_client:
                logger.error("[%s] HTTP client not initialized, cannot fetch access token", self.name)
                return None

            try:
                resp = await self._http_client.post(
                    _DINGTALK_TOKEN_URL,
                    json={"appKey": self._client_id, "appSecret": self._client_secret},
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()
                self._access_token = data.get("accessToken")
                expires_in = data.get("expireIn", 7200)
                self._token_expires_at = now + expires_in
                logger.debug("[%s] Access token refreshed, expires in %ds", self.name, expires_in)
                return self._access_token
            except Exception as e:
                logger.error("[%s] Failed to fetch access token: %s", self.name, e)
                return None

    # -- Image download -----------------------------------------------------

    async def _download_image(self, download_code: str) -> Optional[str]:
        """Download a DingTalk image by downloadCode and cache it locally.

        Returns the local file path, or None on failure.
        """
        token = await self._get_access_token()
        if not token:
            logger.warning("[%s] Cannot download image: no access token", self.name)
            return None

        if not self._http_client:
            return None

        try:
            resp = await self._http_client.post(
                _DINGTALK_DOWNLOAD_URL,
                headers={"x-acs-dingtalk-access-token": token},
                json={"downloadCode": download_code, "robotCode": self._client_id},
                timeout=30.0,
            )
            resp.raise_for_status()
            # The API returns the raw image bytes directly
            image_bytes = resp.content
            if not image_bytes:
                logger.warning("[%s] Empty image response for downloadCode %s", self.name, download_code[:20])
                return None
            # Detect extension from Content-Type header
            content_type = resp.headers.get("content-type", "image/jpeg")
            ext = _content_type_to_ext(content_type)
            cached_path = cache_image_from_bytes(image_bytes, ext=ext)
            logger.info("[%s] Cached image at %s (%d bytes)", self.name, cached_path, len(image_bytes))
            return cached_path
        except Exception as e:
            logger.error("[%s] Failed to download image (downloadCode=%s...): %s",
                         self.name, download_code[:20], e)
            return None

    # -- Inbound message processing -----------------------------------------

    async def _on_message(self, message: "ChatbotMessage") -> None:
        """Process an incoming DingTalk chatbot message."""
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        if self._is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)
            return

        # Determine message type and extract content
        raw_msg_type = getattr(message, "message_type", "text") or "text"
        text = self._extract_text(message)
        media_urls = []
        media_types = []
        msg_type = MessageType.TEXT

        if raw_msg_type == "picture":
            msg_type = MessageType.PHOTO
            download_code = self._extract_download_code(message)
            if download_code:
                cached_path = await self._download_image(download_code)
                if cached_path:
                    media_urls = [cached_path]
                    media_types = ["image/jpeg"]
                else:
                    logger.warning("[%s] Image download failed, proceeding without media", self.name)
            else:
                logger.warning("[%s] picture message missing downloadCode", self.name)
            # Images without any text are still valid events
        elif not text:
            logger.debug("[%s] Empty message (type=%s), skipping", self.name, raw_msg_type)
            return

        # Chat context
        conversation_id = getattr(message, "conversation_id", "") or ""
        conversation_type = getattr(message, "conversation_type", "1")
        is_group = str(conversation_type) == "2"
        sender_id = getattr(message, "sender_id", "") or ""
        sender_nick = getattr(message, "sender_nick", "") or sender_id
        sender_staff_id = getattr(message, "sender_staff_id", "") or ""

        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"

        # Store session webhook for reply routing
        session_webhook = getattr(message, "session_webhook", None) or ""
        if session_webhook and chat_id:
            self._session_webhooks[chat_id] = session_webhook

        source = self.build_source(
            chat_id=chat_id,
            chat_name=getattr(message, "conversation_title", None),
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_nick,
            user_id_alt=sender_staff_id if sender_staff_id else None,
        )

        # Parse timestamp
        create_at = getattr(message, "create_at", None)
        try:
            timestamp = datetime.fromtimestamp(int(create_at) / 1000, tz=timezone.utc) if create_at else datetime.now(tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=msg_id,
            raw_message=message,
            timestamp=timestamp,
            media_urls=media_urls,
            media_types=media_types,
        )

        logger.debug("[%s] Message from %s in %s: type=%s text=%s media=%d",
                      self.name, sender_nick, chat_id[:20] if chat_id else "?",
                      raw_msg_type, text[:50] if text else "(none)", len(media_urls))
        await self.handle_message(event)

    @staticmethod
    def _extract_text(message: "ChatbotMessage") -> str:
        """Extract plain text from a DingTalk chatbot message."""
        # SDK 0.24+: message.text is a TextContent object
        text_obj = getattr(message, "text", None)
        if text_obj is not None:
            content = getattr(text_obj, "content", None) or ""
            if isinstance(content, str):
                content = content.strip()
            else:
                content = str(content).strip() if content else ""
            if content:
                return content

        # richText fallback: message.rich_text_content.rich_text_list
        rich_text_obj = getattr(message, "rich_text_content", None)
        if rich_text_obj is not None:
            items = getattr(rich_text_obj, "rich_text_list", None) or []
            parts = [item["text"] for item in items
                     if isinstance(item, dict) and item.get("text")]
            content = " ".join(parts).strip()
            if content:
                return content

        return ""

    @staticmethod
    def _extract_download_code(message: "ChatbotMessage") -> Optional[str]:
        """Extract the downloadCode from a DingTalk picture message.

        SDK 0.24+: message.image_content is an ImageContent object
        with a .download_code attribute.
        """
        image_content = getattr(message, "image_content", None)
        if image_content is not None:
            code = getattr(image_content, "download_code", None)
            if code:
                return str(code)
        return None

    # -- Deduplication ------------------------------------------------------

    def _is_duplicate(self, msg_id: str) -> bool:
        """Check and record a message ID. Returns True if already seen."""
        now = time.time()
        if len(self._seen_messages) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages = {k: v for k, v in self._seen_messages.items() if v > cutoff}

        if msg_id in self._seen_messages:
            return True
        self._seen_messages[msg_id] = now
        return False

    # -- Outbound messaging -------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a markdown reply via DingTalk session webhook."""
        metadata = metadata or {}

        session_webhook = metadata.get("session_webhook") or self._session_webhooks.get(chat_id)
        if not session_webhook:
            return SendResult(success=False,
                              error="No session_webhook available. Reply must follow an incoming message.")

        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "Hermes", "text": content[:self.MAX_MESSAGE_LENGTH]},
        }

        try:
            resp = await self._http_client.post(session_webhook, json=payload, timeout=15.0)
            if resp.status_code < 300:
                return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
            body = resp.text
            logger.warning("[%s] Send failed HTTP %d: %s", self.name, resp.status_code, body[:200])
            return SendResult(success=False, error=f"HTTP {resp.status_code}: {body[:200]}")
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout sending message to DingTalk")
        except Exception as e:
            logger.error("[%s] Send error: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """DingTalk does not support typing indicators."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a DingTalk conversation."""
        return {"name": chat_id, "type": "group" if "group" in chat_id.lower() else "dm"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _content_type_to_ext(content_type: str) -> str:
    """Map a Content-Type header value to a file extension."""
    ct = content_type.lower().split(";")[0].strip()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(ct, ".jpg")


# ---------------------------------------------------------------------------
# Internal stream handler
# ---------------------------------------------------------------------------

class _IncomingHandler(ChatbotHandler if DINGTALK_STREAM_AVAILABLE else object):
    """dingtalk-stream ChatbotHandler that forwards messages to the adapter."""

    def __init__(self, adapter: DingTalkAdapter, loop: asyncio.AbstractEventLoop):
        if DINGTALK_STREAM_AVAILABLE:
            super().__init__()
        self._adapter = adapter
        self._loop = loop

    def process(self, message: "ChatbotMessage"):
        """Called by dingtalk-stream in its thread when a message arrives.

        Schedules the async handler on the main event loop.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.error("[DingTalk] Event loop unavailable, cannot dispatch message")
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        future = asyncio.run_coroutine_threadsafe(self._adapter._on_message(message), loop)
        try:
            future.result(timeout=60)
        except Exception:
            logger.exception("[DingTalk] Error processing incoming message")

        return dingtalk_stream.AckMessage.STATUS_OK, "OK"
