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
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    cache_document_from_bytes,
    cache_image_from_bytes,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 20000
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
_SESSION_WEBHOOKS_MAX = 500
_DINGTALK_WEBHOOK_RE = re.compile(r'^https://(api|oapi)\.dingtalk\.com/')
_FILE_MESSAGE_TYPES = {"file", "document"}

# DingTalk OpenAPI endpoints
_DINGTALK_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
_DINGTALK_DOWNLOAD_URL = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"
_DINGTALK_MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"
_IO_RETRY_DELAYS = [1, 2]
# Access token TTL is 7200s; refresh 5 minutes early


@dataclass
class DingTalkInboundEnvelope:
    raw_payload: Dict[str, Any]
    raw_msg_type: str
    message_id: str
    conversation_id: str
    conversation_type: str
    sender_id: str
    sender_staff_id: Optional[str]
    sender_nick: Optional[str]
    session_webhook: Optional[str]
    create_at_ms: Optional[int]
    text: str
    image_refs: List[Dict[str, Any]]
    file_refs: List[Dict[str, Any]]
    sdk_message: Any = None


@dataclass
class ResolvedAttachment:
    kind: str
    local_path: str
    filename: Optional[str]
    mime_type: Optional[str]
    size_bytes: Optional[int]
    download_code: Optional[str]
    raw_message_type: Optional[str]


