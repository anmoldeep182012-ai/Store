# 🛒 Telegram Account Store Bot

A Telegram bot for selling Telegram accounts, with colored buttons, UPI deposits, instant delivery, and a button-driven admin panel.

Built with **python-telegram-bot 22.8** (Bot API 10.0) and MongoDB (PyMongo async client).

## ✨ Features

### 🎨 UI
- **Colored buttons** using Bot API 9.4 `style`: 🔵 primary, 🟢 success, 🔴 danger. Telegram apps older than Feb 2026 show them in the default color.
- Screens update in place (message edit) so the chat stays clean.
- Card-style layouts, stock bars (▰▰▱), page navigation (◀️ 1/5 ▶️).
- **Copy buttons** for phone, 2FA, UPI ID, OTP code and referral link.

### 👤 Customers
- 🛒 Buy by country, with a quantity selector (1–10).
- 💳 UPI deposit with an auto-generated **QR code**. The user submits a 12-digit UTR and an admin verifies it.
- 📦 Order history with full account details.
- 📲 **Get OTP**: reads the login code automatically (needs a session string + `API_ID`/`API_HASH`).
- 👛 Wallet with transaction history.
- 🎁 Refer & earn (a % of every referred user's deposit).
- 🎟 Gift codes.

### ⚙️ Admin panel (`/admin` or the button on the home screen)
- 📊 Dashboard: sales today / 7 days / all time, users, stock, pending deposits.
- 🌍 Add, edit, hide and delete countries; change prices.
- 📦 Bulk-add stock by pasting lines or uploading a `.txt` file. Duplicates and invalid lines are reported.
- 💳 Approve or reject deposits, or approve a different amount.
- 👤 Find a user by ID or @username: add/deduct balance, ban/unban, send a direct message.
- 📢 Broadcast any message (text, photo, video…) to all users.
- 🎟 Create and delete gift codes.
- 🛠 Settings (store name, UPI, support, minimum deposit, referral %, OTP window) and maintenance mode.
- 💾 Database backup sent as a file.

### 🔒 Safety
- Purchases, wallet changes, and deposit approvals run in MongoDB transactions. Use a MongoDB Atlas replica-set cluster.
- Each UTR can be used only once. Payment requests expire. Each deposit can be approved only once, even with several admins.
- Rate limiting, banned-user blocking, admin-only checks on every admin action.
- All secrets come from `.env`. Session strings and 2FA passwords are encrypted before they are stored in MongoDB. Keep `SESSION_ENCRYPTION_KEY` private and unchanged across redeployments.
- Telegram user/chat/bot context and pending input state are persisted in MongoDB.

## 🗄 MongoDB setup

1. Create a MongoDB Atlas cluster and database user.
2. Add your deployment host to Atlas Network Access (or use a restricted allowlist).
3. Copy the Atlas connection string into `MONGODB_URI` in `.env` and set `MONGODB_DATABASE`.
4. Generate `SESSION_ENCRYPTION_KEY` once using the command in `.env.example`. Save it securely; losing or changing this key makes previously encrypted session strings unreadable.
5. Use an Atlas replica-set cluster: purchase and deposit operations rely on MongoDB transactions.

The MongoDB migration does not automatically import an existing `store.db` SQLite file. If your deployed bot already has records in SQLite, export/retain that database and migrate its data before switching the live deployment.

## 🚀 Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # then edit BOT_TOKEN, ADMIN_IDS, MONGODB_URI, ...
# Generate SESSION_ENCRYPTION_KEY using the command shown in .env.example
python bot.py
```

Requires Python 3.10+.

## 🖥 Run 24/7 on a VPS (e.g. via Termius)

Tested commands for Ubuntu 22.04 / 24.04. Connect to the server over SSH (Termius → New Host → IP, username, password), then:

```bash
# 1. Install tools
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# 2. Download the bot
git clone https://github.com/surojbera1000/Store.git
cd Store

# 3. Install dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 4. Configure (Ctrl+O, Enter to save, Ctrl+X to exit)
cp .env.example .env
nano .env

# 5. Test run (stop with Ctrl+C)
.venv/bin/python bot.py
```

Install it as a service so it keeps running after you close Termius and restarts on crash or reboot:

```bash
sudo tee /etc/systemd/system/store-bot.service > /dev/null <<EOF
[Unit]
Description=Telegram Store Bot
After=network-online.target

[Service]
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/.venv/bin/python bot.py
Restart=always
RestartSec=5
User=$(whoami)

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now store-bot
```

Useful commands:

| Task | Command |
|------|---------|
| Status | `sudo systemctl status store-bot` |
| Live logs | `sudo journalctl -u store-bot -f` |
| Restart (after editing `.env`) | `sudo systemctl restart store-bot` |
| Update to latest code | `git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart store-bot` |

## 📦 Adding stock

Admin Panel → 🌍 Countries & Stock → ➕ Add Country:
```
India | IN | 🇮🇳 | 45
```
Open the country → ➕ Add Stock and send one account per line (or a `.txt` file):
```
+919876543210 | 2fa_password | session_string
+14155550123 | - | -
```
Use `-` for empty fields. The **session** is a Telethon `StringSession`. When it is present and `API_ID`/`API_HASH` are set, the buyer's 📲 Get OTP button fetches the login code from Telegram (777000) automatically. Without a session, the buyer is asked to contact support.

## 🗂 Structure

```
bot.py               entry point, gate (ban / maintenance / rate limit), error handler
config.py            settings from .env
database.py          MongoDB collections and all data operations
mongo_persistence.py MongoDB persistence for Telegram user/chat/bot state
core.py              screen rendering, text-input states, admin notifications
ui/theme.py          colored button helpers, card layout, formatting
handlers/user.py     home, shop, purchase, orders, OTP, wallet, referral, gift codes, help
handlers/deposit.py  UPI QR, UTR, admin approve/reject
handlers/admin.py    admin panel
services/otp.py      optional Telethon OTP reader
```

## 💡 Tips
- Set `LOG_CHANNEL_ID` to get every sale and approved deposit posted to a private channel.
- Take a 💾 Backup regularly. The JSON backup contains customer and inventory data, so keep it private.

## Server 2 — TG-Lion [Stable Acc]

Server 2 is intentionally separate from the owner-managed Server 1 stock.

- Server 1: countries and accounts added by the owner/admin in MongoDB.
- Server 2: live countries/stock returned by the TG-Lion API.
- Users can search Server 2 by country name, code, or partial text.
- Server 2 prices are calculated as provider USD price × configured USD/INR rate × (1 + commission %).
- Admins can change the Server 2 commission and USD/INR rate from the admin panel.
- Server 2 orders are stored separately from Server 1 orders.
- TG-Lion API credentials belong in .env (TG_LION_API_KEY and TG_LION_YOUR_ID).

The provider API used by this integration documents available_countries, get_balance, getNumber, and getCode.
