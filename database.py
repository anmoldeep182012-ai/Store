"""Async MongoDB data layer.

All business records live in MongoDB. Multi-document financial and inventory
operations use transactions, so MongoDB must be a replica set (MongoDB Atlas
clusters satisfy this requirement).
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from cryptography.fernet import Fernet, InvalidToken
from bson import json_util
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo import AsyncMongoClient

import config


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).strftime("%Y-%m-%d %H:%M:%S")


def local_midnight_utc() -> str:
    """UTC timestamp of the start of 'today' in the configured time zone."""
    offset = timedelta(minutes=config.TZ_OFFSET_MINUTES)
    local = datetime.now(timezone.utc) + offset
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - offset
    return start.strftime("%Y-%m-%d %H:%M:%S")


class PurchaseError(Exception):
    """Raised when a purchase cannot be completed. Message is user-facing."""


class Database:
    COLLECTIONS = (
        "users", "countries", "accounts", "orders", "deposits",
        "transactions", "coupons", "coupon_uses", "settings", "counters", "server2_orders",
        "marketplace_listings", "marketplace_orders", "marketplace_logs",
    )

    def __init__(self, uri: str, database_name: str):
        self.uri = uri
        self.database_name = database_name
        self.client: Optional[AsyncMongoClient] = None
        self.database = None
        self.fernet: Optional[Fernet] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        if not self.uri:
            raise RuntimeError("MONGODB_URI is missing. Add your MongoDB connection string to .env.")
        if not config.SESSION_ENCRYPTION_KEY:
            raise RuntimeError(
                "SESSION_ENCRYPTION_KEY is missing. Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        try:
            self.fernet = Fernet(config.SESSION_ENCRYPTION_KEY.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("SESSION_ENCRYPTION_KEY is invalid. Use a Fernet key.") from exc

        self.client = AsyncMongoClient(self.uri, serverSelectionTimeoutMS=10000)
        self.database = self.client[self.database_name]
        await self.client.admin.command("ping")

        await self.database.users.create_index("username")
        await self.database.accounts.create_index("phone", unique=True)
        await self.database.accounts.create_index(
            [("country_id", ASCENDING), ("status", ASCENDING)]
        )
        await self.database.orders.create_index([("user_id", ASCENDING), ("_id", DESCENDING)])
        await self.database.deposits.create_index(
            "utr", unique=True, sparse=True
        )
        await self.database.deposits.create_index(
            [("user_id", ASCENDING), ("status", ASCENDING)]
        )
        await self.database.transactions.create_index(
            [("user_id", ASCENDING), ("_id", DESCENDING)]
        )
        await self.database.server2_orders.create_index([("user_id", ASCENDING), ("_id", DESCENDING)])
        await self.database.server2_orders.create_index([("number", ASCENDING)], unique=True)
        await self.database.marketplace_listings.create_index(
            [("status", ASCENDING), ("type", ASCENDING), ("_id", DESCENDING)]
        )
        await self.database.marketplace_listings.create_index("chat_id")
        await self.database.marketplace_orders.create_index(
            [("buyer_id", ASCENDING), ("_id", DESCENDING)]
        )
        await self.database.marketplace_orders.create_index(
            [("status", ASCENDING), ("_id", DESCENDING)]
        )
        await self.database.marketplace_orders.create_index("invite_link", sparse=True)
        await self.database.coupon_uses.create_index(
            [("code", ASCENDING), ("user_id", ASCENDING)], unique=True
        )

        for key, value in config.DEFAULT_SETTINGS.items():
            await self.database.settings.update_one(
                {"_id": key}, {"$setOnInsert": {"value": value}}, upsert=True
            )

    async def close(self) -> None:
        if self.client:
            await self.client.close()
            self.client = None
            self.database = None

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[Any]:
        """Run a multi-document operation in a MongoDB transaction."""
        async with self.client.start_session() as session:
            async with await session.start_transaction():
                yield session

    async def _next_id(self, name: str, session: Any) -> int:
        counter = await self.database.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        return int(counter["value"])

    @staticmethod
    def _plain(document: Optional[dict]) -> Optional[dict]:
        if not document:
            return None
        result = dict(document)
        result.pop("_id", None)
        return result

    def _encrypt_session(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt_session(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise RuntimeError(
                "A stored session could not be decrypted. Check that SESSION_ENCRYPTION_KEY "
                "is the same key used when this session was saved."
            ) from exc

    def _account_out(self, document: Optional[dict]) -> Optional[dict]:
        result = self._plain(document)
        if result is None:
            return None
        result["id"] = document["_id"]
        session_enc = result.pop("session_enc", None)
        twofa_enc = result.pop("twofa_enc", None)
        legacy_twofa = result.pop("twofa", None)
        result["session"] = self._decrypt_session(session_enc)
        result["twofa"] = self._decrypt_session(twofa_enc) if twofa_enc else legacy_twofa
        return result

    def _country_out(self, document: Optional[dict], stock: int = 0) -> Optional[dict]:
        result = self._plain(document)
        if result is None:
            return None
        result["id"] = document["_id"]
        result["stock"] = stock
        return result

    # ------------------------------------------------------------- settings
    async def get_setting(self, key: str) -> str:
        item = await self.database.settings.find_one({"_id": key})
        return item["value"] if item else config.DEFAULT_SETTINGS.get(key, "")

    async def set_setting(self, key: str, value: str) -> None:
        await self.database.settings.update_one(
            {"_id": key}, {"$set": {"value": value}}, upsert=True
        )

    # ---------------------------------------------------------------- users
    async def upsert_user(self, user_id: int, username: Optional[str], first_name: str,
                          referrer_id: Optional[int] = None) -> bool:
        """Create or refresh a user. Returns True when the user is new."""
        async with self.tx() as session:
            existing = await self.database.users.find_one({"_id": user_id}, session=session)
            if existing:
                await self.database.users.update_one(
                    {"_id": user_id},
                    {"$set": {"username": username, "first_name": first_name}},
                    session=session,
                )
                return False

            if referrer_id == user_id:
                referrer_id = None
            if referrer_id and not await self.database.users.find_one(
                {"_id": referrer_id}, {"_id": 1}, session=session
            ):
                referrer_id = None
            await self.database.users.insert_one({
                "_id": user_id,
                "username": username,
                "first_name": first_name,
                "balance": 0.0,
                "total_deposit": 0.0,
                "total_spent": 0.0,
                "referrer_id": referrer_id,
                "ref_earned": 0.0,
                "banned": 0,
                "joined_at": now(),
            }, session=session)
            return True

    async def get_user(self, user_id: int) -> Optional[dict]:
        doc = await self.database.users.find_one({"_id": user_id})
        return self._plain(doc) | {"id": doc["_id"]} if doc else None

    async def find_user(self, query: str) -> Optional[dict]:
        query = query.strip().lstrip("@")
        if query.isdigit():
            return await self.get_user(int(query))
        user = await self.database.users.find_one({
            "username": {"$regex": f"^{__import__('re').escape(query)}$", "$options": "i"}
        })
        return self._plain(user) | {"id": user["_id"]} if user else None

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.database.users.update_one(
            {"_id": user_id}, {"$set": {"banned": int(banned)}}
        )

    async def all_user_ids(self) -> list[int]:
        cursor = self.database.users.find({"banned": 0}, {"_id": 1})
        return [doc["_id"] async for doc in cursor]

    async def referral_count(self, user_id: int) -> int:
        return await self.database.users.count_documents({"referrer_id": user_id})

    async def _change_balance(self, session: Any, user_id: int, amount: float,
                              tx_type: str, note: str) -> float:
        user = await self.database.users.find_one({"_id": user_id}, session=session)
        if user is None:
            raise PurchaseError("User not found.")
        new_balance = round(float(user["balance"]) + amount, 2)
        if new_balance < 0:
            raise PurchaseError("Insufficient balance.")
        await self.database.users.update_one(
            {"_id": user_id}, {"$set": {"balance": new_balance}}, session=session
        )
        tx_id = await self._next_id("transactions", session)
        await self.database.transactions.insert_one({
            "_id": tx_id,
            "user_id": user_id,
            "type": tx_type,
            "amount": round(amount, 2),
            "balance_after": new_balance,
            "note": note,
            "created_at": now(),
        }, session=session)
        return new_balance

    async def admin_adjust_balance(self, user_id: int, amount: float, admin_id: int) -> float:
        async with self.tx() as session:
            return await self._change_balance(
                session, user_id, amount, "admin", f"Adjusted by admin {admin_id}"
            )

    async def transactions(self, user_id: int, limit: int = 10) -> list[dict]:
        cursor = self.database.transactions.find(
            {"user_id": user_id}
        ).sort("_id", DESCENDING).limit(limit)
        return [self._plain(doc) | {"id": doc["_id"]} async for doc in cursor]

    # ------------------------------------------------------------ countries
    async def countries(self, only_active: bool = True) -> list[dict]:
        query = {"active": 1} if only_active else {}
        documents = [doc async for doc in self.database.countries.find(query)]
        result = []
        for doc in documents:
            stock = await self.database.accounts.count_documents({
                "country_id": doc["_id"], "status": "available"
            })
            result.append(self._country_out(doc, stock))
        return sorted(result, key=lambda item: (item["stock"] == 0, item["name"].casefold()))

    async def get_country(self, country_id: int) -> Optional[dict]:
        doc = await self.database.countries.find_one({"_id": country_id})
        if not doc:
            return None
        stock = await self.database.accounts.count_documents({
            "country_id": country_id, "status": "available"
        })
        return self._country_out(doc, stock)

    async def add_country(self, name: str, code: str, flag: str, price: float) -> int:
        async with self.tx() as session:
            country_id = await self._next_id("countries", session)
            await self.database.countries.insert_one({
                "_id": country_id,
                "name": name,
                "code": code.upper(),
                "flag": flag,
                "price": round(price, 2),
                "active": 1,
            }, session=session)
            return country_id

    async def update_country(self, country_id: int, field: str, value: Any) -> None:
        if field not in {"name", "code", "flag", "price", "active"}:
            raise ValueError(field)
        if field == "price":
            value = round(float(value), 2)
        await self.database.countries.update_one(
            {"_id": country_id}, {"$set": {field: value}}
        )

    async def delete_country(self, country_id: int) -> bool:
        if await self.database.accounts.count_documents({"country_id": country_id}):
            return False
        result = await self.database.countries.delete_one({"_id": country_id})
        return result.deleted_count > 0

    # ------------------------------------------------------------- accounts
    async def add_accounts(self, country_id: int,
                           items: list[tuple[str, Optional[str], Optional[str]]]) -> tuple[int, int]:
        """Insert (phone, twofa, session) items. Returns (added, duplicates)."""
        added = dupes = 0
        async with self.tx() as session:
            if not await self.database.countries.find_one({"_id": country_id}, session=session):
                raise ValueError("Country not found.")
            for phone, twofa, session_string in items:
                if await self.database.accounts.find_one(
                    {"phone": phone}, {"_id": 1}, session=session
                ):
                    dupes += 1
                    continue
                account_id = await self._next_id("accounts", session)
                await self.database.accounts.insert_one({
                    "_id": account_id,
                    "country_id": country_id,
                    "phone": phone,
                    "twofa_enc": self._encrypt_session(twofa),
                    "session_enc": self._encrypt_session(session_string),
                    "status": "available",
                    "added_at": now(),
                    "sold_at": None,
                }, session=session)
                added += 1
        return added, dupes

    async def stock_page(self, country_id: int, offset: int, limit: int) -> list[dict]:
        cursor = self.database.accounts.find({
            "country_id": country_id, "status": "available"
        }).sort("_id", ASCENDING).skip(offset).limit(limit)
        return [self._account_out(doc) async for doc in cursor]

    async def get_account(self, account_id: int) -> Optional[dict]:
        return self._account_out(await self.database.accounts.find_one({"_id": account_id}))

    async def delete_account(self, account_id: int) -> bool:
        result = await self.database.accounts.delete_one({
            "_id": account_id, "status": "available"
        })
        return result.deleted_count > 0

    # ------------------------------------------------------------- purchase
    async def purchase(self, user_id: int, country_id: int, qty: int) -> list[dict]:
        """Atomically charge the user and reserve qty accounts."""
        async with self.tx() as session:
            country = await self.database.countries.find_one(
                {"_id": country_id, "active": 1}, session=session
            )
            if not country:
                raise PurchaseError("This country is not available right now.")

            cursor = self.database.accounts.find({
                "country_id": country_id, "status": "available"
            }, session=session).sort("_id", ASCENDING).limit(qty)
            accounts = [doc async for doc in cursor]
            if len(accounts) < qty:
                raise PurchaseError(
                    f"Only {len(accounts)} in stock. Please lower the quantity."
                )

            total = round(float(country["price"]) * qty, 2)
            await self._change_balance(
                session, user_id, -total, "purchase", f"{qty} x {country['name']}"
            )
            await self.database.users.update_one(
                {"_id": user_id}, {"$inc": {"total_spent": total}}, session=session
            )

            account_ids = [doc["_id"] for doc in accounts]
            reserved = await self.database.accounts.update_many(
                {"_id": {"$in": account_ids}, "status": "available"},
                {"$set": {"status": "sold", "sold_at": now()}},
                session=session,
            )
            if reserved.modified_count != qty:
                raise PurchaseError("Stock changed while purchasing. Please try again.")

            order_ids = []
            for account_id in account_ids:
                order_id = await self._next_id("orders", session)
                await self.database.orders.insert_one({
                    "_id": order_id,
                    "user_id": user_id,
                    "account_id": account_id,
                    "country_id": country_id,
                    "price": round(float(country["price"]), 2),
                    "created_at": now(),
                }, session=session)
                order_ids.append(order_id)

        return [await self.get_order(order_id) for order_id in order_ids]

    # --------------------------------------------------------------- orders
    async def _order_out(self, order: dict) -> dict:
        account = await self.database.accounts.find_one({"_id": order["account_id"]})
        country = await self.database.countries.find_one({"_id": order["country_id"]})
        user = await self.database.users.find_one({"_id": order["user_id"]})
        if not account or not country or not user:
            return self._plain(order) | {"id": order["_id"]}
        return self._plain(order) | {
            "id": order["_id"],
            "phone": account["phone"],
            "twofa": (
                self._decrypt_session(account.get("twofa_enc"))
                if account.get("twofa_enc") else account.get("twofa")
            ),
            "session": self._decrypt_session(account.get("session_enc")),
            "country_name": country["name"],
            "country_flag": country["flag"],
            "username": user.get("username"),
            "first_name": user.get("first_name"),
        }

    async def get_order(self, order_id: int) -> Optional[dict]:
        order = await self.database.orders.find_one({"_id": order_id})
        return await self._order_out(order) if order else None

    async def user_orders(self, user_id: int, offset: int, limit: int) -> list[dict]:
        cursor = self.database.orders.find({"user_id": user_id}).sort(
            "_id", DESCENDING
        ).skip(offset).limit(limit)
        return [await self._order_out(order) async for order in cursor]

    async def count_user_orders(self, user_id: int) -> int:
        return await self.database.orders.count_documents({"user_id": user_id})

    async def recent_orders(self, limit: int = 15) -> list[dict]:
        cursor = self.database.orders.find().sort("_id", DESCENDING).limit(limit)
        return [await self._order_out(order) async for order in cursor]

    # ---------------------------------------------------------- server 2 orders
    async def reserve_server2_balance(self, user_id: int, amount: float, note: str) -> float:
        """Reserve wallet funds before a provider purchase."""
        async with self.tx() as session:
            return await self._change_balance(
                session, user_id, -round(amount, 2), "server2_purchase", note
            )

    async def refund_server2_balance(self, user_id: int, amount: float, note: str) -> float:
        async with self.tx() as session:
            return await self._change_balance(
                session, user_id, round(amount, 2), "refund", note
            )

    async def create_server2_order(
        self,
        user_id: int,
        country_code: str,
        country_name: str,
        provider_price_usd: float,
        price: float,
        number: str,
        code: str = "",
        password: str = "",
    ) -> int:
        async with self.tx() as session:
            order_id = await self._next_id("server2_orders", session)
            await self.database.server2_orders.insert_one({
                "_id": order_id,
                "user_id": user_id,
                "country_code": country_code.upper(),
                "country_name": country_name,
                "provider_price_usd": round(provider_price_usd, 4),
                "price": round(price, 2),
                "number": number,
                "code": code or None,
                "password": password or None,
                "status": "delivered",
                "created_at": now(),
            }, session=session)
            await self.database.users.update_one(
                {"_id": user_id},
                {"$inc": {"total_spent": round(price, 2)}},
                session=session,
            )
            return order_id

    async def get_server2_order(self, order_id: int) -> Optional[dict]:
        doc = await self.database.server2_orders.find_one({"_id": order_id})
        return self._plain(doc) | {"id": doc["_id"]} if doc else None

    async def update_server2_credentials(
        self, order_id: int, code: str, password: str
    ) -> Optional[dict]:
        await self.database.server2_orders.update_one(
            {"_id": order_id},
            {"$set": {"code": code or None, "password": password or None}},
        )
        return await self.get_server2_order(order_id)

    async def server2_orders(self, user_id: int, offset: int, limit: int) -> list[dict]:
        cursor = self.database.server2_orders.find({"user_id": user_id}).sort(
            "_id", DESCENDING
        ).skip(offset).limit(limit)
        return [self._plain(doc) | {"id": doc["_id"]} async for doc in cursor]

    async def count_server2_orders(self, user_id: int) -> int:
        return await self.database.server2_orders.count_documents({"user_id": user_id})

    # ------------------------------------------------------ channel/group marketplace
    async def create_marketplace_listing(self, data: dict) -> dict:
        async with self.tx() as session:
            listing_id = await self._next_id("marketplace_listings", session)
            document = {
                "_id": listing_id,
                "type": data["type"],
                "title": data["title"],
                "username": data.get("username"),
                "chat_id": int(data["chat_id"]),
                "chat_title": data["chat_title"],
                "description": data.get("description", ""),
                "tags": list(data.get("tags") or []),
                "price": round(float(data["price"]), 2),
                "seller_admin_id": int(data["seller_admin_id"]),
                "delivery_options": ["transfer", "invite"],
                "status": "active",
                "verified": True,
                "verification_method": "telegram_bot_api",
                "created_at": now(),
                "updated_at": now(),
            }
            await self.database.marketplace_listings.insert_one(document, session=session)
            await self.database.marketplace_logs.insert_one({
                "action": "listing_created", "actor_id": int(data["seller_admin_id"]),
                "listing_id": listing_id, "created_at": now(),
            }, session=session)
        return self._plain(document) | {"id": listing_id}

    async def get_marketplace_listing(self, listing_id: int) -> Optional[dict]:
        document = await self.database.marketplace_listings.find_one({"_id": listing_id})
        return self._plain(document) | {"id": document["_id"]} if document else None

    async def marketplace_listings(
        self, listing_type: Optional[str] = None, status: str = "active",
        offset: int = 0, limit: int = 10,
    ) -> list[dict]:
        query: dict[str, Any] = {"status": status, "verified": True}
        if listing_type:
            query["type"] = listing_type
        cursor = self.database.marketplace_listings.find(query).sort(
            "_id", DESCENDING
        ).skip(offset).limit(limit)
        return [self._plain(document) | {"id": document["_id"]}
                async for document in cursor]

    async def marketplace_listing_count(
        self, listing_type: Optional[str] = None, status: str = "active"
    ) -> int:
        query: dict[str, Any] = {"status": status, "verified": True}
        if listing_type:
            query["type"] = listing_type
        return await self.database.marketplace_listings.count_documents(query)

    async def hide_marketplace_listing(self, listing_id: int, actor_id: int) -> bool:
        result = await self.database.marketplace_listings.update_one(
            {"_id": listing_id, "status": "active"},
            {"$set": {"status": "hidden", "updated_at": now()}},
        )
        if result.modified_count:
            await self.database.marketplace_logs.insert_one({
                "action": "listing_hidden", "actor_id": actor_id,
                "listing_id": listing_id, "created_at": now(),
            })
        return result.modified_count > 0

    async def delete_marketplace_listing(self, listing_id: int, actor_id: int) -> bool:
        result = await self.database.marketplace_listings.update_one(
            {"_id": listing_id, "status": {"$in": ["active", "hidden"]}},
            {"$set": {"status": "deleted", "updated_at": now()}},
        )
        if result.modified_count:
            await self.database.marketplace_logs.insert_one({
                "action": "listing_deleted", "actor_id": actor_id,
                "listing_id": listing_id, "created_at": now(),
            })
        return result.modified_count > 0

    async def create_marketplace_order(
        self, buyer_id: int, listing_id: int, delivery_method: str
    ) -> dict:
        async with self.tx() as session:
            listing = await self.database.marketplace_listings.find_one(
                {"_id": listing_id, "status": "active", "verified": True},
                session=session,
            )
            if not listing:
                raise PurchaseError("This listing is no longer available.")
            if listing["seller_admin_id"] == buyer_id:
                raise PurchaseError("You cannot purchase your own listing.")
            if delivery_method not in listing.get("delivery_options", []):
                raise PurchaseError("That delivery method is not available.")
            price = round(float(listing["price"]), 2)
            await self._change_balance(
                session, buyer_id, -price, "marketplace_purchase",
                f"Marketplace listing #{listing_id}",
            )
            order_id = await self._next_id("marketplace_orders", session)
            status = "awaiting_transfer" if delivery_method == "transfer" else "awaiting_join"
            document = {
                "_id": order_id,
                "listing_id": listing_id,
                "buyer_id": buyer_id,
                "seller_admin_id": listing["seller_admin_id"],
                "chat_id": listing["chat_id"],
                "chat_title": listing["chat_title"],
                "listing_title": listing["title"],
                "listing_type": listing["type"],
                "username": listing.get("username"),
                "price": price,
                "delivery_method": delivery_method,
                "status": status,
                "invite_link": None,
                "created_at": now(),
                "updated_at": now(),
            }
            result = await self.database.marketplace_listings.update_one(
                {"_id": listing_id, "status": "active"},
                {"$set": {
                    "status": "reserved",
                    "reserved_order_id": order_id,
                    "updated_at": now(),
                }},
                session=session,
            )
            if result.modified_count != 1:
                raise PurchaseError("Someone else just reserved this listing. Please retry.")
            await self.database.marketplace_orders.insert_one(document, session=session)
            await self.database.marketplace_logs.insert_one({
                "action": "order_created", "actor_id": buyer_id,
                "listing_id": listing_id, "order_id": order_id,
                "created_at": now(),
            }, session=session)
        return self._plain(document) | {"id": order_id}

    async def get_marketplace_order(self, order_id: int) -> Optional[dict]:
        document = await self.database.marketplace_orders.find_one({"_id": order_id})
        return self._plain(document) | {"id": document["_id"]} if document else None

    async def set_marketplace_invite_link(self, order_id: int, invite_link: str) -> bool:
        result = await self.database.marketplace_orders.update_one(
            {"_id": order_id, "status": "awaiting_join"},
            {"$set": {"invite_link": invite_link, "updated_at": now()}},
        )
        return result.modified_count > 0

    async def find_pending_transfer_order(self, chat_id: int, buyer_id: int) -> Optional[dict]:
        document = await self.database.marketplace_orders.find_one({
            "chat_id": int(chat_id), "buyer_id": int(buyer_id),
            "delivery_method": "transfer", "status": "awaiting_transfer",
        })
        return self._plain(document) | {"id": document["_id"]} if document else None

    async def find_pending_invite_order(self, chat_id: int, buyer_id: int) -> Optional[dict]:
        document = await self.database.marketplace_orders.find_one({
            "chat_id": int(chat_id), "buyer_id": int(buyer_id),
            "delivery_method": "invite", "status": "awaiting_join",
        })
        return self._plain(document) | {"id": document["_id"]} if document else None

    async def find_marketplace_order_by_invite(self, chat_id: int, invite_link: str) -> Optional[dict]:
        document = await self.database.marketplace_orders.find_one({
            "chat_id": int(chat_id), "invite_link": invite_link,
            "delivery_method": "invite", "status": "awaiting_join",
        })
        return self._plain(document) | {"id": document["_id"]} if document else None

    async def complete_marketplace_order(
        self, order_id: int, actor_id: int, verification: str
    ) -> Optional[dict]:
        async with self.tx() as session:
            order = await self.database.marketplace_orders.find_one(
                {"_id": order_id, "status": {"$in": ["awaiting_transfer", "awaiting_join"]}},
                session=session,
            )
            if not order:
                return None
            result = await self.database.marketplace_orders.update_one(
                {"_id": order_id, "status": order["status"]},
                {"$set": {
                    "status": "completed", "verification": verification,
                    "verified_at": now(), "updated_at": now(),
                }},
                session=session,
            )
            if result.modified_count != 1:
                return None
            await self.database.marketplace_listings.update_one(
                {"_id": order["listing_id"], "reserved_order_id": order_id},
                {"$set": {"status": "sold", "updated_at": now()}},
                session=session,
            )
            await self.database.marketplace_logs.insert_one({
                "action": "order_completed", "actor_id": actor_id,
                "listing_id": order["listing_id"], "order_id": order_id,
                "verification": verification, "created_at": now(),
            }, session=session)
        return self._plain(order) | {
            "id": order_id, "status": "completed", "verification": verification,
        }

    async def refund_marketplace_order(self, order_id: int, actor_id: int, reason: str) -> Optional[dict]:
        async with self.tx() as session:
            order = await self.database.marketplace_orders.find_one(
                {"_id": order_id, "status": {"$in": ["awaiting_transfer", "awaiting_join"]}},
                session=session,
            )
            if not order:
                return None
            await self._change_balance(
                session, order["buyer_id"], round(float(order["price"]), 2),
                "refund", f"Marketplace order #{order_id}: {reason}",
            )
            await self.database.marketplace_orders.update_one(
                {"_id": order_id},
                {"$set": {
                    "status": "refunded", "refund_reason": reason,
                    "resolved_by": actor_id, "updated_at": now(),
                }},
                session=session,
            )
            await self.database.marketplace_listings.update_one(
                {"_id": order["listing_id"], "reserved_order_id": order_id},
                {"$set": {"status": "active", "updated_at": now()},
                 "$unset": {"reserved_order_id": ""}},
                session=session,
            )
            await self.database.marketplace_logs.insert_one({
                "action": "order_refunded", "actor_id": actor_id,
                "listing_id": order["listing_id"], "order_id": order_id,
                "reason": reason, "created_at": now(),
            }, session=session)
        return self._plain(order) | {"id": order_id, "status": "refunded"}

    async def marketplace_orders(
        self, buyer_id: Optional[int] = None, offset: int = 0, limit: int = 15
    ) -> list[dict]:
        query = {"buyer_id": buyer_id} if buyer_id is not None else {}
        cursor = self.database.marketplace_orders.find(query).sort(
            "_id", DESCENDING
        ).skip(offset).limit(limit)
        return [self._plain(document) | {"id": document["_id"]}
                async for document in cursor]

    async def marketplace_order_count(self, buyer_id: Optional[int] = None) -> int:
        query = {"buyer_id": buyer_id} if buyer_id is not None else {}
        return await self.database.marketplace_orders.count_documents(query)

    # ------------------------------------------------------------- deposits
    async def create_deposit(self, user_id: int, amount: float) -> int:
        async with self.tx() as session:
            deposit_id = await self._next_id("deposits", session)
            ts = now()
            await self.database.deposits.insert_one({
                "_id": deposit_id,
                "user_id": user_id,
                "amount": round(amount, 2),
                "status": "created",
                "admin_id": None,
                "created_at": ts,
                "updated_at": ts,
            }, session=session)
            return deposit_id

    async def get_deposit(self, deposit_id: int) -> Optional[dict]:
        doc = await self.database.deposits.find_one({"_id": deposit_id})
        return self._plain(doc) | {"id": doc["_id"]} if doc else None

    async def count_pending_deposits(self, user_id: int) -> int:
        return await self.database.deposits.count_documents({
            "user_id": user_id, "status": "pending"
        })

    async def submit_utr(self, deposit_id: int, user_id: int, utr: str) -> str:
        """Attach a UTR to a deposit. Returns ok, duplicate, expired or invalid."""
        try:
            async with self.tx() as session:
                dep = await self.database.deposits.find_one(
                    {"_id": deposit_id, "user_id": user_id}, session=session
                )
                if not dep or dep["status"] != "created":
                    return "invalid"
                if dep["created_at"] < ago(minutes=config.PAYMENT_TIMEOUT_MIN):
                    await self.database.deposits.update_one(
                        {"_id": deposit_id},
                        {"$set": {"status": "expired", "updated_at": now()}},
                        session=session,
                    )
                    return "expired"
                if await self.database.deposits.find_one({"utr": utr}, session=session):
                    return "duplicate"
                await self.database.deposits.update_one(
                    {"_id": deposit_id},
                    {"$set": {"utr": utr, "status": "pending", "updated_at": now()}},
                    session=session,
                )
            return "ok"
        except DuplicateKeyError:
            return "duplicate"

    async def cancel_deposit(self, deposit_id: int, user_id: int) -> None:
        await self.database.deposits.update_one(
            {"_id": deposit_id, "user_id": user_id, "status": "created"},
            {"$set": {"status": "cancelled", "updated_at": now()}},
        )

    async def resolve_deposit(self, deposit_id: int, approve: bool, admin_id: int,
                              amount: Optional[float] = None) -> Optional[dict]:
        """Resolve a pending deposit and credit wallet/referral balance atomically."""
        ref_percent = float(await self.get_setting("ref_percent") or 0)
        async with self.tx() as session:
            dep = await self.database.deposits.find_one(
                {"_id": deposit_id}, session=session
            )
            if not dep or dep["status"] != "pending":
                return None
            dep_out = self._plain(dep) | {"id": dep["_id"]}

            if not approve:
                await self.database.deposits.update_one(
                    {"_id": deposit_id},
                    {"$set": {"status": "rejected", "admin_id": admin_id, "updated_at": now()}},
                    session=session,
                )
                return {
                    "deposit": dep_out, "credited": 0, "ref_bonus": 0,
                    "referrer_id": None,
                }

            credited = round(amount if amount is not None else dep["amount"], 2)
            await self.database.deposits.update_one(
                {"_id": deposit_id},
                {"$set": {
                    "status": "approved", "amount": credited,
                    "admin_id": admin_id, "updated_at": now(),
                }},
                session=session,
            )
            await self._change_balance(
                session, dep["user_id"], credited, "deposit", f"Deposit #{deposit_id}"
            )
            await self.database.users.update_one(
                {"_id": dep["user_id"]}, {"$inc": {"total_deposit": credited}},
                session=session,
            )
            buyer = await self.database.users.find_one(
                {"_id": dep["user_id"]}, {"referrer_id": 1}, session=session
            )
            referrer_id = buyer.get("referrer_id") if buyer else None
            bonus = 0.0
            if referrer_id and ref_percent > 0:
                bonus = round(credited * ref_percent / 100, 2)
                if bonus > 0:
                    await self._change_balance(
                        session, referrer_id, bonus, "referral", f"From user {dep['user_id']}"
                    )
                    await self.database.users.update_one(
                        {"_id": referrer_id}, {"$inc": {"ref_earned": bonus}},
                        session=session,
                    )
            return {
                "deposit": dep_out, "credited": credited, "ref_bonus": bonus,
                "referrer_id": referrer_id,
            }

    async def pending_deposits(self, limit: int = 20) -> list[dict]:
        cursor = self.database.deposits.find({"status": "pending"}).sort(
            "_id", ASCENDING
        ).limit(limit)
        result = []
        async for dep in cursor:
            user = await self.database.users.find_one({"_id": dep["user_id"]})
            result.append(self._plain(dep) | {
                "id": dep["_id"],
                "username": user.get("username") if user else None,
                "first_name": user.get("first_name") if user else None,
            })
        return result

    # -------------------------------------------------------------- coupons
    async def create_coupon(self, code: str, amount: float, max_uses: int) -> bool:
        try:
            await self.database.coupons.insert_one({
                "_id": code.upper(),
                "amount": round(amount, 2),
                "max_uses": max_uses,
                "used": 0,
                "created_at": now(),
            })
            return True
        except DuplicateKeyError:
            return False

    async def coupons(self) -> list[dict]:
        cursor = self.database.coupons.find().sort("created_at", DESCENDING)
        return [self._plain(doc) | {"code": doc["_id"]} async for doc in cursor]

    async def delete_coupon(self, code: str) -> None:
        async with self.tx() as session:
            await self.database.coupons.delete_one({"_id": code}, session=session)
            await self.database.coupon_uses.delete_many({"code": code}, session=session)

    async def redeem_coupon(self, user_id: int, code: str) -> tuple[str, float]:
        """Returns (status, amount): ok, invalid, used or exhausted."""
        code = code.strip().upper()
        try:
            async with self.tx() as session:
                coupon = await self.database.coupons.find_one({"_id": code}, session=session)
                if not coupon:
                    return "invalid", 0
                if coupon["used"] >= coupon["max_uses"]:
                    return "exhausted", 0
                usage_id = f"{code}:{user_id}"
                if await self.database.coupon_uses.find_one(
                    {"_id": usage_id}, session=session
                ):
                    return "used", 0
                await self.database.coupon_uses.insert_one({
                    "_id": usage_id, "code": code, "user_id": user_id
                }, session=session)
                await self.database.coupons.update_one(
                    {"_id": code}, {"$inc": {"used": 1}}, session=session
                )
                await self._change_balance(
                    session, user_id, coupon["amount"], "coupon", f"Code {code}"
                )
                return "ok", coupon["amount"]
        except DuplicateKeyError:
            return "used", 0

    # ---------------------------------------------------------------- stats
    async def _revenue(self, since: Optional[str]) -> tuple[float, int]:
        match = {"created_at": {"$gte": since}} if since else {}
        cursor = await self.database.orders.aggregate([
            {"$match": match},
            {"$group": {"_id": None, "total": {"$sum": "$price"}, "count": {"$sum": 1}}},
        ])
        rows = await cursor.to_list(length=1)
        if not rows:
            return 0.0, 0
        return float(rows[0]["total"]), int(rows[0]["count"])

    async def stats(self) -> dict:
        today = local_midnight_utc()
        week = ago(days=7)
        users_today = await self.database.users.count_documents({"joined_at": {"$gte": today}})
        deposits_cursor = await self.database.deposits.aggregate([
            {"$match": {"status": "approved"}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
        ])
        deposits = await deposits_cursor.to_list(length=1)
        balances_cursor = await self.database.users.aggregate([
            {"$group": {"_id": None, "total": {"$sum": "$balance"}}},
        ])
        balances = await balances_cursor.to_list(length=1)
        return {
            "users": await self.database.users.count_documents({}),
            "users_today": users_today,
            "stock": await self.database.accounts.count_documents({"status": "available"}),
            "sold": await self.database.accounts.count_documents({"status": "sold"}),
            "today": await self._revenue(today),
            "week": await self._revenue(week),
            "all": await self._revenue(None),
            "deposits": float(deposits[0]["total"]) if deposits else 0.0,
            "pending": await self.database.deposits.count_documents({"status": "pending"}),
            "balances": float(balances[0]["total"]) if balances else 0.0,
        }

    # ---------------------------------------------------------------- backup
    async def backup(self, target: str) -> None:
        """Write a JSON snapshot of every application collection to target."""
        names = await self.database.list_collection_names()
        snapshot = {}
        for name in names:
            snapshot[name] = [
                doc async for doc in self.database[name].find({})
            ]
        payload = json_util.dumps(snapshot, ensure_ascii=False, indent=2)
        await asyncio.to_thread(Path(target).write_text, payload, encoding="utf-8")


db = Database(config.MONGODB_URI, config.MONGODB_DATABASE)
