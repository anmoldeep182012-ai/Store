"""Customer-facing screens: home, shop, purchase, orders, wallet, referral."""
from __future__ import annotations

import math
import time

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from core import Ctx, clear_state, is_admin, log_event, on_input, set_state, show, user_link
from database import PurchaseError, db
from services import otp, provider
from ui.theme import (
    back, back_home, blue, btn, card, copy_btn, e, green, kb, local_time, money, pager, red, row,
    stock_bar, stock_label, url_btn,
)

COUNTRIES_PER_PAGE = 10
ORDERS_PER_PAGE = 8
MAX_QTY = 10
OTP_COOLDOWN_SEC = 15


# ----------------------------------------------------------------- home
async def home_screen(update: Update, context: Ctx) -> None:
    clear_state(context)
    user = update.effective_user
    data = await db.get_user(user.id)
    store = e(await db.get_setting("store_name"))
    orders = await db.count_user_orders(user.id)

    text = card(
        f"✨ {store}",
        f"👋 Welcome, <b>{e(user.first_name)}</b>!",
        "",
        row("💰 Balance", money(data["balance"])),
        row("📦 Orders", str(orders)),
        row("🆔 User ID", f"<code>{user.id}</code>"),
        footer="Fresh accounts • Instant delivery • 24/7",
    )
    rows = [
        [blue("🛒  Buy Accounts", "shop:0")],
        [green("💳 Deposit", "dep"), btn("👛 Wallet", "wallet")],
        [btn("📦 My Orders", "orders:0"), btn("🎁 Refer & Earn", "ref")],
        [btn("🎟 Redeem Code", "redeem"), btn("🆘 Help", "help")],
    ]
    if is_admin(user.id):
        rows.append([red("⚙️ Admin Panel", "adm")])
    await show(update, text, kb(*rows))


async def start_cmd(update: Update, context: Ctx) -> None:
    await home_screen(update, context)


async def home_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    await home_screen(update, context)


async def noop_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()


# ----------------------------------------------------------------- shop
async def shop_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    countries = await db.countries()
    total_stock = sum(country["stock"] for country in countries)
    await show(update, card(
        "🛒 Buy Accounts",
        "Choose where you want to shop.",
        "",
        row("🖥 Server 1", f"{len(countries)} countries • {total_stock} accounts"),
        row("🖥 Server 2", "TG-Lion provider stock"),
        row("📢 Marketplace", "Verified channels & groups"),
        footer="Server 1, Server 2 and Marketplace inventories are separate.",
    ), kb(
        [blue("📦 Server 1 [Owner Stock]", "shop1:0")],
        [blue("🔎 Search Server 2 [Stable Acc]", "s2search")],
        [blue("📢 Channels & Groups", "market:home")],
        back("home"),
    ))


