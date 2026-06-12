from collections import defaultdict, deque
from typing import Any, TypedDict


class Message(TypedDict):
    role: str
    content: str


class ConversationStore:
    def __init__(self, max_messages_per_chat: int = 40) -> None:
        self._max = max_messages_per_chat
        self._store: dict[str, deque[Message]] = defaultdict(self._new_deque)

    def _new_deque(self) -> deque[Message]:
        return deque(maxlen=self._max)

    def add(self, key: str, role: str, content: str) -> None:
        self._store[key].append({"role": role, "content": content})

    def get(self, key: str) -> list[Message]:
        return list(self._store.get(key, []))

    def reset(self, key: str) -> None:
        if key in self._store:
            del self._store[key]

    def trim(self, key: str, keep_last: int) -> None:
        """Remove oldest messages, keeping at most *keep_last* entries.

        Mutates the deque in-place to avoid losing the shared reference and
        to sidestep any snapshot-and-replace race with concurrent readers.
        """
        current = self._store.get(key)
        if current is None:
            return
        while len(current) > max(keep_last, 0):
            current.popleft()

    def snapshot(self) -> dict[str, list[Message]]:
        """Serialise all conversations to a plain dict (for JSON persistence)."""
        return {k: list(v) for k, v in self._store.items() if v}

    def restore(self, data: dict[str, Any]) -> None:
        """Restore conversations from a previously saved snapshot.

        Only messages with valid role/content string fields are accepted;
        malformed entries are silently skipped to guard against corrupt files.
        """
        for key, messages in data.items():
            if not isinstance(messages, list):
                continue
            dq = self._new_deque()
            for msg in messages:
                if (
                    isinstance(msg, dict)
                    and isinstance(msg.get("role"), str)
                    and isinstance(msg.get("content"), str)
                ):
                    dq.append({"role": msg["role"], "content": msg["content"]})
            if dq:
                self._store[key] = dq
