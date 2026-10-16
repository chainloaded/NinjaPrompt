"""Chat persistence inside the .bndb, using BinaryView metadata (saved with the database).
Only chats live here; all settings are global (see config.py).

Chats written by the pre-rename "open_sidekick" version are still read, so nothing disappears."""
import json
import time
import uuid

KEY_INDEX = "ninjaprompt.index"
KEY_CHAT = "ninjaprompt.chat."
LEGACY_INDEX = "open_sidekick.index"
LEGACY_CHAT = "open_sidekick.chat."


class Chat:
    def __init__(self, id=None, title="New chat", created=None, updated=None, messages=None):
        self.id = id or uuid.uuid4().hex[:8]
        self.title = title
        self.created = created or time.time()
        self.updated = updated or self.created
        self.messages = messages if messages is not None else []

    def touch(self):
        self.updated = time.time()

    def to_json(self):
        return json.dumps(
            {"id": self.id, "title": self.title, "created": self.created, "updated": self.updated, "messages": self.messages}
        )

    @classmethod
    def from_json(cls, s):
        d = json.loads(s)
        return cls(d["id"], d.get("title", "Chat"), d.get("created"), d.get("updated"), d.get("messages", []))


class ChatStore:
    def __init__(self, bv):
        self.bv = bv

    def _read(self, key):
        try:
            return self.bv.query_metadata(key)
        except Exception:  # missing key raises KeyError
            return None

    def _read_index(self, key):
        raw = self._read(key)
        try:
            return json.loads(raw) if raw else []
        except ValueError:
            return []

    def _index(self):
        merged = {}
        for key in (LEGACY_INDEX, KEY_INDEX):  # new entries override legacy ones
            for e in self._read_index(key):
                merged[e["id"]] = e
        return list(merged.values())

    def list(self):
        return sorted(self._index(), key=lambda e: e.get("updated", 0), reverse=True)

    def load(self, chat_id):
        raw = self._read(KEY_CHAT + chat_id) or self._read(LEGACY_CHAT + chat_id)
        return Chat.from_json(raw) if raw else None

    def save(self, chat):
        if not chat.messages:  # never persist empty chats
            return
        self.bv.store_metadata(KEY_CHAT + chat.id, chat.to_json())
        idx = [e for e in self._read_index(KEY_INDEX) if e["id"] != chat.id]
        idx.append({"id": chat.id, "title": chat.title, "updated": chat.updated})
        self.bv.store_metadata(KEY_INDEX, json.dumps(idx))

    def delete(self, chat_id):
        for key in (KEY_CHAT + chat_id, LEGACY_CHAT + chat_id):
            try:
                self.bv.remove_metadata(key)
            except Exception:
                pass
        for key in (KEY_INDEX, LEGACY_INDEX):
            if self._read(key) is not None:
                self.bv.store_metadata(key, json.dumps([e for e in self._read_index(key) if e["id"] != chat_id]))