async def server1_shop_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[1])
    countries = await db.countries()
    if not countries:
        await show(update, card("🛒 Store", "No countries are listed yet. Please check back soon."),
                   kb(back()))
        return

    pages = math.ceil(len(countries) / COUNTRIES_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = countries[page * COUNTRIES_PER_PAGE:(page + 1) * COUNTRIES_PER_PAGE]
    total_stock = sum(c["stock"] for c in countries)

    buttons, pair = [], []
    for c in chunk:
        mark = "" if c["stock"] else " ✖"
        pair.append(btn(f"{c['flag']} {c['name']} • {money(c['price'])}{mark}",
                        f"c:{c['id']}:1", style="primary" if c["stock"] else None))
        if len(pair) == 2:
            buttons.append(pair)
            pair = []
    if pair:
        buttons.append(pair)

    text = card(
        "🛒 Choose a Country",
        row("🌍 Countries", str(len(countries))),
        row("📦 Total stock", str(total_stock)),
        "",
        "Tap a country to see price and stock.",
        footer="✖ = out of stock",
    )
    await show(update, text, kb(*buttons, pager("shop1", page, pages),
                                [blue("🔎 Search Server 2 [Stable Acc]", "s2search")], back("shop:0")))


async def country_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    _, cid, qty = query.data.split(":")
    country = await db.get_country(int(cid))
    if not country or not country["active"]:
        await query.answer("This country is no longer available.", show_alert=True)
        return
    await query.answer()

    stock = country["stock"]
    qty = max(1, min(int(qty), MAX_QTY, stock or 1))
    user = await db.get_user(update.effective_user.id)
    total = country["price"] * qty

    text = card(
        f"{country['flag']} {e(country['name'])}",
        row("💵 Price", f"{money(country['price'])} / account"),
        row("📦 Stock", f"{stock_bar(stock)} {stock}"),
        f"▸ Status: {stock_label(stock)}",
        "",
        row("🔢 Quantity", str(qty)),
        row("🧾 Total", money(total)),
        row("💰 Your balance", money(user["balance"])),
        footer="Every account is delivered instantly after payment.",
    )
    rows = []
    if stock:
        rows.append([
            btn("➖", f"c:{cid}:{qty - 1}") if qty > 1 else btn("•", "noop"),
            btn(f"{qty}", "noop"),
            btn("➕", f"c:{cid}:{qty + 1}") if qty < min(stock, MAX_QTY) else btn("•", "noop"),
        ])
        if user["balance"] >= total:
            rows.append([green(f"⚡ Buy Now • {money(total)}", f"cf:{cid}:{qty}")])
        else:
            need = total - user["balance"]
            rows.append([blue(f"💳 Add {money(need)} to buy", "dep")])
    else:
        rows.append([btn("🔔 Out of stock. Check back later", "noop")])
    rows.append(back_home("shop1:0"))
    await show(update, text, kb(*rows))


async def confirm_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    await query.answer()
    _, cid, qty = query.data.split(":")
    country = await db.get_country(int(cid))
    if not country:
        return
    qty = int(qty)
    text = card(
        "🧾 Confirm Purchase",
        row("🌍 Country", f"{country['flag']} {e(country['name'])}"),
        row("🔢 Quantity", str(qty)),
        row("💵 Total", money(country["price"] * qty)),
        "",
        "The amount will be deducted from your wallet.",
    )
    await show(update, text, kb(
        [green("✅ Confirm & Pay", f"pay:{cid}:{qty}"), red("✖ Cancel", f"c:{cid}:{qty}")],
    ))


async def pay_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    _, cid, qty = query.data.split(":")
    user = update.effective_user
    qty = max(1, min(int(qty), MAX_QTY))
    try:
        orders = await db.purchase(user.id, int(cid), qty)
    except PurchaseError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer("✅ Purchase successful!")

    total = sum(o["price"] for o in orders)
    balance = (await db.get_user(user.id))["balance"]
    first = orders[0]
    summary = card(
        "✅ Purchase Successful",
        row("🌍 Country", f"{first['country_flag']} {e(first['country_name'])}"),
        row("🔢 Accounts", str(len(orders))),
        row("💵 Paid", money(total)),
        row("💰 Balance left", money(balance)),
        "",
        "Your account details are below 👇" if len(orders) > 1 else None,
    )
    if len(orders) == 1:
        await show(update, delivery_text(first), delivery_keyboard(first))
    else:
        await show(update, summary, kb([btn("📦 My Orders", "orders:0"), btn("🏠 Home", "home")]))
        for order in orders:
            await update.effective_chat.send_message(delivery_text(order),
                                                     reply_markup=delivery_keyboard(order))

    await log_event(context, card(
        "🛍 New Sale",
        f"▸ Buyer: {user_link(user.id, user.username, user.first_name)}",
        row("Country", f"{first['country_flag']} {e(first['country_name'])}"),
        row("Qty", str(len(orders))),
        row("Total", money(total)),
        row("Orders", ", ".join(f"#{o['id']}" for o in orders)),
    ))


def delivery_text(order: dict) -> str:
    otp_line = ("2️⃣ Tap <b>📲 Get OTP</b> below to receive the login code."
                if order.get("session") and otp.available()
                else "2️⃣ Contact support with your order ID to receive the login code.")
    return card(
        f"🎉 Order #{order['id']} Delivered",
        row("🌍 Country", f"{order['country_flag']} {e(order['country_name'])}"),
        f"▸ 📱 Phone: <code>{e(order['phone'])}</code>",
        f"▸ 🔐 2FA: <code>{e(order['twofa'] or 'Not set')}</code>",
        row("💵 Price", money(order["price"])),
        row("🕒 Date", local_time(order["created_at"])),
        "",
        "<b>How to log in</b>",
        "1️⃣ Open Telegram ➜ Add Account ➜ enter the phone number.",
        otp_line,
        "3️⃣ Enter the 2FA password if asked.",
        footer="Tap the phone or 2FA to copy it.",
    )


def delivery_keyboard(order: dict, back_label: str = "📦 My Orders"):
    copy_row = [copy_btn("📋 Copy Phone", order["phone"])]
    if order["twofa"]:
        copy_row.append(copy_btn("📋 Copy 2FA", order["twofa"]))
    return kb(
        copy_row,
        [blue("📲 Get OTP", f"otp:{order['id']}")],
        [btn(back_label, "orders:0"), btn("🏠 Home", "home")],
    )


# -------------------------------------------------------------- server 2
async def server2_search_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    if not provider.configured():
        await show(update, card(
            "🔎 Server 2",
            "Server 2 is not configured yet.",
            "Please contact the store administrator.",
        ), kb(back()))
        return
    set_state(context, "server2_search")
    await show(update, card(
        "🔎 Search Server 2 [Stable Acc]",
        "Send a country name, code, or a few letters.",
        "",
        "Examples: <code>United Kingdom</code>, <code>UK</code>, <code>GB</code>, <code>India</code>, <code>in</code>, <code>uni</code>",
        footer="Server 2 contains provider accounts only. Server 1 stock is separate.",
    ), kb([red("✖ Cancel", "shop:0")]))


def _server2_country_matches(countries: list[dict], query: str) -> list[dict]:
    """Match codes and names, treating UK as an alias for United Kingdom."""
    query = " ".join(query.casefold().split())
    if not query:
        return []

    # UK means United Kingdom (GB), not Ukraine (UA).
    if query in {"uk", "gb", "britain", "great britain", "united kingdom"}:
        return [
            country for country in countries
            if country["code"].upper() in {"GB", "UK"}
            or country["name"].casefold().startswith("united kingdom")
        ]

    # Exact ISO/provider code wins before any partial-name match.
    exact_code = [
        country for country in countries
        if country["code"].casefold() == query
    ]
    if exact_code:
        return exact_code

    exact_name = [
        country for country in countries
        if country["name"].casefold().strip() == query
    ]
    if exact_name:
        return exact_name

    # Short prefixes such as "uni" can match United Kingdom/States/etc.
    # Longer fragments can match anywhere in a country name.
    return [
        country for country in countries
        if country["name"].casefold().startswith(query)
        or country["code"].casefold().startswith(query)
        or (len(query) >= 4 and query in country["name"].casefold())
    ]


@on_input("server2_search")
async def server2_search_input(update: Update, context: Ctx, state: dict) -> None:
    query = (update.message.text or "").strip().casefold()
    if not query:
        await update.message.reply_text("❌ Send a country name, code, or a few letters.")
        return
    try:
        countries = await provider.available_countries()
    except provider.ProviderError as exc:
        clear_state(context)
        await update.message.reply_text(f"⚠️ {e(exc)}", reply_markup=kb(back()))
        return

    matches = _server2_country_matches(countries, query)
    if not matches:
        clear_state(context)
        await update.message.reply_text(
            f"❌ No Server 2 country matched <b>{e(update.message.text)}</b>.",
            reply_markup=kb([blue("🔎 Search Again", "s2search"), btn("🏠 Home", "home")]),
        )
        return

    clear_state(context)
    matches = sorted(matches, key=lambda item: (item["qty"] == 0, item["name"].casefold()))
    markup_percent = float(await db.get_setting("server2_markup") or 0)
    usd_inr = float(await db.get_setting("server2_usd_inr") or 0)
    if usd_inr <= 0:
        await update.message.reply_text("⚠️ Server 2 INR conversion rate is not configured.")
        return

    rows = []
    for country in matches[:20]:
        selling = round(country["price_usd"] * usd_inr * (1 + markup_percent / 100), 2)
        stock = "✖" if country["qty"] <= 0 else f"📦 {country['qty']}"
        rows.append([btn(
            f"{country['name']} • {money(selling)} • {stock}",
            f"s2:{country['code']}",
            style="primary" if country["qty"] > 0 else None,
        )])

    text = card(
        "🔎 Server 2 [Stable Acc]",
        row("🔍 Search", e(update.message.text)),
        row("🌍 Matches", str(len(matches))),
        "",
        "Select a country to buy from Server 2.",
        footer=f"Markup: {markup_percent:g}% • USD/INR: {usd_inr:g}",
    )
    await update.message.reply_text(
        text,
        reply_markup=kb(*rows, [btn("🔎 Search Again", "s2search")], [btn("🏠 Home", "home")]),
    )


async def server2_country_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    code = query.data.split(":")[1].upper()
    try:
        countries = await provider.available_countries()
    except provider.ProviderError as exc:
        await query.answer(str(exc), show_alert=True)
        return

    country = next((c for c in countries if c["code"] == code), None)
    if not country:
        await query.answer("Country is no longer available.", show_alert=True)
        return
    if country["qty"] <= 0:
        await query.answer("Server 2 is out of stock for this country.", show_alert=True)
        return

    markup_percent = float(await db.get_setting("server2_markup") or 0)
    usd_inr = float(await db.get_setting("server2_usd_inr") or 0)
    if usd_inr <= 0:
        await query.answer("Server 2 INR rate is not configured.", show_alert=True)
        return

    selling = round(country["price_usd"] * usd_inr * (1 + markup_percent / 100), 2)
    user = await db.get_user(update.effective_user.id)
    await query.answer()

    text = card(
        f"🌍 {e(country['name'])}",
        row("🖥 Server", "Server 2 • Stable Acc"),
        row("💵 Provider price", f"USD {country['price_usd']:.4f}"),
        row("📈 Commission", f"{markup_percent:g}%"),
        row("💰 Your price", money(selling)),
        row("📦 Stock", str(country["qty"])),
        row("💳 Your balance", money(user["balance"])),
        "",
        "Server 2 accounts come directly from the configured provider.",
    )
    rows = []
    if user["balance"] >= selling:
        rows.append([green(f"⚡ Buy Now • {money(selling)}", f"s2buy:{code}")])
    else:
        rows.append([blue(f"💳 Add {money(selling - user['balance'])}", "dep")])
    rows.append([btn("🔎 Search Again", "s2search"), btn("🏠 Home", "home")])
    await show(update, text, kb(*rows))


async def server2_buy_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    code = query.data.split(":")[1].upper()
    user_id = update.effective_user.id
    reserved = 0.0

    try:
        countries = await provider.available_countries()
        country = next((c for c in countries if c["code"] == code), None)
        if not country or country["qty"] <= 0:
            await query.answer("Country is out of stock.", show_alert=True)
            return

        markup_percent = float(await db.get_setting("server2_markup") or 0)
        usd_inr = float(await db.get_setting("server2_usd_inr") or 0)
        if usd_inr <= 0:
            raise provider.ProviderError("Server 2 INR conversion rate is not configured.")

        estimated = round(country["price_usd"] * usd_inr * (1 + markup_percent / 100), 2)
        user = await db.get_user(user_id)
        if user["balance"] < estimated:
            await query.answer("Insufficient wallet balance.", show_alert=True)
            return

        await db.reserve_server2_balance(
            user_id, estimated, f"Server 2 {country['name']}"
        )
        reserved = estimated

        try:
            purchased = await provider.buy_number(code, country["price_usd"])
        except Exception:
            await db.refund_server2_balance(user_id, estimated, "Server 2 purchase failed")
            reserved = 0.0
            raise

        actual_price = round(
            purchased["price_usd"] * usd_inr * (1 + markup_percent / 100), 2
        )
        if actual_price > estimated:
            await db.refund_server2_balance(user_id, estimated, "Server 2 price mismatch")
            reserved = 0.0
            await query.answer("Provider price changed. Please try again.", show_alert=True)
            return

        if estimated > actual_price:
            await db.refund_server2_balance(user_id, estimated - actual_price, "Server 2 price adjustment")
            reserved = actual_price

        order_id = await db.create_server2_order(
            user_id=user_id,
            country_code=code,
            country_name=purchased["name"],
            provider_price_usd=purchased["price_usd"],
            price=actual_price,
            number=purchased["number"],
        )
        reserved = 0.0
    except provider.ProviderError as exc:
        if reserved:
            await db.refund_server2_balance(user_id, reserved, "Server 2 purchase failed")
        await query.answer(str(exc), show_alert=True)
        return
    except Exception:
        if reserved:
            await db.refund_server2_balance(user_id, reserved, "Server 2 purchase failed")
        await query.answer("⚠️ Server 2 purchase failed. Your reserved balance was refunded.", show_alert=True)
        return

    await query.answer("✅ Server 2 account purchased!")
    await show(update, server2_delivery_text({
        "id": order_id,
        "country_name": purchased["name"],
        "country_code": code,
        "number": purchased["number"],
        "price": actual_price,
        "code": None,
        "password": None,
    }), server2_delivery_keyboard({"id": order_id, "number": purchased["number"]}))
    await log_event(context, card(
        "🖥 Server 2 Sale",
        f"▸ Buyer: {user_link(user_id, update.effective_user.username, update.effective_user.first_name)}",
        row("Country", e(purchased["name"])),
        row("Provider price", f"USD {purchased['price_usd']:.4f}"),
        row("Customer price", money(actual_price)),
        row("Order", f"#{order_id}"),
    ))


def server2_delivery_text(order: dict) -> str:
    return card(
        f"🎉 Server 2 Order #{order['id']}",
        row("🖥 Server", "Server 2 • Stable Acc"),
        row("🌍 Country", e(order["country_name"])),
        f"▸ 📱 Phone: <code>{e(order['number'])}</code>",
        f"▸ 📲 OTP: <code>{e(order.get('code') or 'Tap Get OTP')}</code>",
        f"▸ 🔐 Password: <code>{e(order.get('password') or 'Tap Get OTP')}</code>",
        row("💵 Paid", money(order["price"])),
        "",
        "Use <b>📲 Get OTP</b> below when Telegram asks for the login code.",
        footer="Server 2 provider stock is separate from owner-added Server 1 stock.",
    )


def server2_delivery_keyboard(order: dict):
    return kb(
        [copy_btn("📋 Copy Phone", str(order["number"]))],
        [blue("📲 Get OTP", f"s2otp:{order['id']}")],
        [btn("📦 Server 2 Orders", "s2orders:0"), btn("🏠 Home", "home")],
    )


async def server2_otp_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    order = await db.get_server2_order(int(query.data.split(":")[1]))
    if not order or order["user_id"] != update.effective_user.id:
        await query.answer("Order not found.", show_alert=True)
        return

    if order.get("code") and order.get("password"):
        await query.answer("OTP already fetched.", show_alert=True)
        await show(update, server2_delivery_text(order), server2_delivery_keyboard(order))
        return

    await query.answer("⏳ Requesting code…")
    try:
        credentials = await provider.get_code(order["number"])
    except provider.ProviderError as exc:
        await query.message.reply_text(
            f"⌛ {e(exc)}\\n\\nIf you just requested the Telegram login code, wait a few seconds and tap <b>📲 Get OTP</b> again."
        )
        return

    order = await db.update_server2_credentials(
        order["id"], credentials["code"], credentials["pass"]
    )
    await show(update, server2_delivery_text(order), server2_delivery_keyboard(order))


async def server2_orders_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    user_id = update.effective_user.id
    page = int(update.callback_query.data.split(":")[1])
    total = await db.count_server2_orders(user_id)
    if not total:
        await show(update, card("📦 Server 2 Orders", "You haven't bought a Server 2 account yet."),
                   kb([blue("🔎 Search Server 2", "s2search"), btn("🏠 Home", "home")]))
        return
    pages = math.ceil(total / ORDERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    orders = await db.server2_orders(user_id, page * ORDERS_PER_PAGE, ORDERS_PER_PAGE)
    rows = [[btn(
        f"#{o['id']} • {e(o['country_code'])} • {money(o['price'])}",
        f"s2o:{o['id']}"
    )] for o in orders]
    await show(update, card("📦 Server 2 Orders", row("Total orders", str(total)),
                            "", "Tap an order to view it."),
               kb(*rows, pager("s2orders", page, pages),
                  [btn("🔎 Search Server 2", "s2search")], back()))


async def server2_order_cb(update: Update, context: Ctx) -> None:
    order = await db.get_server2_order(int(update.callback_query.data.split(":")[1]))
    if not order or order["user_id"] != update.effective_user.id:
        await update.callback_query.answer("Order not found.", show_alert=True)
        return
    await update.callback_query.answer()
    await show(update, server2_delivery_text(order), server2_delivery_keyboard(order))


# --------------------------------------------------------------- orders
async def orders_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    page = int(query.data.split(":")[1])
    total = await db.count_user_orders(user_id)
    if not total:
        await show(update, card("📦 My Orders", "You haven't bought anything yet."),
                   kb([blue("🛒 Start Shopping", "shop:0")], back()))
        return
    pages = math.ceil(total / ORDERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    orders = await db.user_orders(user_id, page * ORDERS_PER_PAGE, ORDERS_PER_PAGE)
    buttons = [[btn(f"#{o['id']} • {o['country_flag']} {o['phone']} • {money(o['price'])}",
                    f"o:{o['id']}")] for o in orders]
    text = card("📦 My Orders", row("Total orders", str(total)), "",
                "Tap an order to view its details.")
    await show(update, text, kb(*buttons, pager("orders", page, pages), [btn("🖥 Server 2 Orders", "s2orders:0")], back()))


async def order_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    order = await db.get_order(int(query.data.split(":")[1]))
    if not order or order["user_id"] != update.effective_user.id:
        await query.answer("Order not found.", show_alert=True)
        return
    await query.answer()
    await show(update, delivery_text(order), delivery_keyboard(order, back_label="‹ Back"))


async def otp_cb(update: Update, context: Ctx) -> None:
    query = update.callback_query
    order = await db.get_order(int(query.data.split(":")[1]))
    if not order or order["user_id"] != update.effective_user.id:
        await query.answer("Order not found.", show_alert=True)
        return

    support = await db.get_setting("support")
    if not order["session"] or not otp.available():
        await query.answer(f"Auto OTP is not available for this account. "
                           f"Contact {support or 'support'} with order #{order['id']}.",
                           show_alert=True)
        return

    hours = int(await db.get_setting("otp_hours") or 24)
    if order["created_at"] < _hours_ago(hours):
        await query.answer(f"The OTP window ({hours}h) for this order has ended.", show_alert=True)
        return

    key = f"otp_last_{order['id']}"
    wait = OTP_COOLDOWN_SEC - (time.monotonic() - context.user_data.get(key, 0))
    if wait > 0:
        await query.answer(f"Please wait {int(wait) + 1}s before trying again.")
        return
    context.user_data[key] = time.monotonic()

    await query.answer("⏳ Checking for the code…")
    code, status = await otp.fetch_code(order["session"])
    if status == "ok":
        await update.effective_chat.send_message(
            card("📲 Login Code", f"▸ Order: <b>#{order['id']}</b>",
                 f"▸ Phone: <code>{e(order['phone'])}</code>",
                 f"▸ Code: <code>{code}</code>",
                 footer="Never share this code with anyone."),
            reply_markup=kb([copy_btn("📋 Copy Code", code)]),
        )
    elif status == "none":
        await query.message.reply_text(
            "⌛ No code yet. Request the code in Telegram first, wait a few seconds, "
            "then tap <b>📲 Get OTP</b> again.")
    else:
        await query.message.reply_text(
            f"⚠️ Could not read the code automatically. Contact {e(support or 'support')} "
            f"with order <b>#{order['id']}</b>.")


def _hours_ago(hours: int) -> str:
    from database import ago
    return ago(hours=hours)


# --------------------------------------------------------------- wallet
async def wallet_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    user = await db.get_user(update.effective_user.id)
    text = card(
        "👛 My Wallet",
        f"💰 <b>{money(user['balance'])}</b>",
        "",
        row("📥 Total deposited", money(user["total_deposit"])),
        row("🛍 Total spent", money(user["total_spent"])),
        row("🎁 Referral earnings", money(user["ref_earned"])),
    )
    await show(update, text, kb(
        [green("💳 Add Funds", "dep"), btn("📜 History", "txs")],
        back(),
    ))


TX_ICONS = {"deposit": "📥", "purchase": "🛍", "referral": "🎁", "coupon": "🎟", "admin": "🛠"}


async def txs_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    txs = await db.transactions(update.effective_user.id, 15)
    if not txs:
        lines = ["No transactions yet."]
    else:
        lines = [
            f"{TX_ICONS.get(t['type'], '•')} <b>{'+' if t['amount'] > 0 else '−'}"
            f"{money(abs(t['amount']))}</b> {e(t['note'] or t['type'])}\n"
            f"     <i>{local_time(t['created_at'])}</i>"
            for t in txs
        ]
    await show(update, card("📜 Transaction History", *lines, footer="Last 15 transactions"),
               kb(back("wallet")))


# ------------------------------------------------------------- referral
async def ref_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    user_id = update.effective_user.id
    user = await db.get_user(user_id)
    count = await db.referral_count(user_id)
    percent = await db.get_setting("ref_percent")
    link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    text = card(
        "🎁 Refer & Earn",
        f"Earn <b>{e(percent)}%</b> of every deposit your friends make, forever.",
        "",
        row("👥 Friends invited", str(count)),
        row("💰 Earned", money(user["ref_earned"])),
        "",
        f"🔗 Your link:\n<code>{link}</code>",
    )
    share = f"https://t.me/share/url?url={link}"
    await show(update, text, kb(
        [url_btn("📤 Share Link", share, "primary"), copy_btn("📋 Copy Link", link)],
        back(),
    ))


# --------------------------------------------------------------- redeem
async def redeem_cb(update: Update, context: Ctx) -> None:
    await update.callback_query.answer()
    set_state(context, "redeem")
    await show(update, card("🎟 Redeem Code", "Send your gift code as a message."),
               kb([red("✖ Cancel", "home")]))


@on_input("redeem")
async def redeem_input(update: Update, context: Ctx, state: dict) -> None:
    clear_state(context)
    status, amount = await db.redeem_coupon(update.effective_user.id, update.message.text)
    messages = {
        "ok": f"🎉 Code redeemed! <b>{money(amount)}</b> was added to your wallet.",
        "invalid": "❌ This code is not valid.",
        "used": "⚠️ You have already used this code.",
        "exhausted": "⚠️ This code has reached its usage limit.",
    }
    await update.message.reply_text(messages[status],
                                    reply_markup=kb([btn("👛 Wallet", "wallet"), btn("🏠 Home", "home")]))


# ----------------------------------------------------------------- help
async def help_cb(update: Update, context: Ctx) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    support = await db.get_setting("support")
    text = card(
        "🆘 Help Center",
        "<b>How to buy</b>",
        "1️⃣ Add funds with 💳 <b>Deposit</b> (UPI).",
        "2️⃣ Open 🛒 <b>Buy Accounts</b> and choose Server 1, Server 2 or Channels & Groups.",
        "3️⃣ Confirm. The account is delivered instantly.",
        "4️⃣ Log in and tap 📲 <b>Get OTP</b> for the code.",
        "",
        "<b>Deposits</b>",
        "Pay the exact amount by UPI, then send the 12-digit UTR. "
        "Your wallet is credited once an admin verifies it.",
        "",
        row("📞 Support", e(support or "Not set")),
    )
    rows = []
    if support and support.startswith("@"):
        rows.append([url_btn("💬 Contact Support", f"https://t.me/{support[1:]}", "primary")])
    rows.append(back())
    await show(update, text, kb(*rows))


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["start", "menu"], start_cmd))
    app.add_handler(CommandHandler("help", help_cb))
    app.add_handler(CallbackQueryHandler(home_cb, pattern=r"^home$"))
    app.add_handler(CallbackQueryHandler(noop_cb, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(shop_cb, pattern=r"^shop:0$"))
    app.add_handler(CallbackQueryHandler(server1_shop_cb, pattern=r"^shop1:-?\d+$"))
    app.add_handler(CallbackQueryHandler(server2_search_cb, pattern=r"^s2search$"))
    app.add_handler(CallbackQueryHandler(server2_country_cb, pattern=r"^s2:[A-Z]+$"))
    app.add_handler(CallbackQueryHandler(server2_buy_cb, pattern=r"^s2buy:[A-Z]+$"))
    app.add_handler(CallbackQueryHandler(server2_otp_cb, pattern=r"^s2otp:\d+$"))
    app.add_handler(CallbackQueryHandler(server2_orders_cb, pattern=r"^s2orders:-?\d+$"))
    app.add_handler(CallbackQueryHandler(server2_order_cb, pattern=r"^s2o:\d+$"))
    app.add_handler(CallbackQueryHandler(country_cb, pattern=r"^c:\d+:-?\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_cb, pattern=r"^cf:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(pay_cb, pattern=r"^pay:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(orders_cb, pattern=r"^orders:-?\d+$"))
    app.add_handler(CallbackQueryHandler(order_cb, pattern=r"^o:\d+$"))
    app.add_handler(CallbackQueryHandler(otp_cb, pattern=r"^otp:\d+$"))
    app.add_handler(CallbackQueryHandler(wallet_cb, pattern=r"^wallet$"))
    app.add_handler(CallbackQueryHandler(txs_cb, pattern=r"^txs$"))
    app.add_handler(CallbackQueryHandler(ref_cb, pattern=r"^ref$"))
    app.add_handler(CallbackQueryHandler(redeem_cb, pattern=r"^redeem$"))
    app.add_handler(CallbackQueryHandler(help_cb, pattern=r"^help$"))