def _extract_dingtalk_file_download_url(body: bytes) -> Optional[str]:
    """Parse JSON from messageFiles/download when the API returns a downloadUrl instead of raw bytes."""
    if not body or not body.lstrip().startswith(b"{"):
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    url = payload.get("downloadUrl") or payload.get("download_url")
    if url:
        return str(url).strip()
    for key in ("result", "data", "body"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            url = inner.get("downloadUrl") or inner.get("download_url")
            if url:
                return str(url).strip()
    result = payload.get("result")
    if isinstance(result, dict):
        url = result.get("downloadUrl") or result.get("download_url")
        if url:
            return str(url).strip()
    return None


def _get_existing_attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a real attribute without triggering MagicMock auto-attribute creation."""
    try:
        return object.__getattribute__(obj, name)
    except AttributeError:
        return default
    except Exception:
        return getattr(obj, name, default)


def _extract_message_content_dict(message: Any) -> Dict[str, Any]:
    """Extract a content dict from SDK fields or raw callback payloads."""
    candidates: List[Any] = []
    content = _get_existing_attr(message, "content", None)
    if content is not None:
        candidates.append(content)
    raw_dict = _get_existing_attr(message, "raw_dict", None)
    if isinstance(raw_dict, dict):
        candidates.append(raw_dict.get("content"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return {}


def _extract_filename_from_headers(headers: Any) -> Optional[str]:
    """Best-effort parse of a filename from Content-Disposition."""
    if not headers:
        return None
    content_disposition = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    if not content_disposition:
        return None
    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.IGNORECASE)
    if match:
        return os.path.basename(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', content_disposition, flags=re.IGNORECASE)
    if match:
        return os.path.basename(match.group(1).strip())
    return None


def _sanitize_filename(filename: Optional[str], default_stem: str = "attachment") -> str:
    """Collapse a user-supplied filename to a safe basename."""
    safe_name = os.path.basename((filename or "").replace("\x00", "").strip())
    safe_name = re.sub(r"[\r\n\t]+", "_", safe_name)
    if not safe_name or safe_name in {".", ".."}:
        safe_name = default_stem
    return safe_name


def _classify_dingtalk_error(exc: Exception) -> str:
    """Return a coarse error category for DingTalk transport logs."""
    text = str(exc).lower()
    if isinstance(exc, asyncio.TimeoutError) or "timeout" in text or "timed out" in text:
        return "timeout"
    if "ssl" in text and "eof" in text:
        return "ssl_eof"
    if "connect" in text or "connection" in text:
        return "connection"
    return "error"


def _ext_to_media_type(path: str) -> str:
    """Infer image/* MIME from cached file path."""
    ext = os.path.splitext(path)[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
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
            # Suppress dingtalk_stream SDK noisy reconnection logs globally
            logging.getLogger("dingtalk_stream").setLevel(logging.CRITICAL)
            logging.getLogger("dingtalk_stream.client").setLevel(logging.CRITICAL)

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

    async def _retry_http_call(self, operation_name: str, func):
        """Retry selected timeout-prone HTTP operations with small backoff."""
        attempts = len(_IO_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                return await func()
            except httpx.TimeoutException as e:
                if attempt >= attempts - 1:
                    raise
                delay = _IO_RETRY_DELAYS[attempt]
                logger.warning(
                    "[%s] %s timeout, retrying in %ss (%d/%d)",
                    self.name,
                    operation_name,
                    delay,
                    attempt + 1,
                    attempts - 1,
                )
                await asyncio.sleep(delay)

    # -- Attachment download ------------------------------------------------

    async def _download_message_file(self, download_code: str) -> Optional[tuple[bytes, str, Optional[str]]]:
        """Download a DingTalk message file and return bytes, MIME type, and filename."""
        token = await self._get_access_token()
        if not token:
            logger.warning("[%s] Cannot download message file: no access token", self.name)
            return None

        if not self._http_client:
            return None

        try:
            resp = await self._retry_http_call(
                "attachment download",
                lambda: self._http_client.post(
                    _DINGTALK_DOWNLOAD_URL,
                    headers={"x-acs-dingtalk-access-token": token},
                    json={"downloadCode": download_code, "robotCode": self._client_id},
                    timeout=30.0,
                ),
            )
            resp.raise_for_status()
            body = resp.content
            if not body:
                logger.warning("[%s] Empty file response for downloadCode %s", self.name, download_code[:20])
                return None

            remote_url = _extract_dingtalk_file_download_url(body)
            if remote_url:
                try:
                    resp2 = await self._http_client.get(
                        remote_url,
                        timeout=60.0,
                        follow_redirects=True,
                    )
                    resp2.raise_for_status()
                    return (
                        resp2.content,
                        resp2.headers.get("content-type", "application/octet-stream"),
                        _extract_filename_from_headers(resp2.headers),
                    )
                except Exception as e:
                    logger.error("[%s] Failed to GET downloadUrl for file: %s", self.name, e)
                    return None

            if body.lstrip().startswith(b"{"):
                try:
                    jd = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    jd = None
                logger.error(
                    "[%s] messageFiles/download returned JSON without a usable downloadUrl: %s",
                    self.name,
                    str(jd)[:800] if jd is not None else body[:400],
                )
                return None

            return (
                body,
                resp.headers.get("content-type", "application/octet-stream"),
                _extract_filename_from_headers(resp.headers),
            )
        except Exception as e:
            category = _classify_dingtalk_error(e)
            logger.error("[%s] Failed to download message file (downloadCode=%s..., category=%s): %s",
                         self.name, download_code[:20], category, e)
            return None

    async def _download_attachment(
        self,
        download_code: str,
        raw_message_type: str,
        fallback_filename: Optional[str] = None,
    ) -> Optional[ResolvedAttachment]:
        """Download a non-image attachment and store it in the document cache."""
        payload = await self._download_message_file(download_code)
        if not payload:
            return None

        data, content_type, remote_filename = payload
        normalized_content_type = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower() or "application/octet-stream"
        filename = _sanitize_filename(
            fallback_filename or remote_filename,
            default_stem="dingtalk_attachment",
        )
        stem, ext = os.path.splitext(filename)
        if not ext:
            guessed_ext = mimetypes.guess_extension(normalized_content_type) or ".bin"
            filename = f"{stem}{guessed_ext}"

        try:
            local_path = cache_document_from_bytes(data, filename)
        except ValueError as e:
            logger.error("[%s] Failed to cache attachment %s: %s", self.name, filename, e)
            return None

        return ResolvedAttachment(
            kind="file",
            local_path=local_path,
            filename=filename,
            mime_type=normalized_content_type,
            size_bytes=len(data),
            download_code=download_code,
            raw_message_type=raw_message_type,
        )

    # -- Image download -----------------------------------------------------

    async def _download_image(self, download_code: str) -> Optional[str]:
        """Download a DingTalk image by downloadCode and cache it locally.

        Returns the local file path, or None on failure.
        """
        payload = await self._download_message_file(download_code)
        if not payload:
            return None

        image_bytes, content_type, _filename = payload
        normalized_content_type = (content_type or "").split(";", 1)[0].strip().lower()
        if not normalized_content_type.startswith("image/"):
            logger.warning("[%s] Refusing to treat non-image payload as image: %s", self.name, normalized_content_type or "(missing)")
            return None
        if not image_bytes:
            logger.warning("[%s] Empty image bytes after download for %s", self.name, download_code[:20])
            return None

        ext = _content_type_to_ext(normalized_content_type)
        try:
            cached_path = cache_image_from_bytes(image_bytes, ext=ext)
        except ValueError as e:
            logger.error("[%s] Downloaded payload is not a valid image (%s): %s", self.name, ext, e)
            return None
        logger.info("[%s] Cached image at %s (%d bytes)", self.name, cached_path, len(image_bytes))
        return cached_path

    # -- Inbound message processing -----------------------------------------

    def _build_inbound_envelope(
        self,
        raw_payload: Dict[str, Any],
        sdk_message: Optional["ChatbotMessage"] = None,
    ) -> DingTalkInboundEnvelope:
        """Build a stable raw-first envelope from DingTalk callback data."""
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        raw_msg_type = str(payload.get("msgtype") or getattr(sdk_message, "message_type", "text") or "text").strip() or "text"
        message_id = str(
            payload.get("msgId")
            or payload.get("msg_id")
            or getattr(sdk_message, "message_id", None)
            or uuid.uuid4().hex
        )
        conversation_id = str(
            payload.get("conversationId")
            or payload.get("conversation_id")
            or getattr(sdk_message, "conversation_id", "")
            or ""
        )
        conversation_type = str(
            payload.get("conversationType")
            or payload.get("conversation_type")
            or getattr(sdk_message, "conversation_type", "1")
            or "1"
        )
        sender_id = str(
            payload.get("senderId")
            or payload.get("sender_id")
            or getattr(sdk_message, "sender_id", "")
            or ""
        )
        sender_staff_id = (
            str(
                payload.get("senderStaffId")
                or payload.get("sender_staff_id")
                or getattr(sdk_message, "sender_staff_id", "")
                or ""
            ).strip()
            or None
        )
        sender_nick = (
            str(
                payload.get("senderNick")
                or payload.get("sender_nick")
                or getattr(sdk_message, "sender_nick", "")
                or ""
            ).strip()
            or None
        )
        session_webhook = (
            str(
                payload.get("sessionWebhook")
                or payload.get("session_webhook")
                or getattr(sdk_message, "session_webhook", "")
                or ""
            ).strip()
            or None
        )
        create_at_value = payload.get("createAt")
        if create_at_value is None:
            create_at_value = payload.get("create_at")
        if create_at_value is None:
            create_at_value = getattr(sdk_message, "create_at", None)
        try:
            create_at_ms = int(create_at_value) if create_at_value is not None else None
        except (TypeError, ValueError):
            create_at_ms = None

        return DingTalkInboundEnvelope(
            raw_payload=payload,
            raw_msg_type=raw_msg_type,
            message_id=message_id,
            conversation_id=conversation_id,
            conversation_type=conversation_type,
            sender_id=sender_id,
            sender_staff_id=sender_staff_id,
            sender_nick=sender_nick,
            session_webhook=session_webhook,
            create_at_ms=create_at_ms,
            text=self._extract_text_from_payload(payload, sdk_message=sdk_message),
            image_refs=self._collect_image_refs(payload, sdk_message=sdk_message),
            file_refs=self._collect_file_refs(payload, sdk_message=sdk_message),
            sdk_message=sdk_message,
        )

    def _extract_text_from_payload(
        self,
        raw_payload: Dict[str, Any],
        sdk_message: Optional["ChatbotMessage"] = None,
    ) -> str:
        """Extract text using raw payload first, then SDK fallbacks."""
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        msg_type = str(payload.get("msgtype") or "").strip().lower()

        content = payload.get("content")
        if msg_type == "text":
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, dict):
                maybe_text = content.get("content") or content.get("text")
                if isinstance(maybe_text, str) and maybe_text.strip():
                    return maybe_text.strip()
            text_block = payload.get("text")
            if isinstance(text_block, dict):
                maybe_text = text_block.get("content")
                if isinstance(maybe_text, str) and maybe_text.strip():
                    return maybe_text.strip()

        return (self._extract_text(sdk_message) if sdk_message is not None else "").strip()

    def _collect_image_refs(
        self,
        raw_payload: Dict[str, Any],
        sdk_message: Optional["ChatbotMessage"] = None,
    ) -> List[Dict[str, Any]]:
        """Collect normalized image refs from raw payload or SDK fallback."""
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        raw_msg_type = str(payload.get("msgtype") or getattr(sdk_message, "message_type", "text") or "text").strip() or "text"
        refs: List[Dict[str, Any]] = []

        if raw_msg_type == "picture":
            code = None
            content = payload.get("content")
            if isinstance(content, dict):
                code = content.get("downloadCode") or content.get("download_code")
            if not code and sdk_message is not None:
                code = self._extract_download_code(sdk_message)
            if code:
                refs.append({"download_code": str(code), "raw_message_type": raw_msg_type})
            return refs

        if raw_msg_type == "richText" and sdk_message is not None:
            try:
                image_codes = sdk_message.get_image_list()
            except Exception:
                image_codes = None
            if image_codes:
                for code in image_codes:
                    if code:
                        refs.append({"download_code": str(code), "raw_message_type": raw_msg_type})
        return refs

    def _collect_file_refs(
        self,
        raw_payload: Dict[str, Any],
        sdk_message: Optional["ChatbotMessage"] = None,
    ) -> List[Dict[str, Any]]:
        """Collect normalized file refs from raw payload or SDK fallback."""
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        raw_msg_type = str(payload.get("msgtype") or getattr(sdk_message, "message_type", "text") or "text").strip() or "text"
        if raw_msg_type not in _FILE_MESSAGE_TYPES:
            return []

        content = payload.get("content")
        if not isinstance(content, dict) and sdk_message is not None:
            content = _extract_message_content_dict(sdk_message)
        if not isinstance(content, dict):
            return []

        code = content.get("downloadCode") or content.get("download_code")
        if not code:
            return []

        filename = None
        for key in ("fileName", "file_name", "filename", "name", "title"):
            value = content.get(key)
            if value:
                filename = str(value)
                break

        return [{
            "download_code": str(code),
            "filename": filename,
            "raw_message_type": raw_msg_type,
        }]

    @staticmethod
    def _coerce_resolved_attachment(item: Any, fallback_download_code: Optional[str] = None) -> Optional[ResolvedAttachment]:
        """Normalize legacy dict or dataclass attachment results into ResolvedAttachment."""
        if item is None:
            return None
        if isinstance(item, ResolvedAttachment):
            return item
        if isinstance(item, dict):
            return ResolvedAttachment(
                kind=str(item.get("kind") or "file"),
                local_path=str(item.get("local_path") or ""),
                filename=item.get("filename"),
                mime_type=item.get("mime_type"),
                size_bytes=item.get("size_bytes"),
                download_code=item.get("download_code") or fallback_download_code,
                raw_message_type=item.get("raw_message_type"),
            )
        return None

    async def _resolve_attachments(
        self,
        envelope: DingTalkInboundEnvelope,
    ) -> tuple[List[ResolvedAttachment], List[str], List[str]]:
        """Resolve image/file refs into cached attachments and image media lists."""
        resolved: List[ResolvedAttachment] = []
        media_urls: List[str] = []
        media_types: List[str] = []

        image_codes = [ref["download_code"] for ref in envelope.image_refs if ref.get("download_code")]
        if envelope.raw_msg_type in ("picture", "richText") and image_codes:
            paths = await asyncio.gather(*[self._download_image(code) for code in image_codes])
            for code, path in zip(image_codes, paths):
                if not path:
                    continue
                media_urls.append(path)
                media_type = _ext_to_media_type(path)
                media_types.append(media_type)
                resolved.append(
                    ResolvedAttachment(
                        kind="image",
                        local_path=path,
                        filename=os.path.basename(path),
                        mime_type=media_type,
                        size_bytes=None,
                        download_code=code,
                        raw_message_type=envelope.raw_msg_type,
                    )
                )
            if image_codes and not media_urls:
                logger.warning(
                    "[%s] Image download failed for %d file(s), proceeding without media",
                    self.name,
                    len(image_codes),
                )

        file_refs = [ref for ref in envelope.file_refs if ref.get("download_code")]
        if envelope.raw_msg_type in _FILE_MESSAGE_TYPES and file_refs:
            downloaded = await asyncio.gather(
                *[
                    self._download_attachment(
                        ref["download_code"],
                        envelope.raw_msg_type,
                        fallback_filename=ref.get("filename"),
                    )
                    for ref in file_refs
                ]
            )
            file_attachments = [
                coerced
                for ref, item in zip(file_refs, downloaded)
                for coerced in [self._coerce_resolved_attachment(item, fallback_download_code=ref.get("download_code"))]
                if coerced is not None
            ]
            resolved.extend(file_attachments)
            for item in file_attachments:
                if item.local_path:
                    media_urls.append(item.local_path)
                    media_types.append(item.mime_type or "application/octet-stream")
            if file_refs and not file_attachments:
                logger.warning(
                    "[%s] Attachment download failed for %d file(s), proceeding without attachments",
                    self.name,
                    len(file_refs),
                )

        logger.info(
            "[%s] Resolved inbound attachments: msg_id=%s total=%d media=%d files=%d",
            self.name,
            envelope.message_id,
            len(resolved),
            len(media_urls),
            len([item for item in resolved if item.kind == "file"]),
        )
        return resolved, media_urls, media_types

    def _derive_message_type(
        self,
        envelope: DingTalkInboundEnvelope,
        resolved_attachments: List[ResolvedAttachment],
        text: str,
        media_urls: List[str],
    ) -> MessageType:
        """Choose the normalized Hermes message type from resolved state."""
        if any(item.kind == "file" for item in resolved_attachments) or envelope.raw_msg_type in _FILE_MESSAGE_TYPES:
            return MessageType.DOCUMENT
        if envelope.raw_msg_type == "picture":
            return MessageType.PHOTO
        if media_urls:
            if not text:
                return MessageType.PHOTO
            return MessageType.TEXT
        return MessageType.TEXT

    def _build_message_event(
        self,
        envelope: DingTalkInboundEnvelope,
        resolved_attachments: List[ResolvedAttachment],
        media_urls: List[str],
        media_types: List[str],
    ) -> MessageEvent:
        """Build the final MessageEvent from normalized envelope state."""
        text = (envelope.text or "").strip()
        conversation_id = envelope.conversation_id
        conversation_type = envelope.conversation_type or "1"
        is_group = str(conversation_type) == "2"
        sender_id = envelope.sender_id or ""
        sender_nick = envelope.sender_nick or sender_id
        sender_staff_id = envelope.sender_staff_id or ""

        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"

        session_webhook = envelope.session_webhook or ""
        if session_webhook and chat_id and _DINGTALK_WEBHOOK_RE.match(session_webhook):
            if len(self._session_webhooks) >= _SESSION_WEBHOOKS_MAX:
                try:
                    self._session_webhooks.pop(next(iter(self._session_webhooks)))
                except StopIteration:
                    pass
            self._session_webhooks[chat_id] = session_webhook

        source = self.build_source(
            chat_id=chat_id,
            chat_name=getattr(envelope.sdk_message, "conversation_title", None),
            chat_type=chat_type,
            user_id=sender_id,
            user_name=sender_nick,
            user_id_alt=sender_staff_id if sender_staff_id else None,
        )

        try:
            timestamp = (
                datetime.fromtimestamp(int(envelope.create_at_ms) / 1000, tz=timezone.utc)
                if envelope.create_at_ms
                else datetime.now(tz=timezone.utc)
            )
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        message_type = self._derive_message_type(envelope, resolved_attachments, text, media_urls)
        attachment_dicts = [
            {
                "kind": item.kind,
                "local_path": item.local_path,
                "filename": item.filename,
                "mime_type": item.mime_type,
                "size_bytes": item.size_bytes,
                "raw_message_type": item.raw_message_type,
            }
            for item in resolved_attachments
            if item.kind == "file"
        ]

        return MessageEvent(
            text=text,
            message_type=message_type,
            source=source,
            message_id=envelope.message_id,
            raw_message=envelope.raw_payload,
            timestamp=timestamp,
            media_urls=media_urls,
            media_types=media_types,
            attachments=attachment_dicts,
        )

    async def _process_inbound_envelope(self, envelope: DingTalkInboundEnvelope) -> None:
        """Process a normalized DingTalk envelope into a MessageEvent."""
        if self._is_duplicate(envelope.message_id):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, envelope.message_id)
            return

        logger.info(
            "[%s] Normalized inbound envelope: msg_id=%s type=%s text_len=%d image_refs=%d file_refs=%d",
            self.name,
            envelope.message_id,
            envelope.raw_msg_type,
            len(envelope.text or ""),
            len(envelope.image_refs),
            len(envelope.file_refs),
        )

        raw_msg_type = envelope.raw_msg_type
        text = (envelope.text or "").strip()
        resolved_attachments, media_urls, media_types = await self._resolve_attachments(envelope)

        if not text and not media_urls and not resolved_attachments:
            if raw_msg_type == "picture" and envelope.image_refs:
                pass
            elif raw_msg_type in _FILE_MESSAGE_TYPES and envelope.file_refs:
                pass
            else:
                logger.debug("[%s] Empty message (type=%s), skipping", self.name, raw_msg_type)
                return

        event = self._build_message_event(envelope, resolved_attachments, media_urls, media_types)
        logger.info(
            "[%s] Dispatching inbound event: msg_id=%s type=%s media=%d attachments=%d",
            self.name,
            event.message_id,
            event.message_type.value,
            len(event.media_urls),
            len(event.attachments),
        )
        await self.handle_message(event)

    async def _on_message(self, message: "ChatbotMessage") -> None:
        """Compatibility wrapper that builds a raw-first envelope from an SDK message."""
        raw_payload = _get_existing_attr(message, "raw_dict", None)
        if not isinstance(raw_payload, dict):
            raw_payload = {}
        envelope = self._build_inbound_envelope(raw_payload, sdk_message=message)
        await self._process_inbound_envelope(envelope)

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
        image_content = _get_existing_attr(message, "image_content", None)
        if image_content is not None:
            code = _get_existing_attr(image_content, "download_code", None)
            if code:
                return str(code)
        content = _extract_message_content_dict(message)
        code = content.get("downloadCode") or content.get("download_code")
        if code:
            return str(code)
        return None

    @staticmethod
    def _extract_attachment_filename(message: "ChatbotMessage") -> Optional[str]:
        """Extract a human-readable filename from a DingTalk file payload."""
        content = _extract_message_content_dict(message)
        for key in ("fileName", "file_name", "filename", "name", "title"):
            value = content.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _collect_download_codes(message: "ChatbotMessage") -> List[str]:
        """Collect file downloadCode list for picture and richText (inline images) messages."""
        raw = getattr(message, "message_type", "text") or "text"
        out: List[str] = []
        if raw == "picture":
            code = DingTalkAdapter._extract_download_code(message)
            if code:
                out.append(code)
            return out
        if raw in _FILE_MESSAGE_TYPES:
            content = _extract_message_content_dict(message)
            code = content.get("downloadCode") or content.get("download_code")
            if code:
                out.append(str(code))
            return out
        if raw == "richText":
            try:
                lst = message.get_image_list()
            except Exception:
                lst = None
            if lst:
                for c in lst:
                    if c:
                        out.append(str(c))
            return out
        return out

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

    async def _send_payload(
        self,
        chat_id: str,
        payload: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a prebuilt payload to the session webhook with retry handling."""
        metadata = metadata or {}

        session_webhook = metadata.get("session_webhook") or self._session_webhooks.get(chat_id)
        if not session_webhook:
            return SendResult(
                success=False,
                error="No session_webhook available. Reply must follow an incoming message.",
            )

        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        # Suppress dingtalk_stream's noisy ping timeout / SSL EOF warnings
        logging.getLogger("dingtalk_stream").setLevel(logging.CRITICAL)
        logging.getLogger("dingtalk_stream.client").setLevel(logging.CRITICAL)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await self._http_client.post(session_webhook, json=payload, timeout=20.0)
                if resp.status_code < 300:
                    return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
                body = resp.text
                logger.warning("[%s] Send failed HTTP %d: %s", self.name, resp.status_code, body[:200])
                return SendResult(success=False, error=f"HTTP {resp.status_code}: {body[:200]}")
            except httpx.TimeoutException:
                if attempt < max_retries - 1:
                    logger.warning("[%s] Timeout sending to DingTalk. Retrying %d/%d...",
                                   self.name, attempt + 1, max_retries)
                    await asyncio.sleep(2)
                    continue
                return SendResult(success=False, error="Timeout sending message to DingTalk")
            except Exception as e:
                logger.error("[%s] Send error: %s", self.name, e)
                return SendResult(success=False, error=str(e))

        return SendResult(success=False, error="Max retries exceeded sending to DingTalk")

    async def _send_markdown(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send the existing markdown payload format."""
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "Hermes", "text": content[:self.MAX_MESSAGE_LENGTH]},
        }
        return await self._send_payload(chat_id, payload, metadata=metadata)

    async def _upload_media_file(self, file_path: str, media_type: str) -> str:
        """Upload a local file to DingTalk and return its media_id."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {file_path}")

        token = await self._get_access_token()
        if not token:
            raise RuntimeError("Unable to obtain DingTalk access token for media upload")
        if not self._http_client:
            raise RuntimeError("HTTP client not initialized")

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files = {
            "media": (path.name, path.read_bytes(), content_type),
        }
        resp = await self._retry_http_call(
            "media upload",
            lambda: self._http_client.post(
                f"{_DINGTALK_MEDIA_UPLOAD_URL}?access_token={token}&type={media_type}",
                files=files,
                timeout=30.0,
            ),
        )
        resp.raise_for_status()
        body = resp.json()
        media_id = body.get("media_id")
        if body.get("errcode") not in (None, 0) or not media_id:
            raise RuntimeError(f"media upload failed: {body}")
        return str(media_id)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a markdown reply via DingTalk session webhook."""
        return await self._send_markdown(chat_id, content, metadata=metadata)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local image file as a native DingTalk image message."""
        try:
            media_id = await self._upload_media_file(image_path, media_type="image")
            return await self._send_payload(
                chat_id,
                {
                    "msgtype": "image",
                    "image": {"media_id": media_id},
                },
                metadata=metadata,
            )
        except FileNotFoundError as e:
            return SendResult(success=False, error=str(e))
        except Exception as e:
            logger.warning("[%s] Native image send failed, falling back to markdown: %s", self.name, e)
            fallback = f"{caption}\n🖼️ Image: {image_path}" if caption else f"🖼️ Image: {image_path}"
            return await self.send(
                chat_id=chat_id,
                content=fallback,
                reply_to=reply_to,
                metadata=metadata,
            )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local file as a native DingTalk file message."""
        path = Path(file_path)
        if not path.is_file():
            return SendResult(success=False, error=f"File not found: {file_path}")

        try:
            media_id = await self._upload_media_file(str(path), media_type="file")
            result = await self._send_payload(
                chat_id,
                {
                    "msgtype": "file",
                    "file": {"media_id": media_id},
                },
                metadata=metadata,
            )
            if caption:
                await self.send(
                    chat_id=chat_id,
                    content=caption,
                    reply_to=reply_to,
                    metadata=metadata,
                )
            return result
        except Exception as e:
            logger.warning("[%s] Native file send failed, falling back to markdown: %s", self.name, e)
            display_name = file_name or path.name
            fallback = f"{caption}\n📎 File: {display_name}" if caption else f"📎 File: {display_name}"
            return await self.send(
                chat_id=chat_id,
                content=fallback,
                reply_to=reply_to,
                metadata=metadata,
            )

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
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "text/markdown": ".md",
    }.get(ct, ".bin")


# ---------------------------------------------------------------------------
# Internal stream handler
# ---------------------------------------------------------------------------

class _IncomingHandler(ChatbotHandler if DINGTALK_STREAM_AVAILABLE else object):
    """dingtalk-stream ChatbotHandler that forwards messages to the adapter.

    SDK 0.24+: process() must be async — the SDK awaits it directly in its
    own event loop, so no thread-bridging is needed.
    """

    def __init__(self, adapter: DingTalkAdapter):
        if DINGTALK_STREAM_AVAILABLE:
            super().__init__()
        self._adapter = adapter

    async def process(self, callback_message):
        """Called by dingtalk-stream when a message arrives.

        SDK 0.24+: receives a CallbackMessage; parse .data into
        ChatbotMessage before dispatching.
        """
        try:
            raw_data = getattr(callback_message, "data", None)
            if raw_data is None:
                logger.warning("[DingTalk] Incoming callback missing data payload")
                raw_data = {}
            else:
                try:
                    preview = json.dumps(raw_data, ensure_ascii=False, default=str)
                except Exception:
                    preview = str(raw_data)
                logger.info("[DingTalk] Raw callback payload: %s", preview[:2000])
            incoming = dingtalk_stream.ChatbotMessage.from_dict(raw_data)
            envelope = self._adapter._build_inbound_envelope(raw_data, sdk_message=incoming)
            await self._adapter._process_inbound_envelope(envelope)
        except Exception:
            logger.exception("[DingTalk] Error processing incoming message")
        return dingtalk_stream.AckMessage.STATUS_OK, "OK"
