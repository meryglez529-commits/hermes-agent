import re

with open('gateway/platforms/dingtalk.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix first conflict (keep both)
content = content.replace('''<<<<<<< HEAD
        self._seen_messages.clear()
        self._access_token = None
        self._token_expires_at = 0.0
=======
        self._dedup.clear()
>>>>>>> upstream/main''', '''        self._seen_messages.clear()
        self._access_token = None
        self._token_expires_at = 0.0
        self._dedup.clear()''')

# Write back
with open('gateway/platforms/dingtalk.py', 'w', encoding='utf-8') as f:
    f.write(content)
