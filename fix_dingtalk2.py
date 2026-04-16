import re

with open('gateway/platforms/dingtalk.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix second conflict (replace with the working deduplication logic integrating _on_message correctly)
conflict_pattern = re.compile(r'<<<<<<< HEAD\n    def _build_inbound_envelope\(.*?\n=======\n    async def _on_message\(self, message: "ChatbotMessage"\) -> None:\n        """Process an incoming DingTalk chatbot message\."""\n        msg_id = getattr\(message, "message_id", None\) or uuid\.uuid4\(\)\.hex\n        if self\._dedup\.is_duplicate\(msg_id\):\n            logger\.debug\("\[%s\] Duplicate message %s, skipping", self\.name, msg_id\)\n            return\n>>>>>>> upstream/main', re.DOTALL)

# Since this is a complex structural change where upstream added dedup to _on_message, but HEAD has a totally new raw envelope parsing architecture:
# We should keep _build_inbound_envelope, and inject _on_message BEFORE it.

replacement = '''    async def _on_message(self, message: "ChatbotMessage") -> None:
        """Process an incoming DingTalk chatbot message."""
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)
            return
            
        envelope = self._build_inbound_envelope({}, sdk_message=message)
        await self._process_envelope(envelope)

    def _build_inbound_envelope(
        self,
        raw_payload: Dict[str, Any],
        sdk_message: Optional["ChatbotMessage"] = None,
    ) -> DingTalkInboundEnvelope:'''

content = content.replace('''<<<<<<< HEAD
    def _build_inbound_envelope(
        self,
        raw_payload: Dict[str, Any],
        sdk_message: Optional["ChatbotMessage"] = None,
    ) -> DingTalkInboundEnvelope:''', replacement)

# Clean up the bottom half of the conflict
content = content.replace('''        except (TypeError, ValueError):
            create_at_ms = None
=======
    async def _on_message(self, message: "ChatbotMessage") -> None:
        """Process an incoming DingTalk chatbot message."""
        msg_id = getattr(message, "message_id", None) or uuid.uuid4().hex
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s, skipping", self.name, msg_id)
            return
>>>>>>> upstream/main''', '''        except (TypeError, ValueError):
            create_at_ms = None''')

with open('gateway/platforms/dingtalk.py', 'w', encoding='utf-8') as f:
    f.write(content)
