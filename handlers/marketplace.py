"""Owner-managed Telegram channel/group marketplace.

Listings are published only after Telegram creator and bot permissions are checked.
Orders are completed from Telegram chat_member / chat_join_request updates.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CallbackQueryHandler, ChatJoinRequestHandler, ChatMemberHandler,
)

from core import (
    Ctx, clear_state, is_admin, notify_admins, on_input, send, set_state, show,
)
from database import PurchaseError, db
from ui.theme import back, blue, btn, card, e, green, kb, money, pager, red, row, url_btn


PAGE_SIZE = 8


def _chat_type(chat) -> str | None:
    if chat.type == "channel":
        return "channel"
    if chat.type in ("group", "supergroup"):
        return "group"
    return None


async def _verify_listing_chat(bot, chat_ref: str, owner_id: int) -> tuple[dict, str]:
    """Require a real channel/group, submitting user as creator, bot as invite-capable admin."""
    try:
        chat = await bot.get_chat(chat_ref)
        owner = await bot.get_chat_member(chat.id, owner_id)
        bot_member = await bot.get_chat_member(chat.id, bot.id)
    except Exception as exc:
        raise ValueError(
            "I couldn't access that chat. Add this bot as an administrator first, "
            "then send its @username or chat ID again."
        ) from exc

    kind = _chat_type(chat)
    if not kind:
        raise ValueError("That link is not a Telegram channel or group.")
    if owner.status != ChatMemberStatus.OWNER:
        raise ValueError(
            "Ownership check failed. The account adding this listing must be the "
            "actual creator of the channel/group."
        )
    if bot_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        raise ValueError("The bot must be an administrator in that channel/group.")
    if not getattr(bot_member, "can_invite_users", False):
        raise ValueError("Give the bot the 'Invite Users' administrator permission.")
    return {
        "chat_id": chat.id,
        "chat_title": chat.title or chat.full_name or str(chat.id),
        "username": f"@{chat.username}" if chat.username else None,
        "type": kind,
    }, kind


# ---------------------------------------------------------------- customer marketplace
async def market_home_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    channels = await db.marketplace_listing_count("channel")
    groups = await db.marketplace_listing_count("group")
    await show(update, card(
        "📢 Channels & Groups",
        row("📣 Channels", str(channels)),
        row("👥 Groups", str(groups)),
        "",
        "Browse owner-verified Telegram communities.",
        footer="Every listing is checked through Telegram before publication.",
    ), kb(
        [blue("📣 Channels", "market:list:channel:0")],
        [blue("👥 Groups", "market:list:group:0")],
        [btn("📦 My Marketplace Orders", "market:orders")],
        back("shop:0"),
    ))


async def market_list_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    _, _, kind, page_text = query.data.split(":")
    page = int(page_text)
    await query.answer()
    total = await db.marketplace_listing_count(kind)
    if not total:
        await show(update, card(
            "📣 Channels" if kind == "channel" else "👥 Groups",
            "No verified listings are available right now.",
        ), kb([btn("‹ Categories", "market:home")], [btn("🏠 Home", "home")]))
        return
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    listings = await db.marketplace_listings(kind, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    rows = [[btn(
        f"{'📣' if kind == 'channel' else '👥'} {e(item['title'])} • {money(item['price'])}",
        f"market:view:{item['id']}",
    )] for item in listings]
    await show(update, card(
        "📣 Available Channels" if kind == "channel" else "👥 Available Groups",
        row("Verified listings", str(total)),
        "",
        "Select a listing to view its details.",
    ), kb(*rows, pager(f"market:list:{kind}", page, pages),
           [btn("‹ Categories", "market:home"), btn("🏠 Home", "home")]))


async def market_view_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    listing_id = int(query.data.split(":")[2])
    listing = await db.get_marketplace_listing(listing_id)
    if not listing or listing["status"] != "active" or not listing["verified"]:
        await query.answer("This listing is no longer available.", show_alert=True)
        return
    await query.answer()
    tags = " ".join(f"#{e(tag).replace(' ', '')}" for tag in listing.get("tags", []))
    text = card(
        f"{'📣 Channel' if listing['type'] == 'channel' else '👥 Group'} • {e(listing['title'])}",
        row("💰 Price", money(listing["price"])),
        row("🔎 Username", e(listing.get("username") or "Private")),
        row("✅ Verification", "Telegram creator checked"),
        f"▸ {e(listing.get('description') or 'No description provided.')}",
        tags,
        "",
        "Choose how you want to receive this listing.",
        footer="Ownership transfer is checked by Telegram. Invite delivery is checked when you join.",
    )
    await show(update, text, kb(
        [green("🔐 Transfer Ownership", f"market:buy:{listing_id}:transfer")],
        [blue("🔗 Invite Link", f"market:buy:{listing_id}:invite")],
        [btn("‹ Back", f"market:list:{listing['type']}:0"), btn("🏠 Home", "home")],
    ))


async def market_buy_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    _, _, listing_text, method = query.data.split(":")
    listing_id = int(listing_text)
    buyer_id = update.effective_user.id
    listing = await db.get_marketplace_listing(listing_id)
    if not listing or listing["status"] != "active":
        await query.answer("This listing is no longer available.", show_alert=True)
        return
    if method == "invite":
        try:
            existing_member = await context.bot.get_chat_member(listing["chat_id"], buyer_id)
        except Exception:
            existing_member = None
        if existing_member and existing_member.status not in ("left", "kicked"):
            await query.answer("You already have access to this chat. Invite delivery is not available.", show_alert=True)
            return

    try:
        order = await db.create_marketplace_order(buyer_id, listing_id, method)
    except PurchaseError as exc:
        await query.answer(str(exc), show_alert=True)
        return

    if method == "invite":
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=listing["chat_id"],
                name=f"Store order #{order['id']}",
                expire_date=datetime.now(timezone.utc) + timedelta(hours=24),
                creates_join_request=True,
            )
            if not await db.set_marketplace_invite_link(order["id"], invite.invite_link):
                raise RuntimeError("Could not save the buyer's invite link.")
        except Exception:
            await db.refund_marketplace_order(
                order["id"], buyer_id, "The bot could not create a secure invite link."
            )
            await query.answer("Invite creation failed. Your wallet was refunded.", show_alert=True)
            return
        await query.answer("✅ Order created!")
        await show(update, card(
            f"📦 Marketplace Order #{order['id']}",
            row("Listing", e(listing["title"])),
            row("Paid", money(order["price"])),
            row("Status", "Awaiting your join request"),
            "",
            "Open your personal invite link. The bot will approve the request "
            "only for this buyer and verify the join with Telegram.",
        ), kb(
            [url_btn("🔗 Open Personal Invite", invite.invite_link, "primary")],
            [btn("📦 My Marketplace Orders", "market:orders"), btn("🏠 Home", "home")],
        ))
    else:
        await query.answer("✅ Order created!")
        await show(update, card(
            f"📦 Marketplace Order #{order['id']}",
            row("Listing", e(listing["title"])),
            row("Paid", money(order["price"])),
            row("Status", "Awaiting ownership transfer"),
            "",
            "The seller has been notified. Once ownership is transferred to your "
            "Telegram account, the bot will verify it automatically.",
        ), kb([btn("📦 My Marketplace Orders", "market:orders"), btn("🏠 Home", "home")]))
        await send(
            context, order["seller_admin_id"],
            card(
                f"📦 Marketplace Order #{order['id']}",
                row("Listing", e(listing["title"])),
                row("Buyer", f"<code>{buyer_id}</code>"),
                row("Price", money(order["price"])),
                row("Delivery", "Transfer ownership"),
                "",
                "Transfer ownership to the buyer. The bot will detect the creator change.",
            ),
            kb([red("↩️ Refund Order", f"mktadm:refund:{order['id']}")]),
        )


async def market_orders_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    orders = await db.marketplace_orders(buyer_id=update.effective_user.id, limit=15)
    if not orders:
        await show(update, card("📦 Marketplace Orders", "You haven't purchased a channel or group yet."),
                   kb([btn("📣 Browse Marketplace", "market:home")], back("shop:0")))
        return
    rows = [[btn(
        f"#{order['id']} • {e(order['listing_title'])} • {order['status'].replace('_', ' ').title()}",
        f"market:order:{order['id']}",
    )] for order in orders]
    await show(update, card("📦 Marketplace Orders", "Your latest marketplace purchases."), kb(
        *rows, [btn("📣 Browse", "market:home")], back("shop:0")
    ))


async def market_order_cb(update: Update, context: Ctx) -> None:
    order_id = int(update.callback_query.data.split(":")[2])
    order = await db.get_marketplace_order(order_id)
    if not order or order["buyer_id"] != update.effective_user.id:
        await update.callback_query.answer("Order not found.", show_alert=True)
        return
    await update.callback_query.answer()
    text = card(
        f"📦 Marketplace Order #{order['id']}",
        row("Listing", e(order["listing_title"])),
        row("Price", money(order["price"])),
        row("Delivery", order["delivery_method"].title()),
        row("Status", order["status"].replace("_", " ").title()),
        row("Verification", e(order.get("verification") or "Pending")),
        row("Created", e(order["created_at"])),
    )
    rows = []
    if order.get("invite_link") and order["status"] == "awaiting_join":
        rows.append([url_btn("🔗 Open Personal Invite", order["invite_link"], "primary")])
        await show(update, text, kb(*rows, [btn("‹ Orders", "market:orders"), btn("🏠 Home", "home")]))
        return
    await show(update, text, kb([btn("‹ Orders", "market:orders"), btn("🏠 Home", "home")]))


# ---------------------------------------------------------------- admin marketplace
async def market_admin_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if not is_admin(update.effective_user.id):
        await query.answer("Admins only.", show_alert=True)
        return
    parts = query.data.split(":")
    action = parts[1] if len(parts) > 1 else "home"
    args = parts[2:]
    await query.answer()

    if action in ("home", ""):
        listings = await db.marketplace_listing_count(status="active")
        orders = await db.marketplace_order_count()
        await show(update, card(
            "⚙️ Channels & Groups Manager",
            row("Active listings", str(listings)),
            row("Marketplace orders", str(orders)),
            "",
            "Only verified channel/group owners can publish listings.",
        ), kb(
            [green("➕ Add Channel / Group", "mktadm:add")],
            [blue("📋 Active Listings", "mktadm:list")],
            [btn("📦 Marketplace Orders", "mktadm:orders")],
            back("adm"),
        ))
    elif action == "add":
        set_state(context, "marketplace_chat")
        await show(update, card(
            "➕ Add Channel / Group",
            "Send the public <code>@username</code> or numeric chat ID.",
            "",
            "Requirements:",
            "• You must be the actual creator.",
            "• The bot must be an admin with Invite Users permission.",
        ), kb([red("✖ Cancel", "mktadm")]))
    elif action == "list":
        listings = await db.marketplace_listings(status="active", limit=20)
        rows = [[btn(
            f"#{item['id']} • {e(item['title'])} • {money(item['price'])}",
            f"mktadm:view:{item['id']}",
        )] for item in listings]
        await show(update, card("📋 Active Listings", f"{len(listings)} shown."), kb(
            *rows, back("mktadm")
        ))
    elif action == "view":
        listing = await db.get_marketplace_listing(int(args[0]))
        if not listing:
            await query.message.reply_text("⚠️ Listing not found.")
            return
        await show(update, card(
            f"📋 Listing #{listing['id']}",
            row("Title", e(listing["title"])),
            row("Type", listing["type"]),
            row("Chat ID", str(listing["chat_id"])),
            row("Creator", str(listing["seller_admin_id"])),
            row("Price", money(listing["price"])),
            row("Status", listing["status"]),
            row("Verified", str(listing["verified"])),
        ), kb(
            [btn("💵 Change Price", f"mktadm:price:{listing['id']}")],
            [red("🙈 Hide Listing", f"mktadm:hide:{listing['id']}"),
             red("🗑 Delete Listing", f"mktadm:delete:{listing['id']}")],
            back("mktadm:list"),
        ))
    elif action == "price":
        listing_id = int(args[0])
        set_state(context, "marketplace_editprice", listing_id=listing_id)
        await show(update, card(
            "💵 Change Listing Price",
            f"Send the new price in INR for listing <b>#{listing_id}</b>."
        ), kb([red("✖ Cancel", f"mktadm:view:{listing_id}")]))
    elif action == "hide":
        changed = await db.hide_marketplace_listing(int(args[0]), update.effective_user.id)
        await show(update, card("✅ Listing hidden." if changed else "⚠️ Listing could not be hidden."),
                   kb(back("mktadm:list")))
    elif action == "delete":
        await db.delete_marketplace_listing(int(args[0]), update.effective_user.id)
        await show(update, card("✅ Listing archived."), kb(back("mktadm:list")))
    elif action == "orders":
        orders = await db.marketplace_orders(limit=20)
        rows = [[btn(
            f"#{order['id']} • {e(order['listing_title'])} • {order['status']}",
            f"mktadm:ord:{order['id']}",
        )] for order in orders]
        await show(update, card("📦 Marketplace Orders", f"{len(orders)} shown."), kb(
            *rows, back("mktadm")
        ))
    elif action == "ord":
        order = await db.get_marketplace_order(int(args[0]))
        if not order:
            await query.message.reply_text("⚠️ Order not found.")
            return
        rows = []
        if order["status"] in ("awaiting_transfer", "awaiting_join"):
            rows.append([red("↩️ Refund Buyer", f"mktadm:refund:{order['id']}")])
        await show(update, card(
            f"📦 Marketplace Order #{order['id']}",
            row("Listing", e(order["listing_title"])),
            row("Buyer", str(order["buyer_id"])),
            row("Price", money(order["price"])),
            row("Delivery", order["delivery_method"]),
            row("Status", order["status"]),
        ), kb(*rows, back("mktadm:orders")))
    elif action == "refund":
        order = await db.refund_marketplace_order(
            int(args[0]), update.effective_user.id, "Refunded by store admin"
        )
        if not order:
            await query.message.reply_text("⚠️ Order already completed or refunded.")
            return
        if order.get("invite_link"):
            try:
                await context.bot.revoke_chat_invite_link(order["chat_id"], order["invite_link"])
            except Exception:
                pass
        await send(context, order["buyer_id"], card(
            f"↩️ Marketplace Order #{order['id']} Refunded",
            f"{money(order['price'])} was returned to your wallet.",
        ))
        await show(update, card(f"✅ Order #{order['id']} refunded."), kb(back("mktadm:orders")))


@on_input("marketplace_editprice", admin=True)
async def marketplace_editprice_input(update: Update, context: Ctx, state: dict) -> None:
    try:
        price = round(float(update.message.text.replace(",", "").strip()), 2)
        if price <= 0 or price > 10000000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send a valid price greater than zero.")
        return
    changed = await db.update_marketplace_price(
        int(state["listing_id"]), price, update.effective_user.id
    )
    clear_state(context)
    if not changed:
        await update.message.reply_text("⚠️ Listing is no longer editable.")
        return
    await update.message.reply_text(
        f"✅ Marketplace listing price updated to <b>{money(price)}</b>.",
        reply_markup=kb([btn("⚙️ Marketplace Manager", "mktadm")]),
    )


@on_input("marketplace_chat", admin=True)
async def marketplace_chat_input(update: Update, context: Ctx, state: dict) -> None:
    try:
        info, kind = await _verify_listing_chat(
            context.bot, update.message.text.strip(), update.effective_user.id
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ {e(exc)}")
        return
    set_state(context, "marketplace_price", **info)
    await update.message.reply_text(
        card(
            "✅ Telegram ownership verified",
            row("Title", e(info["chat_title"])),
            row("Type", kind.title()),
            row("Username", e(info["username"] or "Private")),
            "",
            "Send the listing price in INR.",
        )
    )


@on_input("marketplace_price", admin=True)
async def marketplace_price_input(update: Update, context: Ctx, state: dict) -> None:
    try:
        price = round(float(update.message.text.replace(",", "").strip()), 2)
        if price <= 0 or price > 10000000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Send a valid price greater than zero.")
        return
    set_state(context, "marketplace_tags", **{
        key: state[key] for key in ("chat_id", "chat_title", "username", "type")
    }, price=price)
    await update.message.reply_text(
        "🏷️ Send optional decorative tags separated by commas (or send <code>-</code> for none)."
    )


@on_input("marketplace_tags", admin=True)
async def marketplace_tags_input(update: Update, context: Ctx, state: dict) -> None:
    tags = [] if update.message.text.strip() == "-" else [
        tag.strip().lstrip("#")[:32] for tag in update.message.text.split(",") if tag.strip()
    ][:8]
    saved = {
        key: state[key]
        for key in ("chat_id", "chat_title", "username", "type", "price")
    }
    saved["tags"] = tags
    set_state(context, "marketplace_description", **saved)
    await update.message.reply_text(
        "📝 Send a short listing description (or send <code>-</code> to skip)."
    )


@on_input("marketplace_description", admin=True)
async def marketplace_description_input(update: Update, context: Ctx, state: dict) -> None:
    description = "" if update.message.text.strip() == "-" else update.message.text.strip()[:700]
    data = {
        key: state[key]
        for key in ("chat_id", "chat_title", "username", "type", "price", "tags")
    }
    data.update({
        "description": description,
        "seller_admin_id": update.effective_user.id,
    })
    try:
        listing = await db.create_marketplace_listing(data)
    except Exception as exc:
        await update.message.reply_text(
            f"⚠️ Could not save this listing: {e(exc)}"
        )
        clear_state(context)
        return
    clear_state(context)
    await update.message.reply_text(
        card(
            "✅ Listing Published",
            row("Listing ID", str(listing["id"])),
            row("Title", e(listing["title"])),
            row("Type", listing["type"].title()),
            row("Price", money(listing["price"])),
            row("Tags", e(", ".join(data["tags"]) or "None")),
            row("Description", e(description or "None")),
            footer="Ownership and bot permissions were verified with Telegram.",
        ),
        reply_markup=kb([btn("⚙️ Marketplace Manager", "mktadm")]),
    )


# ---------------------------------------------------------------- verification events
async def marketplace_chat_member(update: Update, context: Ctx) -> None:
    member_update = update.chat_member
    if not member_update:
        return
    chat_id = member_update.chat.id
    changed_user = member_update.new_chat_member.user
    new_status = member_update.new_chat_member.status

    # Invite orders are completed only by their order-specific join-request
    # handler below; a normal public join must never complete a paid order.
    order = None
    verification = None
    if new_status == ChatMemberStatus.OWNER:
        order = await db.find_pending_transfer_order(chat_id, changed_user.id)
        verification = "telegram_creator_transfer"

    if not order:
        return

    # Confirm against Telegram's current membership state rather than trusting only
    # a stale event payload.
    try:
        current = await context.bot.get_chat_member(chat_id, changed_user.id)
    except Exception:
        return
    if verification == "telegram_creator_transfer" and current.status != ChatMemberStatus.OWNER:
        return
    if verification == "telegram_invite_join" and current.status in (
        "left", "kicked", "restricted"
    ):
        return

    completed = await db.complete_marketplace_order(order["id"], changed_user.id, verification)
    if not completed:
        return
    await send(context, changed_user.id, card(
        f"✅ Marketplace Order #{order['id']} Completed",
        row("Listing", e(order["listing_title"])),
        row("Verification", "Telegram confirmed ownership transfer."
            if verification == "telegram_creator_transfer"
            else "Telegram confirmed you joined using your personal invite."),
        row("Paid", money(order["price"])),
    ))
    await send(context, order["seller_admin_id"], card(
        f"✅ Marketplace Order #{order['id']} Completed",
        f"Telegram verification succeeded for <b>{e(order['listing_title'])}</b>.",
    ))


async def marketplace_join_request(update: Update, context: Ctx) -> None:
    request = update.chat_join_request
    if not request or not request.invite_link:
        return
    invite = request.invite_link.invite_link
    order = await db.find_marketplace_order_by_invite(request.chat.id, invite)
    if not order:
        return
    if request.from_user.id != order["buyer_id"]:
        await request.decline()
        await send(context, order["seller_admin_id"], card(
            f"⚠️ Unexpected join request • Order #{order['id']}",
            f"Someone other than the buyer tried to use the private invite link.",
            row("Telegram ID", str(request.from_user.id)),
        ))
        return
    await request.approve()
    try:
        current = await context.bot.get_chat_member(request.chat.id, request.from_user.id)
    except Exception:
        return
    if current.status not in (
        ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER
    ):
        return
    completed = await db.complete_marketplace_order(
        order["id"], request.from_user.id, "telegram_order_invite_join"
    )
    if completed:
        try:
            await context.bot.revoke_chat_invite_link(request.chat.id, invite)
        except Exception:
            pass
        await send(context, request.from_user.id, card(
            f"✅ Marketplace Order #{order['id']} Completed",
            row("Listing", e(order["listing_title"])),
            "Telegram confirmed that you joined using your personal invite link.",
            row("Paid", money(order["price"])),
        ))
        await send(context, order["seller_admin_id"], card(
            f"✅ Marketplace Order #{order['id']} Completed",
            f"Buyer joined <b>{e(order['listing_title'])}</b> using the order-specific invite.",
        ))


def register(app: Application) -> None:
    app.add_handler(CallbackQueryHandler(market_home_cb, pattern=r"^market:home$"))
    app.add_handler(CallbackQueryHandler(market_list_cb, pattern=r"^market:list:(channel|group):-?\d+$"))
    app.add_handler(CallbackQueryHandler(market_view_cb, pattern=r"^market:view:\d+$"))
    app.add_handler(CallbackQueryHandler(market_buy_cb, pattern=r"^market:buy:\d+:(transfer|invite)$"))
    app.add_handler(CallbackQueryHandler(market_orders_cb, pattern=r"^market:orders$"))
    app.add_handler(CallbackQueryHandler(market_order_cb, pattern=r"^market:order:\d+$"))
    app.add_handler(CallbackQueryHandler(market_admin_cb, pattern=r"^mktadm(?::.*)?$"))
    app.add_handler(ChatMemberHandler(marketplace_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(marketplace_join_request))
