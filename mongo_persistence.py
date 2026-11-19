"""MongoDB-backed python-telegram-bot persistence."""
from __future__ import annotations

import pickle
from typing import Any

from telegram.ext import BasePersistence, PersistenceInput


class MongoPersistence(BasePersistence):
    """Persist PTB user/chat/bot data and conversation state in MongoDB."""

    def __init__(self, uri: str, database_name: str):
        super().__init__(
            store_data=PersistenceInput(
                bot_data=True, chat_data=True, user_data=True, callback_data=True
            ),
            update_interval=5,
        )
        from pymongo import AsyncMongoClient
        self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=10000)
        self.db = self.client[database_name]
        self.data = self.db["telegram_persistence"]
        self.conversations = self.db["telegram_conversations"]

    @staticmethod
    def _pack(value: Any) -> bytes:
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def _unpack(value: bytes | None, default: Any) -> Any:
        return pickle.loads(value) if value is not None else default

    async def get_user_data(self):
        result = {}
        async for doc in self.data.find({"kind": "user"}):
            result[int(doc["key"])] = self._unpack(doc.get("payload"), {})
        return result

    async def get_chat_data(self):
        result = {}
        async for doc in self.data.find({"kind": "chat"}):
            result[int(doc["key"])] = self._unpack(doc.get("payload"), {})
        return result

    async def get_bot_data(self):
        doc = await self.data.find_one({"_id": "bot"})
        return self._unpack(doc.get("payload") if doc else None, {})

    async def get_callback_data(self):
        doc = await self.data.find_one({"_id": "callback"})
        return self._unpack(doc.get("payload") if doc else None, None)

    async def get_conversations(self, name: str):
        result = {}
        async for doc in self.conversations.find({"name": name}):
            result[self._unpack(doc["key"], None)] = self._unpack(doc.get("state"), None)
        return result

    async def update_conversation(self, name: str, key: tuple, new_state: Any):
        packed_key = self._pack(key)
        if new_state is None:
            await self.conversations.delete_one({"name": name, "key": packed_key})
        else:
            await self.conversations.update_one(
                {"name": name, "key": packed_key},
                {"$set": {"state": self._pack(new_state)}},
                upsert=True,
            )

    async def update_user_data(self, user_id: int, data: dict):
        await self.data.update_one(
            {"_id": f"user:{user_id}"},
            {"$set": {"kind": "user", "key": str(user_id), "payload": self._pack(data)}},
            upsert=True,
        )

    async def update_chat_data(self, chat_id: int, data: dict):
        await self.data.update_one(
            {"_id": f"chat:{chat_id}"},
            {"$set": {"kind": "chat", "key": str(chat_id), "payload": self._pack(data)}},
            upsert=True,
        )

    async def update_bot_data(self, data: dict):
        await self.data.update_one(
            {"_id": "bot"}, {"$set": {"kind": "bot", "payload": self._pack(data)}},
            upsert=True,
        )

    async def update_callback_data(self, data):
        await self.data.update_one(
            {"_id": "callback"},
            {"$set": {"kind": "callback", "payload": self._pack(data)}},
            upsert=True,
        )

    async def drop_user_data(self, user_id: int):
        await self.data.delete_one({"_id": f"user:{user_id}"})

    async def drop_chat_data(self, chat_id: int):
        await self.data.delete_one({"_id": f"chat:{chat_id}"})

    async def refresh_user_data(self, user_id: int, user_data: dict):
        stored = await self.data.find_one({"_id": f"user:{user_id}"})
        if stored:
            user_data.clear()
            user_data.update(self._unpack(stored.get("payload"), {}))

    async def refresh_chat_data(self, chat_id: int, chat_data: dict):
        stored = await self.data.find_one({"_id": f"chat:{chat_id}"})
        if stored:
            chat_data.clear()
            chat_data.update(self._unpack(stored.get("payload"), {}))

    async def refresh_bot_data(self, bot_data: dict):
        stored = await self.data.find_one({"_id": "bot"})
        if stored:
            bot_data.clear()
            bot_data.update(self._unpack(stored.get("payload"), {}))

    async def flush(self):
        await self.client.close()
