"""TG-Lion Server 2 provider client.

Server 2 is intentionally separate from the owner's Server 1 inventory.
The client only talks to TG-Lion and never writes provider countries into
the local countries/accounts collections.
"""
from __future__ import annotations

from typing import Any
import logging

import httpx

import config


log = logging.getLogger(__name__)
BASE_URL = config.TG_LION_BASE_URL.strip() or "https://tg-lion.net/"


class ProviderError(Exception):
    """User-safe provider error."""


def configured() -> bool:
    return bool(config.TG_LION_API_KEY and config.TG_LION_YOUR_ID)


async def _request(action: str, **params: Any) -> dict:
    if not configured():
        raise ProviderError("Server 2 is not configured yet.")

    query = {
        "action": action,
        "apiKey": config.TG_LION_API_KEY,
        "YourID": config.TG_LION_YOUR_ID,
        **{k: v for k, v in params.items() if v is not None},
    }

    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            response = await client.get(BASE_URL, params=query)
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        log.warning("TG-Lion %s request timed out", action)
        raise ProviderError("TG-Lion did not respond in time. Try again shortly.") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        log.warning("TG-Lion %s returned HTTP %s", action, status)
        if status in (401, 403):
            raise ProviderError(
                f"TG-Lion rejected the credentials (HTTP {status}). Check TG_LION_API_KEY and TG_LION_YOUR_ID."
            ) from exc
        raise ProviderError(
            f"TG-Lion returned HTTP {status}. Check the API URL or try again later."
        ) from exc
    except httpx.RequestError as exc:
        log.warning("TG-Lion %s connection failed: %s", action, type(exc).__name__)
        raise ProviderError(
            "Could not connect to TG-Lion. Check TG_LION_BASE_URL and provider availability."
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "unknown")
        log.warning(
            "TG-Lion %s returned non-JSON content (HTTP %s, content-type %s)",
            action, response.status_code, content_type,
        )
        raise ProviderError(
            "TG-Lion returned an unexpected response instead of JSON. Check the API URL or contact TG-Lion support."
        ) from exc

    if not isinstance(data, dict):
        raise ProviderError("Server 2 returned an invalid response.")

    if str(data.get("status", "")).lower() not in ("", "ok"):
        message = str(data.get("message") or data.get("error") or "Provider request failed.")
        raise ProviderError(message)

    return data


async def available_countries() -> list[dict]:
    data = await _request("available_countries")
    raw = data.get("countries") or {}
    countries = []
    if isinstance(raw, dict):
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or key).upper()
            name = str(item.get("name") or code)
            try:
                price = float(item.get("price", 0))
                qty = int(item.get("qty", 0))
            except (TypeError, ValueError):
                continue
            countries.append({
                "code": code,
                "name": name,
                "price_usd": price,
                "qty": qty,
                "code_num": str(item.get("code_Num") or ""),
            })
    return countries


async def get_balance() -> str:
    data = await _request("get_balance")
    return str(data.get("balance", "Unknown"))


async def buy_number(country_code: str, max_price_usd: float | None = None) -> dict:
    data = await _request(
        "getNumber",
        country_code=country_code.lower(),
        maxPrice=f"{max_price_usd:.4f}" if max_price_usd is not None else None,
    )
    number = str(data.get("Number") or data.get("number") or "").strip()
    if not number:
        raise ProviderError("Server 2 did not return a phone number.")

    try:
        price = float(data.get("price", 0))
    except (TypeError, ValueError):
        raise ProviderError("Server 2 returned an invalid purchase price.")

    if price <= 0:
        raise ProviderError("Server 2 returned an invalid purchase price.")

    return {
        "number": number,
        "name": str(data.get("name") or country_code.upper()),
        "price_usd": price,
        "new_balance": data.get("new_balance"),
    }


async def get_code(number: str) -> dict:
    data = await _request("getCode", number=number)
    return {
        "number": str(data.get("Number") or number),
        "code": str(data.get("code") or ""),
        "pass": str(data.get("pass") or ""),
    }
