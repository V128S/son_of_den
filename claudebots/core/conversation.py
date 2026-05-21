from collections import defaultdict, deque
from typing import TypedDict


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
