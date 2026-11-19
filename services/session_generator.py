from __future__ import annotations

from getpass import getpass
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

import config

# Active in-memory Telethon clients per admin ID: {admin_id: dict}
_PENDING_SESSIONS: dict[int, dict[str, Any]] = {}


async def cleanup_pending_session(admin_id: int) -> None:
    """Disconnect and remove any pending login client for an admin."""
    entry = _PENDING_SESSIONS.pop(admin_id, None)
    if entry and entry.get("client"):
        try:
            await entry["client"].disconnect()
        except Exception:
            pass


async def start_session_login(admin_id: int, phone: str, twofa: str | None = None) -> None:
    """Step 1: Connect Telethon client, send OTP code, and cache pending state."""
    if not config.API_ID or not config.API_HASH:
        raise RuntimeError("API_ID or API_HASH is missing in .env")

    phone = phone.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    await cleanup_pending_session(admin_id)

    client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await client.disconnect()
        raise ValueError("Invalid phone number. Include country code (e.g. +919876543210).")
    except FloodWaitError as exc:
        await client.disconnect()
        raise RuntimeError(f"Telegram FloodWait: try again in {exc.seconds}s.")
    except Exception:
        await client.disconnect()
        raise

    _PENDING_SESSIONS[admin_id] = {
        "client": client,
        "phone": phone,
        "phone_code_hash": sent.phone_code_hash,
        "twofa": twofa.strip() if twofa else None,
    }


async def finish_session_login(admin_id: int, otp: str) -> tuple[str, str, str | None]:
    """Step 2: Verify OTP (and 2FA if needed), return (phone, string_session, twofa)."""
    entry = _PENDING_SESSIONS.get(admin_id)
    if not entry:
        raise RuntimeError("No active session request found. Run /gsession again.")

    client: TelegramClient = entry["client"]
    phone: str = entry["phone"]
    phone_code_hash: str = entry["phone_code_hash"]
    twofa: str | None = entry["twofa"]

    clean_otp = otp.strip().replace(" ", "").replace("-", "")
    if not clean_otp.isdigit():
        raise ValueError("OTP must contain digits only.")

    try:
        try:
            await client.sign_in(
                phone=phone,
                code=clean_otp,
                phone_code_hash=phone_code_hash,
            )
        except SessionPasswordNeededError:
            if not twofa:
                await cleanup_pending_session(admin_id)
                raise ValueError(
                    "2FA is enabled on this account, but no password was given.\n"
                    "Run <code>/gsession &lt;Number&gt; &lt;2FA&gt;</code> again."
                )
            try:
                await client.sign_in(password=twofa)
            except PasswordHashInvalidError:
                await cleanup_pending_session(admin_id)
                raise ValueError("Invalid 2FA password. Process cancelled.")

        if not await client.is_user_authorized():
            await cleanup_pending_session(admin_id)
            raise RuntimeError("Telegram authorization failed.")

        session_str = client.session.save()
        await cleanup_pending_session(admin_id)
        return phone, session_str, twofa

    except PhoneCodeInvalidError:
        # Keep session alive so admin can retry typing the OTP
        raise ValueError("Invalid OTP code. Check the code and send it again (or tap Cancel).")
    except PhoneCodeExpiredError:
        await cleanup_pending_session(admin_id)
        raise RuntimeError("OTP expired. Run /gsession again.")
    except Exception:
        if not isinstance(Exception, ValueError):
            await cleanup_pending_session(admin_id)
        raise


async def generate_string_session(phone: str) -> str:
    """Generate a Telethon StringSession through local terminal input (CLI tool)."""
    if not config.API_ID or not config.API_HASH:
        raise RuntimeError("API_ID or API_HASH is missing in .env")

    phone = phone.strip()
    if not phone:
        raise ValueError("Phone number is required.")

    client = TelegramClient(
        StringSession(),
        config.API_ID,
        config.API_HASH,
    )

    try:
        await client.connect()
        sent = await client.send_code_request(phone)

        for attempt in range(3):
            code = input("Enter Telegram login code: ").strip()

            try:
                await client.sign_in(
                    phone=phone,
                    code=code,
                    phone_code_hash=sent.phone_code_hash,
                )
                break

            except SessionPasswordNeededError:
                password = getpass("Enter your Telegram 2FA password: ")
                await client.sign_in(password=password)
                break

            except PhoneCodeInvalidError:
                if attempt == 2:
                    raise
                print("Invalid code. Try again.")

        if not await client.is_user_authorized():
            raise RuntimeError("Telegram authorization failed.")

        return client.session.save()

    finally:
        await client.disconnect()