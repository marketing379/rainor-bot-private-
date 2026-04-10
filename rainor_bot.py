#!/usr/bin/env python3
"""
Rainor Bot — Real-time Rain Protocol Market Monitor for Telegram.

Monitors the Rain Protocol production API for newly created prediction markets
and sends instant Telegram notifications to the admin.

Bot Token: 8652656523:AAFHhZ4KVLu1H3tYcmQ_Y5OAQbbWAUs8nik
API Source: https://prod-api.rain.one/pools/public-pools
"""

import asyncio
import json
import logging
import os
import signal
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import cloudscraper
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = "8652656523:AAFHhZ4KVLu1H3tYcmQ_Y5OAQbbWAUs8nik"
RAIN_API_BASE = "https://prod-api.rain.one"
MARKET_URL_TEMPLATE = "https://www.rain.one/detail?id={pool_id}"
DATA_DIR = Path(__file__).resolve().parent
ADMIN_FILE = DATA_DIR / "admin_data.json"
SEEN_MARKETS_FILE = DATA_DIR / "seen_markets.json"
ALERTED_ENDED_FILE = DATA_DIR / "alerted_ended_markets.json"
POLL_INTERVAL_SECONDS = 30   # How often to check for new markets
END_POLL_INTERVAL_SECONDS = 300  # How often to check for ended markets (5 min)
API_TIMEOUT = 15  # Timeout in seconds for each Rain API call

ACTIVE_STATUSES = ["Live", "Pending_Finalization", "Waiting_for_Result"]

# ---------------------------------------------------------------------------
# Blockchain configuration (Arbitrum One)
# ---------------------------------------------------------------------------

PRIVATE_KEY = "0e24d1a045f103b2ce237c27afbeaec058590ff2d21498a632e54cbab1d6c1a6"
ARBITRUM_RPC_URLS = [
    "https://arb1.arbitrum.io/rpc",
    "https://arbitrum-one.publicnode.com",
]
ARBITRUM_CHAIN_ID = 42161

# closePool() ABI — no arguments, nonpayable
CLOSE_POOL_ABI = [
    {
        "type": "function",
        "name": "closePool",
        "inputs": [],
        "outputs": [],
        "stateMutability": "nonpayable",
    }
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Configure the root logger only once (guards against duplicate handlers on restart)
_root_logger = logging.getLogger()
if not _root_logger.handlers:
    _fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    _handler_stdout = logging.StreamHandler(sys.stdout)
    _handler_stdout.setFormatter(logging.Formatter(_fmt))
    _handler_file = logging.FileHandler(DATA_DIR / "rainor.log")
    _handler_file.setFormatter(logging.Formatter(_fmt))
    _root_logger.setLevel(logging.INFO)
    _root_logger.addHandler(_handler_stdout)
    _root_logger.addHandler(_handler_file)

logger = logging.getLogger("rainor")
logger.propagate = True  # propagates to root; root handlers do the actual output

# ---------------------------------------------------------------------------
# Persistent data helpers
# ---------------------------------------------------------------------------


def load_json(path: Path, default=None):
    """Load JSON from *path*; return *default* if the file does not exist."""
    if default is None:
        default = {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    """Atomically write *data* as JSON to *path*."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)


def get_admin_chat_id() -> int | None:
    """Return the stored admin chat ID, or None."""
    data = load_json(ADMIN_FILE)
    cid = data.get("admin_chat_id")
    return int(cid) if cid else None


def set_admin_chat_id(chat_id: int):
    """Persist the admin chat ID."""
    data = load_json(ADMIN_FILE)
    data["admin_chat_id"] = chat_id
    save_json(ADMIN_FILE, data)
    logger.info("Admin chat ID stored: %s", chat_id)


def load_seen_markets() -> set:
    """Return the set of already-notified market IDs."""
    data = load_json(SEEN_MARKETS_FILE, default={"seen": []})
    return set(data.get("seen", []))


def save_seen_markets(seen: set):
    """Persist the set of seen market IDs."""
    save_json(SEEN_MARKETS_FILE, {"seen": sorted(seen)})


def load_alerted_ended() -> set:
    """Return the set of market IDs for which an end-time alert has been sent."""
    data = load_json(ALERTED_ENDED_FILE, default={"alerted": []})
    return set(data.get("alerted", []))


def save_alerted_ended(alerted: set):
    """Persist the set of alerted-ended market IDs."""
    save_json(ALERTED_ENDED_FILE, {"alerted": sorted(alerted)})


# ---------------------------------------------------------------------------
# Rain Protocol API helpers — all use a SHARED httpx client for efficiency
# and have individual try/except so a single failure never propagates up.
# ---------------------------------------------------------------------------

# Reusable client — created once, closed on shutdown
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    """Return (and lazily create) a long-lived async HTTP client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=API_TIMEOUT)
    return _http_client


async def close_http_client():
    """Close the shared HTTP client gracefully."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


async def fetch_public_pools(
    limit: int = 20,
    offset: int = 1,
    sort_by: str = "age",
    status: str | None = None,
) -> list[dict]:
    """Fetch public pools from the Rain production API.

    Returns a list of pool dicts, newest first when *sort_by* = 'age'.
    Never raises — returns [] on any failure.
    """
    params: dict = {
        "limit": limit,
        "offset": offset,
        "sortBy": sort_by,
    }
    if status:
        params["status"] = status

    url = f"{RAIN_API_BASE}/pools/public-pools"
    try:
        client = await get_http_client()
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        return body.get("data", {}).get("pools", [])
    except Exception as exc:
        logger.error("fetch_public_pools failed (status=%s): %s", status, exc)
        return []


async def fetch_pool_count(status: str | None = None) -> int:
    """Fetch just the *count* of pools matching the given status filter.

    Uses limit=1 to transfer minimal data. Returns 0 on failure.
    """
    params: dict = {"limit": 1, "offset": 1, "sortBy": "age"}
    if status:
        params["status"] = status
    url = f"{RAIN_API_BASE}/pools/public-pools"
    try:
        client = await get_http_client()
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        body = resp.json()
        return int(body.get("data", {}).get("count", 0))
    except Exception as exc:
        logger.error("fetch_pool_count failed (status=%s): %s", status, exc)
        return 0


# ---------------------------------------------------------------------------
# Rain API — aggregate stats (Cloudflare-protected, uses cloudscraper)
# ---------------------------------------------------------------------------

_cloudscraper_session: cloudscraper.CloudScraper | None = None


def _get_cloudscraper() -> cloudscraper.CloudScraper:
    """Return (and lazily create) a reusable cloudscraper session."""
    global _cloudscraper_session
    if _cloudscraper_session is None:
        _cloudscraper_session = cloudscraper.create_scraper()
    return _cloudscraper_session


async def fetch_total_users() -> int:
    """Fetch total registered user count from Rain API.

    Uses /users/users-total-count endpoint (Cloudflare-protected,
    so we use cloudscraper in a thread to bypass the JS challenge).
    Returns 0 on failure.
    """
    try:
        loop = asyncio.get_running_loop()
        def _sync_fetch():
            s = _get_cloudscraper()
            r = s.get(f"{RAIN_API_BASE}/users/users-total-count", timeout=15)
            r.raise_for_status()
            return int(r.json().get("data", {}).get("totalUsers", 0))
        return await loop.run_in_executor(None, _sync_fetch)
    except Exception as exc:
        logger.error("fetch_total_users failed: %s", exc)
        return 0


async def fetch_all_pools_count() -> int:
    """Fetch total pool count from /pools/get-all-pools-count.

    This endpoint is NOT behind Cloudflare, so httpx works fine.
    Returns 0 on failure.
    """
    url = f"{RAIN_API_BASE}/pools/get-all-pools-count"
    try:
        client = await get_http_client()
        resp = await client.get(url)
        resp.raise_for_status()
        return int(resp.json().get("data", {}).get("poolsCount", 0))
    except Exception as exc:
        logger.error("fetch_all_pools_count failed: %s", exc)
        return 0


async def fetch_pool_detail(pool_id: str) -> dict | None:
    """Fetch full details for a single pool by its ID."""
    url = f"{RAIN_API_BASE}/pools/pool/{pool_id}"
    try:
        client = await get_http_client()
        resp = await client.get(url)
        resp.raise_for_status()
        body = resp.json()
        return body.get("data")
    except Exception as exc:
        logger.error("fetch_pool_detail failed for %s: %s", pool_id, exc)
        return None


# ---------------------------------------------------------------------------
# In-memory cache for active pools — refreshed every 60 s in the background.
# Commands read from this cache instantly instead of making 3 API calls each.
# ---------------------------------------------------------------------------

_active_pools_cache: list[dict] = []
_active_pools_cache_ts: float = 0.0   # epoch seconds of last successful refresh
ACTIVE_CACHE_TTL = 60  # seconds


async def _refresh_active_pools_cache() -> None:
    """Fetch all active pools from the API and update the in-memory cache.
    Runs all 3 status requests concurrently with asyncio.gather.
    """
    global _active_pools_cache, _active_pools_cache_ts
    try:
        results = await asyncio.gather(
            *[fetch_public_pools(limit=100, offset=1, sort_by="age", status=s)
              for s in ACTIVE_STATUSES],
            return_exceptions=True,
        )
        merged: list[dict] = []
        for r in results:
            if isinstance(r, list):
                merged.extend(r)

        def _sort_key(p: dict):
            try:
                return datetime.fromisoformat(
                    p.get("createdAt", "2000-01-01").replace("Z", "+00:00")
                )
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        merged.sort(key=_sort_key, reverse=True)
        _active_pools_cache = merged
        _active_pools_cache_ts = asyncio.get_event_loop().time()
        logger.debug("Active pools cache refreshed: %d markets.", len(merged))
    except Exception as exc:
        logger.error("Failed to refresh active pools cache: %s", exc)


async def get_active_pools_cached() -> list[dict]:
    """Return the cached active pools, refreshing if the cache is stale or empty."""
    age = asyncio.get_event_loop().time() - _active_pools_cache_ts
    if not _active_pools_cache or age > ACTIVE_CACHE_TTL:
        await _refresh_active_pools_cache()
    return _active_pools_cache


async def fetch_active_pools_paginated(
    limit: int = 20,
    offset: int = 1,
    sort_by: str = "age",
) -> list[dict]:
    """Return a page of active (non-Closed) pools from the in-memory cache.

    *offset* is a 1-based page number; *limit* is the page size.
    The cache is refreshed automatically if stale (>60 s old).
    This function returns instantly when the cache is warm.
    """
    all_pools = await get_active_pools_cached()
    start = (offset - 1) * limit
    end = start + limit
    return all_pools[start:end]


# ---------------------------------------------------------------------------
# Blockchain helpers — close market on-chain via web3
# --------------------------------------------------------------------------def _close_market_blocking(contract_address: str) -> tuple[bool, str]:
    """Blocking helper: connect to Arbitrum and call closePool().
    Must be run in a thread executor to avoid blocking the event loop.
    """
    from web3 import Web3
    from eth_account import Account

    # Connect to Arbitrum One
    w3 = None
    for rpc_url in ARBITRUM_RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
            if w3.is_connected():
                break
        except Exception:
            continue

    if not w3 or not w3.is_connected():
        return False, "Could not connect to Arbitrum One RPC."

    pk = PRIVATE_KEY if PRIVATE_KEY.startswith("0x") else f"0x{PRIVATE_KEY}"
    account = Account.from_key(pk)
    sender = account.address

    checksum_addr = Web3.to_checksum_address(contract_address)
    contract = w3.eth.contract(address=checksum_addr, abi=CLOSE_POOL_ABI)

    try:
        nonce = w3.eth.get_transaction_count(sender)
        gas_price = w3.eth.gas_price
        tx = contract.functions.closePool().build_transaction({
            "from": sender,
            "nonce": nonce,
            "gasPrice": gas_price,
            "chainId": ARBITRUM_CHAIN_ID,
        })
        try:
            tx["gas"] = w3.eth.estimate_gas(tx)
        except Exception:
            tx["gas"] = 500_000

        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        tx_hash_hex = tx_hash.hex()
        arbiscan_url = f"https://arbiscan.io/tx/{tx_hash_hex}"

        if receipt.status == 1:
            return True, f"Transaction confirmed!\n<a href='{arbiscan_url}'>View on Arbiscan</a>"
        else:
            return False, f"Transaction reverted on-chain.\n<a href='{arbiscan_url}'>View on Arbiscan</a>"

    except Exception as exc:
        return False, str(exc)


async def close_market_on_chain(pool_id: str) -> tuple[bool, str]:
    """
    Close a market by calling closePool() on its smart contract.
    All blocking web3 calls are run in a thread executor to avoid
    blocking the asyncio event loop.

    Returns (success: bool, message: str).
    """
    try:
        # Step 1: Get the market's contract address from the API (async)
        detail = await fetch_pool_detail(pool_id)
        if not detail:
            return False, "Could not fetch market details from Rain API."

        contract_address = detail.get("contractAddress")
        if not contract_address:
            return False, "Market has no contract address."

        market_status = detail.get("status", "")
        if market_status == "Closed":
            return False, "Market is already closed."

        # Step 2: Run all blocking web3 calls in a thread executor
        loop = asyncio.get_running_loop()
        success, msg = await loop.run_in_executor(
            None, _close_market_blocking, contract_address
        )
        return success, msg
        # Step 3: Set up the account
        pk = PRIVATE_KEY if PRIVATE_KEY.startswith("0x") else f"0x{PRIVATE_KEY}"
        account = Account.from_key(pk)
        sender = account.address

        # Step 4: Build the closePool() transaction
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address),
            abi=CLOSE_POOL_ABI,
        )

        nonce = w3.eth.get_transaction_count(sender)
        gas_price = w3.eth.gas_price

        tx = contract.functions.closePool().build_transaction({
            "from": sender,
            "nonce": nonce,
            "gas": 300_000,  # generous gas limit for safety
            "gasPrice": gas_price,
            "chainId": ARBITRUM_CHAIN_ID,
            "value": 0,
        })

        # Step 5: Sign and send
        signed_tx = w3.eth.account.sign_transaction(tx, private_key=pk)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        logger.info(
            "closePool tx sent for market %s — tx hash: %s", pool_id, tx_hash_hex
        )

        # Step 6: Wait for receipt (with timeout)
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                return True, (
                    f"Market closed on-chain.\n"
                    f"TX: https://arbiscan.io/tx/0x{tx_hash_hex}"
                )
            else:
                return False, (
                    f"Transaction reverted.\n"
                    f"TX: https://arbiscan.io/tx/0x{tx_hash_hex}"
                )
        except Exception as wait_err:
            return False, (
                f"Transaction sent but receipt not confirmed: {wait_err}\n"
                f"TX: https://arbiscan.io/tx/0x{tx_hash_hex}"
            )

    except Exception as exc:
        logger.error("close_market_on_chain failed for %s: %s", pool_id, exc, exc_info=True)
        return False, f"Error: {exc}"


# ---------------------------------------------------------------------------
# OpenAI helper — Check Answer
# ---------------------------------------------------------------------------


def check_answer_with_ai(question: str, end_date: str) -> str:
    """Use OpenAI (gemini-2.5-flash) to research whether a prediction market
    question has a definitive real-world answer.

    Returns the formatted response text.
    """
    try:
        from openai import OpenAI
        client = OpenAI()

        system_prompt = (
            "You are a fact-checking research assistant. Your job is to determine "
            "whether a prediction market question has a definitive real-world answer.\n\n"
            "INSTRUCTIONS:\n"
            "1. Analyze the question carefully\n"
            "2. Consider what real-world events or data would resolve this question\n"
            "3. Based on your knowledge, determine if the answer is known\n"
            "4. Only report YES or NO if your confidence is 95% or higher\n"
            "5. If confidence is below 95%, report 'Insufficient data'\n\n"
            "You MUST respond in EXACTLY this JSON format:\n"
            "{\n"
            '  "verdict": "YES" or "NO" or "INSUFFICIENT_DATA",\n'
            '  "confidence": <integer 0-100>,\n'
            '  "sources": ["source 1 description", "source 2 description", "source 3 description"],\n'
            '  "summary": "Brief explanation of your reasoning"\n'
            "}\n\n"
            "For sources, list the types of information you checked (e.g., "
            "'Official government announcements', 'Major news outlets (Reuters, AP)', "
            "'Sports league official results', etc.).\n"
            "Respond ONLY with the JSON object, no other text."
        )

        user_prompt = (
            f"Prediction market question: \"{question}\"\n"
            f"Market end date: {end_date}\n"
            f"Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "Has this event/question been definitively resolved? "
            "Research from multiple angles and sources."
        )

        response = client.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1000,
        )

        raw = response.choices[0].message.content.strip()

        # Parse JSON response
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)

        verdict_raw = result.get("verdict", "INSUFFICIENT_DATA")
        confidence = result.get("confidence", 0)
        sources = result.get("sources", [])
        summary = result.get("summary", "No summary provided.")

        # Map verdict to emoji
        if verdict_raw == "YES" and confidence >= 95:
            verdict_display = "\u2705 YES"
        elif verdict_raw == "NO" and confidence >= 95:
            verdict_display = "\u274c NO"
        else:
            verdict_display = "\u26a0\ufe0f Insufficient data"

        # Format sources
        sources_text = ""
        for s in sources[:5]:
            sources_text += f"\u2022 {s}\n"
        if not sources_text:
            sources_text = "\u2022 No specific sources identified\n"

        return (
            f"\U0001f50d <b>Answer Check:</b> {question}\n\n"
            f"<b>Verdict:</b> {verdict_display}\n"
            f"<b>Confidence:</b> {confidence}%\n\n"
            f"<b>Sources checked:</b>\n{sources_text}\n"
            f"<b>Summary:</b> {summary}"
        )

    except json.JSONDecodeError:
        # If the AI didn't return valid JSON, return the raw text
        return (
            f"\U0001f50d <b>Answer Check:</b> {question}\n\n"
            f"<b>Verdict:</b> \u26a0\ufe0f Insufficient data\n"
            f"<b>Confidence:</b> N/A\n\n"
            f"<b>Summary:</b> Could not parse AI response. Raw output:\n{raw[:500]}"
        )
    except Exception as exc:
        logger.error("check_answer_with_ai failed: %s", exc, exc_info=True)
        return (
            f"\U0001f50d <b>Answer Check:</b> {question}\n\n"
            f"\u274c Error: {exc}"
        )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_end_date(raw: str | None) -> str:
    """Convert an ISO-8601 date string to a human-readable format."""
    if not raw:
        return "N/A"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y at %H:%M UTC")
    except Exception:
        return raw


def format_created_date(raw: str | None) -> str:
    """Convert an ISO-8601 date string to a human-readable format for creation date."""
    if not raw:
        return "N/A"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y at %H:%M UTC")
    except Exception:
        return raw


def shorten_wallet(addr: str) -> str:
    """Return a shortened wallet address like 0x1cc3…88a."""
    if addr and len(addr) > 12:
        return f"{addr[:6]}…{addr[-4:]}"
    return addr


def get_creator_display(pool: dict) -> str:
    """Extract and format the creator display name from a pool dict."""
    creator = (
        pool.get("poolOwnerNameOrWallet")
        or pool.get("poolOwnerWalletAddress")
        or "Unknown"
    )
    if creator.startswith("0x") and len(creator) > 12:
        return shorten_wallet(creator)
    return creator


# ---------------------------------------------------------------------------
# Inline keyboard helpers — per-market button rows
# ---------------------------------------------------------------------------


def market_buttons(pool_id: str) -> InlineKeyboardMarkup:
    """Return a 3-row InlineKeyboardMarkup for a market:
    Row 1: [🌐 Market] [⛔ Close]
    Row 2: [💧 Add Liquidity] [📊 Data]
    Row 3: [🔍 Check Answer]
    """
    url = MARKET_URL_TEMPLATE.format(pool_id=pool_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("\U0001f310 Market", url=url),
            InlineKeyboardButton("\u26d4 Close", callback_data=f"close:{pool_id}"),
        ],
        [
            InlineKeyboardButton("\U0001f4a7 Add Liquidity", callback_data=f"liquidity:{pool_id}"),
            InlineKeyboardButton("\U0001f4ca Data", callback_data=f"data:{pool_id}"),
        ],
        [
            InlineKeyboardButton("\U0001f50d Check Answer", callback_data=f"checkanswer:{pool_id}"),
        ],
    ])


def show_more_keyboard(command: str, offset: int) -> InlineKeyboardMarkup:
    """Return an InlineKeyboardMarkup with a Show More button for pagination."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "\U0001f4cb Show More",
            callback_data=f"showmore:{command}:{offset}",
        )]
    ])


def refresh_keyboard(command: str) -> InlineKeyboardMarkup:
    """Return an InlineKeyboardMarkup with a single Refresh button."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Refresh", callback_data=f"refresh:{command}")]
    ])


def close_market_keyboard(pool_id: str) -> InlineKeyboardMarkup:
    """Return an InlineKeyboardMarkup with just a Close Market button (for retry)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u26d4 Close Market", callback_data=f"close:{pool_id}")]
    ])


# ---------------------------------------------------------------------------
# Telegram message sender helpers (with chunking for long messages)
# ---------------------------------------------------------------------------


async def send_chunked(
    target, text: str, parse_mode: str = "HTML",
    reply_markup=None, disable_web_page_preview: bool = True,
):
    """Send a potentially long message, splitting into chunks if needed.

    *target* can be an Update.message (for reply_text) or a tuple (bot, chat_id)
    for bot.send_message. The reply_markup is only attached to the LAST chunk.
    """
    max_len = 4096
    if len(text) <= max_len:
        if hasattr(target, "reply_text"):
            return await target.reply_text(
                text, parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        else:
            bot, chat_id = target
            return await bot.send_message(
                chat_id=chat_id, text=text, parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )

    # Split on double newlines
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for para in paragraphs:
        candidate = (current + "\n\n" + para) if current else para
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            current = para[:max_len]  # safety truncate single paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        rm = reply_markup if is_last else None
        if hasattr(target, "reply_text"):
            await target.reply_text(
                chunk, parse_mode=parse_mode,
                reply_markup=rm,
                disable_web_page_preview=disable_web_page_preview,
            )
        else:
            bot, chat_id = target
            await bot.send_message(
                chat_id=chat_id, text=chunk, parse_mode=parse_mode,
                reply_markup=rm,
                disable_web_page_preview=disable_web_page_preview,
            )


async def send_market_with_buttons(bot, chat_id: int, text: str, pool_id: str):
    """Send a market message with its per-market button row."""
    keyboard = market_buttons(pool_id)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )


async def send_multi_market_messages(
    target, header: str, market_entries: list[tuple[str, str]],
    footer_markup=None,
):
    """Send a header message, then one message per market with its own button row.

    *market_entries* is a list of (text, pool_id) tuples.
    *footer_markup* is an optional InlineKeyboardMarkup for a final button.
    *target* is either an Update.message or a tuple (bot, chat_id).
    """
    if hasattr(target, "reply_text"):
        bot = target._bot  # noqa — internal access
        chat_id = target.chat_id
    else:
        bot, chat_id = target

    # Send the header
    await bot.send_message(
        chat_id=chat_id,
        text=header,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    # Send each market as a separate message with its own button row
    for text, pool_id in market_entries:
        keyboard = market_buttons(pool_id)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

    # Send a final button if provided
    if footer_markup:
        await bot.send_message(
            chat_id=chat_id,
            text="\u2014",
            reply_markup=footer_markup,
        )


# ---------------------------------------------------------------------------
# Notification builders
# ---------------------------------------------------------------------------


def build_new_market_text(pool: dict) -> str:
    """Build the Telegram notification text for a new market (no URL in text)."""
    question = pool.get("question", "Unknown market")
    creator = get_creator_display(pool)
    end_date = format_end_date(pool.get("endDate"))

    options = pool.get("options", [])
    options_str = ""
    if options:
        names = [o.get("optionName", f"Option {o.get('choiceIndex', '?')}") for o in options]
        options_str = f"\n<b>Options:</b> {' | '.join(names)}"

    tags = pool.get("tags", [])
    tags_str = ""
    if tags:
        tags_str = f"\n<b>Tags:</b> {', '.join(tags)}"

    msg = (
        f"\U0001f327 <b>New Market on Rain Protocol</b>\n\n"
        f"<b>New Market:</b> {question}\n"
        f"<b>Creator:</b> {creator}\n"
        f"<b>Market ends:</b> {end_date}"
        f"{options_str}"
        f"{tags_str}"
    )
    return msg


def build_ended_alert_text(pool: dict) -> str:
    """Build the Telegram alert text for a market whose end time has passed (no URL)."""
    question = pool.get("question", "Unknown market")
    creator = get_creator_display(pool)
    ended_str = format_end_date(pool.get("endDate"))

    msg = (
        "\u23f0 <b>Market End Time Reached \u2014 Action Required</b>\n\n"
        f"<b>Market:</b> {question}\n"
        f"<b>Ended:</b> {ended_str}\n"
        f"<b>Creator:</b> {creator}\n\n"
        "Please close this market."
    )
    return msg


# ---------------------------------------------------------------------------
# Polling loop — runs as a background task inside the Application
# ---------------------------------------------------------------------------


async def poll_new_markets(app: Application):
    """Periodically poll the Rain API and notify the admin of new markets."""
    logger.info("Market polling loop started (interval=%ds)", POLL_INTERVAL_SECONDS)

    # On first run, seed the seen set with current markets so we don't spam
    seen = load_seen_markets()
    if not seen:
        logger.info("First run — seeding seen markets from current listings…")
        pools = await fetch_public_pools(limit=50, offset=1, sort_by="age")
        for p in pools:
            seen.add(p["_id"])
        pools_p2 = await fetch_public_pools(limit=50, offset=2, sort_by="age")
        for p in pools_p2:
            seen.add(p["_id"])
        save_seen_markets(seen)
        logger.info("Seeded %d existing markets.", len(seen))

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            admin_chat_id = get_admin_chat_id()
            if not admin_chat_id:
                logger.debug("No admin registered yet — skipping poll cycle.")
                continue

            pools = await fetch_public_pools(limit=20, offset=1, sort_by="age")
            new_pools = [p for p in pools if p["_id"] not in seen]

            if not new_pools:
                continue

            logger.info("Detected %d new market(s)!", len(new_pools))

            for pool in reversed(new_pools):
                pool_id = pool["_id"]
                detail = await fetch_pool_detail(pool_id)
                data = detail if detail else pool

                msg = build_new_market_text(data)
                try:
                    await send_market_with_buttons(
                        app.bot, admin_chat_id, msg, pool_id
                    )
                    logger.info("Notified admin about market: %s", pool_id)
                except Exception as send_err:
                    logger.error("Failed to send notification: %s", send_err)

                seen.add(pool_id)

            save_seen_markets(seen)

        except asyncio.CancelledError:
            logger.info("Polling loop cancelled — shutting down.")
            break
        except Exception as exc:
            logger.error("Error in polling loop: %s", exc, exc_info=True)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Telegram command handlers
#
# Every handler is wrapped in a try/except so that a failure in ANY command
# never propagates to the framework and never crashes the bot.
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — register the admin and send a welcome message."""
    try:
        chat_id = update.effective_chat.id
        set_admin_chat_id(chat_id)

        await update.message.reply_text(
            "\U0001f327 <b>Rainor \u2014 Rain Protocol Market Monitor</b>\n\n"
            "You are now registered as the admin. I will send you a notification "
            "every time a new prediction market is created on Rain Protocol.\n\n"
            "Use /help to see available commands.\n\n"
            f"<i>Your chat ID: {chat_id}</i>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("cmd_start failed: %s", exc, exc_info=True)
        try:
            await update.message.reply_text("Sorry, an error occurred. Please try again.")
        except Exception:
            pass


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help — show available commands."""
    try:
        await update.message.reply_text(
            "\U0001f327 <b>Rainor \u2014 Commands</b>\n\n"
            "/start \u2014 Register as admin and start receiving notifications\n"
            "/status \u2014 Show market counts by status and top markets by volume\n"
            "/latest \u2014 Show the 5 most recent active markets\n"
            "/closing \u2014 List markets closing in the next 48 hours\n"
            "/protocoldata \u2014 Monthly protocol statistics (volume, TVL, fees)\n"
            "/help \u2014 Show this help message\n\n"
            "<b>Per-market buttons:</b>\n"
            "\U0001f310 Market \u2014 Open market on rain.one\n"
            "\u26d4 Close \u2014 Close the market on-chain\n"
            "\U0001f4a7 Add Liquidity \u2014 Coming soon\n"
            "\U0001f4ca Data \u2014 Show detailed market info\n"
            "\U0001f50d Check Answer \u2014 AI-powered answer research",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("cmd_help failed: %s", exc, exc_info=True)
        try:
            await update.message.reply_text("Sorry, an error occurred. Please try again.")
        except Exception:
            pass


# ---- /status ----

async def _build_status_text() -> str:
    """Build the full /status response text (reusable for both command and refresh)."""
    STATUS_CONFIG = [
        ("Live",                 "Live"),
        ("Closed",               "Closed"),
        ("Pending_Finalization",  "Pending Finalization"),
        ("Waiting_for_Result",    "Waiting for Result"),
    ]

    status_counts: dict[str, int] = {}
    counted_total = 0

    for api_key, _display in STATUS_CONFIG:
        count = await fetch_pool_count(status=api_key)
        if count > 0:
            status_counts[api_key] = count
            counted_total += count

    grand_total = await fetch_pool_count(status=None)
    if grand_total == 0 and counted_total == 0:
        return "\u274c Could not fetch market data from Rain API. Please try again later."

    total = grand_total if grand_total > 0 else counted_total

    status_lines = ""
    for api_key, display_label in STATUS_CONFIG:
        if api_key in status_counts:
            count = status_counts[api_key]
            status_lines += (
                f"<b>{display_label}:</b> {count} market{'s' if count != 1 else ''}\n"
            )

    header = (
        "\U0001f4ca <b>Rain Protocol \u2014 Market Status</b>\n\n"
        + status_lines
        + f"\n<b>Total public markets:</b> {total}"
    )

    # ---- Top 20 markets by volume (active only) ----
    top_pools = await fetch_public_pools(limit=50, offset=1, sort_by="volume")
    if not top_pools:
        return header + "\n\n\U0001f4b0 <b>Volume by Market:</b>\n\nCould not fetch volume data."

    EXCLUDED_STATUSES = {"Closed"}
    top_pools_filtered = [
        p for p in top_pools
        if p.get("status", "") not in EXCLUDED_STATUSES
    ]

    top_pools_sorted = sorted(
        top_pools_filtered,
        key=lambda p: p.get("totalVolumeUSD", 0) or 0,
        reverse=True,
    )[:20]

    vol_entries = []
    for p in top_pools_sorted:
        raw_vol = p.get("totalVolumeUSD", 0)
        if not raw_vol:
            continue
        vol_usd = raw_vol / 1_000_000
        question = p.get("question", "Unknown market")
        status_val = p.get("status", "Unknown")
        vol_entries.append(
            f"<b>Market:</b> {question}\n"
            f"<b>Volume:</b> ${vol_usd:,.2f}\n"
            f"<b>Status:</b> {status_val}"
        )

    if not vol_entries:
        return header + "\n\n\U0001f4b0 <b>Volume by Market:</b>\n\nNo markets with recorded volume."

    vol_header = "\U0001f4b0 <b>Volume by Market \u2014 Active Markets (Top 20, highest first):</b>"
    return header + "\n\n" + vol_header + "\n\n" + "\n\n".join(vol_entries)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status — show market counts by status and top-20 markets by volume."""
    try:
        await update.message.reply_text("Fetching Rain Protocol market stats\u2026")
        text = await _build_status_text()
        keyboard = refresh_keyboard("status")
        await send_chunked(update.message, text, reply_markup=keyboard)
    except Exception as exc:
        logger.error("cmd_status failed: %s", exc, exc_info=True)
        try:
            await update.message.reply_text(
                "Sorry, an error occurred while fetching market status. Please try again."
            )
        except Exception:
            pass


# ---- /latest ----

async def _build_latest_entries(offset: int = 1) -> tuple[str, list[tuple[str, str]]]:
    """Build the /latest header and per-market entries for active markets only.

    *offset* is the 1-based page number (each page = 5 markets).
    Returns (header_text, [(market_text, pool_id), ...])
    """
    pools = await fetch_active_pools_paginated(limit=5, offset=offset, sort_by="age")
    if not pools:
        if offset == 1:
            return "Could not fetch active markets from Rain API.", []
        else:
            return "No more active markets to show.", []

    if offset == 1:
        header = "\U0001f327 <b>Latest Active Markets on Rain Protocol</b>"
    else:
        header = f"\U0001f327 <b>Active Markets (page {offset})</b>"

    start_num = (offset - 1) * 5 + 1
    entries = []
    for i, p in enumerate(pools, start_num):
        question = p.get("question", "Unknown")
        status = p.get("status", "?")
        end = format_end_date(p.get("endDate"))
        vol = p.get("totalVolume", 0)
        vol_usd = vol / 1_000_000 if isinstance(vol, (int, float)) else 0
        pool_id = p.get("_id", "")

        text = (
            f"<b>{i}.</b> {question}\n"
            f"   Status: {status} | Volume: ${vol_usd:,.2f}\n"
            f"   Ends: {end}"
        )
        entries.append((text, pool_id))

    return header, entries


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /latest — show the 5 most recent active markets with per-market buttons."""
    try:
        header, entries = await _build_latest_entries(offset=1)
        if not entries:
            await update.message.reply_text(header, parse_mode="HTML")
            return

        footer = show_more_keyboard("latest", offset=2)
        await send_multi_market_messages(
            update.message, header, entries, footer_markup=footer
        )
    except Exception as exc:
        logger.error("cmd_latest failed: %s", exc, exc_info=True)
        try:
            await update.message.reply_text(
                "Sorry, an error occurred while fetching latest markets. Please try again."
            )
        except Exception:
            pass


# ---- /closing ----

async def _build_closing_entries() -> tuple[str, list[tuple[str, str]]]:
    """Build the /closing header and per-market entries.

    Returns (header_text, [(market_text, pool_id), ...])
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=48)

    closing_markets = []
    for page in range(1, 5):
        pools = await fetch_public_pools(limit=50, offset=page, status="Live")
        if not pools:
            break
        for p in pools:
            end_raw = p.get("endDate")
            if not end_raw:
                continue
            try:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if now <= end_dt <= cutoff:
                closing_markets.append(p)
        if len(pools) < 50:
            break

    if not closing_markets:
        return "No markets closing in the next 48 hours.", []

    closing_markets.sort(
        key=lambda p: datetime.fromisoformat(p["endDate"].replace("Z", "+00:00"))
    )

    header = "<b>Markets closing in the next 48 hours:</b>"
    entries = []
    for i, p in enumerate(closing_markets, 1):
        question = p.get("question", "Unknown market")
        participants = p.get("participantCount", 0)
        pool_id = p.get("_id", "")

        text = (
            f"{i}. {question}\n"
            f"   \U0001f465 {participants} participants"
        )
        entries.append((text, pool_id))

    return header, entries


async def cmd_closing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /closing — list all active markets closing within the next 48 hours."""
    try:
        await update.message.reply_text("Fetching markets closing in the next 48 hours\u2026")
        header, entries = await _build_closing_entries()
        if not entries:
            await update.message.reply_text(header, parse_mode="HTML")
            return

        footer = refresh_keyboard("closing")
        await send_multi_market_messages(
            update.message, header, entries, footer_markup=footer
        )
    except Exception as exc:
        logger.error("cmd_closing failed: %s", exc, exc_info=True)
        try:
            await update.message.reply_text(
                "Sorry, an error occurred while fetching closing markets. Please try again."
            )
        except Exception:
            pass


# ---- /protocoldata ----

def _protocoldata_keyboard(active_range: str = "month") -> InlineKeyboardMarkup:
    """Return inline keyboard with time-range buttons for /protocoldata.

    The currently active range button gets a checkmark prefix.
    """
    ranges = [
        ("24h", "24 Hours"),
        ("7d", "7 Days"),
        ("30d", "30 Days"),
        ("all", "All Time"),
    ]
    buttons = []
    for key, label in ranges:
        prefix = "\u2705 " if key == active_range else ""
        buttons.append(
            InlineKeyboardButton(
                f"{prefix}{label}",
                callback_data=f"pdata:{key}",
            )
        )
    return InlineKeyboardMarkup([buttons[:2], buttons[2:]])


async def _build_protocol_data_text(time_range: str = "month") -> str:
    """Build the /protocoldata response text for the given time range.

    Supported ranges: '24h', '7d', '30d', 'all'.
    Default view is current calendar month.
    Uses DefiLlama API for TVL, Volume, and Fees (authoritative on-chain data).
    Uses Rain Protocol API for market counts.
    """
    now = datetime.now(timezone.utc)

    # Determine the cutoff datetime based on the requested range
    if time_range == "24h":
        cutoff = now - timedelta(hours=24)
        period_label = "Last 24 Hours"
        period_range = f"{cutoff.strftime('%b %d %H:%M')} \u2013 {now.strftime('%b %d %H:%M UTC')}"
    elif time_range == "7d":
        cutoff = now - timedelta(days=7)
        period_label = "Last 7 Days"
        period_range = f"{cutoff.strftime('%b %d')} \u2013 {now.strftime('%b %d, %Y')}"
    elif time_range == "30d":
        cutoff = now - timedelta(days=30)
        period_label = "Last 30 Days"
        period_range = f"{cutoff.strftime('%b %d')} \u2013 {now.strftime('%b %d, %Y')}"
    elif time_range == "all":
        cutoff = None
        period_label = "All Time"
        period_range = f"Since inception \u2013 {now.strftime('%b %d, %Y')}"
    else:  # "month" — current calendar month (default)
        cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        period_label = now.strftime("%B %Y")
        period_range = f"{cutoff.strftime('%b %d')} \u2013 {now.strftime('%b %d, %Y')}"

    # --- Collect markets in the time range (from Rain API) ---
    matched_markets: list[dict] = []
    page = 1

    while page <= 20:
        pools = await fetch_public_pools(limit=100, offset=page, sort_by="age")
        if not pools:
            break
        found_older = False
        for p in pools:
            created_raw = p.get("createdAt", "")
            try:
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            except Exception:
                continue
            if cutoff is not None and created_dt < cutoff:
                found_older = True
                break
            matched_markets.append(p)
        if found_older or len(pools) < 100:
            break
        page += 1

    # --- Total markets on protocol (always all-time, from Rain API) ---
    total_count = await fetch_all_pools_count()
    if total_count == 0:
        total_count = await fetch_pool_count()

    # --- Total registered users (all-time, from Rain API) ---
    total_users = await fetch_total_users()

    # --- Fetch TVL, Volume, and Fees from DefiLlama API ---
    dl_tvl = 0.0
    dl_volume = 0.0
    dl_fees = 0.0

    try:
        async with httpx.AsyncClient(timeout=15.0) as dl_client:
            # TVL: current on-chain snapshot (always live, regardless of time range)
            tvl_resp = await dl_client.get("https://api.llama.fi/tvl/rain")
            if tvl_resp.status_code == 200:
                dl_tvl = float(tvl_resp.json())

            # Volume: from DefiLlama DEX adapter
            vol_resp = await dl_client.get("https://api.llama.fi/summary/dexs/rain")
            if vol_resp.status_code == 200:
                vol_data = vol_resp.json()
                if time_range == "24h":
                    dl_volume = float(vol_data.get("total24h") or 0)
                elif time_range == "7d":
                    dl_volume = float(vol_data.get("total7d") or 0)
                elif time_range == "30d":
                    dl_volume = float(vol_data.get("total30d") or 0)
                elif time_range == "all":
                    dl_volume = float(vol_data.get("totalAllTime") or 0)
                else:  # "month"
                    dl_volume = float(vol_data.get("total30d") or 0)

            # Fees: from DefiLlama fees adapter
            fees_resp = await dl_client.get("https://api.llama.fi/summary/fees/rain")
            if fees_resp.status_code == 200:
                fees_data = fees_resp.json()
                if time_range == "24h":
                    dl_fees = float(fees_data.get("total24h") or 0)
                elif time_range == "7d":
                    dl_fees = float(fees_data.get("total7d") or 0)
                elif time_range == "30d":
                    dl_fees = float(fees_data.get("total30d") or 0)
                elif time_range == "all":
                    dl_fees = float(fees_data.get("totalAllTime") or 0)
                else:  # "month"
                    dl_fees = float(fees_data.get("total30d") or 0)
    except Exception as exc:
        logger.warning("DefiLlama API error in /protocoldata: %s", exc)

    # Fees fallback: estimate as 2.5% of volume if DefiLlama fees returned 0
    fee_usd = dl_fees if dl_fees > 0 else dl_volume * 0.025

    # Volume label note for "month" range
    vol_note = " (rolling 30d)" if time_range == "month" else ""

    # Users line: always show total users (it's always all-time)
    users_line = ""
    if total_users > 0:
        users_line = f"\U0001f464 <b>Total users (all time):</b> {total_users:,}\n"

    # Build the message
    text = (
        f"\U0001f4ca <b>Rain Protocol \u2014 {period_label}</b>\n\n"
        f"\U0001f4c5 <b>Period:</b> {period_range}\n\n"
        f"\U0001f195 <b>Markets opened:</b> {len(matched_markets)}\n"
        f"\U0001f4b0 <b>Volume{vol_note}:</b> ${dl_volume:,.0f}\n"
        f"\U0001f512 <b>TVL (current):</b> ${dl_tvl:,.0f}\n"
        f"\U0001f525 <b>Fees{vol_note} (\u2192 Rain burn):</b> ${fee_usd:,.0f}\n\n"
        f"{users_line}"
        f"\U0001f30d <b>Total markets (all time):</b> {total_count:,}\n\n"
        f"<i>TVL \u2022 Volume \u2022 Fees: DefiLlama (on-chain)\n"
        f"Markets \u2022 Users: Rain Protocol API\n"
        f"Updated {now.strftime('%H:%M UTC')}</i>"
    )
    return text


async def cmd_protocoldata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /protocoldata \u2014 show protocol statistics (default: current month)."""
    try:
        await update.message.reply_text("\u23f3 Fetching protocol statistics\u2026")
        text = await _build_protocol_data_text("month")
        keyboard = _protocoldata_keyboard("month")
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.error("cmd_protocoldata failed: %s", exc, exc_info=True)
        try:
            await update.message.reply_text(
                "Sorry, an error occurred while fetching protocol data. Please try again."
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Callback query handler \u2014 processes all inline button presses
# ---------------------------------------------------------------------------


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all inline keyboard button callbacks."""
    query = update.callback_query
    try:
        await query.answer()  # Acknowledge the button press immediately
    except Exception:
        pass

    data = query.data or ""

    try:
        # ---- Refresh buttons ----
        if data.startswith("refresh:"):
            command = data.split(":", 1)[1]

            if command == "status":
                await query.edit_message_text(
                    text="\u23f3 Refreshing\u2026", parse_mode="HTML"
                )
                text = await _build_status_text()
                keyboard = refresh_keyboard(command)
                if len(text) <= 4096:
                    await query.edit_message_text(
                        text=text, parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                else:
                    try:
                        await query.message.delete()
                    except Exception:
                        pass
                    await send_chunked(
                        (context.bot, query.message.chat_id),
                        text, reply_markup=keyboard,
                    )

            elif command == "closing":
                try:
                    await query.message.delete()
                except Exception:
                    pass
                header, entries = await _build_closing_entries()
                if not entries:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=header, parse_mode="HTML",
                    )
                else:
                    footer = refresh_keyboard("closing")
                    await send_multi_market_messages(
                        (context.bot, query.message.chat_id),
                        header, entries, footer_markup=footer,
                    )

            else:
                await query.edit_message_text(text="Unknown command.")

        # ---- Protocol Data time-range buttons ----
        elif data.startswith("pdata:"):
            time_range = data.split(":", 1)[1]
            if time_range not in ("24h", "7d", "30d", "all"):
                time_range = "month"
            await query.edit_message_text(
                text="\u23f3 Fetching protocol statistics\u2026", parse_mode="HTML"
            )
            text = await _build_protocol_data_text(time_range)
            keyboard = _protocoldata_keyboard(time_range)
            if len(text) <= 4096:
                await query.edit_message_text(
                    text=text, parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            else:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await send_chunked(
                    (context.bot, query.message.chat_id),
                    text, reply_markup=keyboard,
                )

        # ---- Show More button (pagination for /latest) ----
        elif data.startswith("showmore:"):
            parts = data.split(":")
            command = parts[1] if len(parts) > 1 else ""
            offset = int(parts[2]) if len(parts) > 2 else 2

            if command == "latest":
                try:
                    await query.message.delete()
                except Exception:
                    pass

                header, entries = await _build_latest_entries(offset=offset)
                if not entries:
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text="No more active markets to show.",
                        parse_mode="HTML",
                    )
                else:
                    footer = show_more_keyboard("latest", offset=offset + 1)
                    await send_multi_market_messages(
                        (context.bot, query.message.chat_id),
                        header, entries, footer_markup=footer,
                    )

        # ---- Close Market button ----
        elif data.startswith("close:"):
            pool_id = data.split(":", 1)[1]

            # Remove buttons from the original message to prevent double-clicks
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"\u23f3 Closing market <code>{pool_id}</code>\u2026 Please wait.",
                parse_mode="HTML",
            )

            success, result_msg = await close_market_on_chain(pool_id)

            if success:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"\u2705 <b>Market closed successfully</b>\n\n{result_msg}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            else:
                # Re-add the Close button so the admin can retry
                keyboard = close_market_keyboard(pool_id)
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"\u274c <b>Failed to close market</b>\n\n{result_msg}",
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )

        # ---- Add Liquidity button (coming soon) ----
        elif data.startswith("liquidity:"):
            await query.answer("\U0001f6a7 Coming soon!", show_alert=True)

        # ---- Data button ----
        elif data.startswith("data:"):
            pool_id = data.split(":", 1)[1]

            # Show loading indicator
            await query.answer("\U0001f4ca Fetching market data\u2026")

            detail = await fetch_pool_detail(pool_id)
            if not detail:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="\u274c Could not fetch market data. Please try again.",
                )
                return

            question = detail.get("question", "Unknown market")
            creator = get_creator_display(detail)
            status_val = detail.get("status", "Unknown")

            # Participants — use participantCount from detail, fallback to list endpoint
            participants = detail.get("participantCount", 0)
            if not participants:
                # The detail endpoint may not have participantCount;
                # try to get it from the list endpoint by searching
                try:
                    client = await get_http_client()
                    resp = await client.get(
                        f"{RAIN_API_BASE}/pools/public-pools",
                        params={"limit": 1, "offset": 1, "sortBy": "age"},
                    )
                    # This won't help for a specific pool — use 0 as fallback
                except Exception:
                    pass

            # Dates
            opened = format_created_date(detail.get("createdAt"))

            # Volume
            # totalLiquidity = initial liquidity added at market creation
            initial_liq_raw = detail.get("totalLiquidity", 0) or 0
            initial_vol = initial_liq_raw / 1_000_000 if initial_liq_raw else 0

            # totalVolume or totalVolumeUSD = current total volume
            current_vol_raw = detail.get("totalVolumeUSD", 0) or detail.get("totalVolume", 0) or 0
            current_vol = current_vol_raw / 1_000_000 if current_vol_raw else 0

            data_text = (
                f"\U0001f4ca <b>Market Data</b>\n\n"
                f"<b>Market:</b> {question}\n"
                f"<b>Opened:</b> {opened}\n"
                f"<b>Creator:</b> {creator}\n"
                f"<b>Unique participants:</b> {participants}\n"
                f"<b>Initial liquidity:</b> ${initial_vol:,.2f}\n"
                f"<b>Current volume:</b> ${current_vol:,.2f}\n"
                f"<b>Status:</b> {status_val}"
            )

            # Send as a new message with the market buttons
            keyboard = market_buttons(pool_id)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=data_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )

        # ---- Check Answer button ----
        elif data.startswith("checkanswer:"):
            pool_id = data.split(":", 1)[1]

            # Show loading alert
            await query.answer("\U0001f50d Researching answer\u2026 This may take a moment.")

            # Send a "thinking" message
            thinking_msg = await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="\U0001f50d <b>Researching answer\u2026</b> Please wait.",
                parse_mode="HTML",
            )

            # Fetch market details for the question and end date
            detail = await fetch_pool_detail(pool_id)
            if not detail:
                await thinking_msg.edit_text(
                    "\u274c Could not fetch market data. Please try again."
                )
                return

            question = detail.get("question", "Unknown market")
            end_date = format_end_date(detail.get("endDate"))

            # Run the AI check in a thread to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            answer_text = await loop.run_in_executor(
                None, check_answer_with_ai, question, end_date
            )

            # Edit the thinking message with the result
            try:
                await thinking_msg.edit_text(
                    answer_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                # If edit fails (e.g., message too long), send as new message
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=answer_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

        else:
            logger.warning("Unknown callback data: %s", data)

    except Exception as exc:
        logger.error("handle_callback failed for data=%s: %s", data, exc, exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="Sorry, an error occurred processing that button. Please try again.",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Background loop — market end-time alerts
# ---------------------------------------------------------------------------


async def _collect_expired_market_ids(now: datetime) -> set:
    """Fetch all non-Closed markets with endDate in the past and return their IDs.
    Used for seeding the alerted set on first run — NO notifications are sent."""
    expired_ids: set = set()

    for status in ACTIVE_STATUSES:
        page = 1
        while True:
            pools = await fetch_public_pools(
                limit=100, offset=page, sort_by="age", status=status
            )
            if not pools:
                break
            for p in pools:
                pool_id = p.get("_id", "")
                if not pool_id:
                    continue
                end_raw = p.get("endDate")
                if not end_raw:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                except Exception:
                    continue
                if end_dt <= now:
                    expired_ids.add(pool_id)
            if len(pools) < 100:
                break
            page += 1

    return expired_ids


async def poll_ended_markets(app: Application):
    """Every 5 minutes, check for non-Closed markets whose endDate has passed
    and alert the admin if not already notified.

    On the very first run (empty alerted_ended_markets.json), all currently-expired
    markets are seeded silently so the admin is NOT flooded with historical alerts.
    Only markets that expire AFTER the bot starts will trigger notifications.
    """
    logger.info("Market end-time alert loop started (interval=%ds)", END_POLL_INTERVAL_SECONDS)

    alerted = load_alerted_ended()

    # -----------------------------------------------------------------------
    # SEED PHASE
    # -----------------------------------------------------------------------
    if not alerted:
        logger.info(
            "First run detected — seeding all currently-expired markets silently…"
        )
        try:
            now_seed = datetime.now(timezone.utc)
            seed_ids = await _collect_expired_market_ids(now_seed)
            if seed_ids:
                alerted.update(seed_ids)
                save_alerted_ended(alerted)
                logger.info(
                    "Seeded %d already-expired market(s) — no alerts sent for these.",
                    len(seed_ids),
                )
            else:
                logger.info("No currently-expired markets found during seeding.")
        except Exception as seed_err:
            logger.error("Error during end-alert seeding: %s", seed_err, exc_info=True)

    # -----------------------------------------------------------------------
    # MAIN LOOP
    # -----------------------------------------------------------------------
    while True:
        try:
            await asyncio.sleep(END_POLL_INTERVAL_SECONDS)

            admin_chat_id = get_admin_chat_id()
            if not admin_chat_id:
                logger.debug("No admin registered — skipping end-time poll cycle.")
                continue

            now = datetime.now(timezone.utc)
            newly_alerted = []

            for status in ACTIVE_STATUSES:
                page = 1
                while True:
                    pools = await fetch_public_pools(
                        limit=100, offset=page, sort_by="age", status=status
                    )
                    if not pools:
                        break

                    for p in pools:
                        pool_id = p.get("_id", "")
                        if not pool_id or pool_id in alerted:
                            continue

                        end_raw = p.get("endDate")
                        if not end_raw:
                            continue

                        try:
                            end_dt = datetime.fromisoformat(
                                end_raw.replace("Z", "+00:00")
                            )
                        except Exception:
                            continue

                        if end_dt > now:
                            continue

                        # Build and send the alert with per-market buttons
                        msg = build_ended_alert_text(p)
                        keyboard = market_buttons(pool_id)

                        try:
                            await app.bot.send_message(
                                chat_id=admin_chat_id,
                                text=msg,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                reply_markup=keyboard,
                            )
                            logger.info(
                                "Sent end-time alert for market: %s", pool_id
                            )
                            newly_alerted.append(pool_id)
                        except Exception as send_err:
                            logger.error(
                                "Failed to send end-time alert for %s: %s",
                                pool_id, send_err,
                            )

                    if len(pools) < 100:
                        break
                    page += 1

            if newly_alerted:
                alerted.update(newly_alerted)
                save_alerted_ended(alerted)
                logger.info(
                    "End-time alerts sent for %d market(s).", len(newly_alerted)
                )

        except asyncio.CancelledError:
            logger.info("End-time alert loop cancelled — shutting down.")
            break
        except Exception as exc:
            logger.error("Error in end-time alert loop: %s", exc, exc_info=True)
            await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Global error handler — catches ANY unhandled exception from any handler
# so the bot NEVER crashes from a command failure.
# ---------------------------------------------------------------------------


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log the error and send a friendly message to the user."""
    logger.error(
        "Unhandled exception in handler: %s",
        context.error,
        exc_info=context.error,
    )
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Sorry, an unexpected error occurred. Please try again later."
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Application lifecycle hooks
# ---------------------------------------------------------------------------


_poll_task = None
_end_alert_task = None


async def post_init(app: Application):
    """Called after the Application is initialized — set bot commands."""
    await app.bot.set_my_commands([
        BotCommand("start", "Register as admin and start notifications"),
        BotCommand("status", "Show market counts by status and top volume"),
        BotCommand("latest", "Show 5 most recent active markets"),
        BotCommand("closing", "List markets closing in the next 48 hours"),
        BotCommand("protocoldata", "Monthly protocol statistics"),
        BotCommand("help", "Show available commands"),
    ])
    logger.info("Bot commands registered.")


async def post_shutdown(app: Application):
    """Called on shutdown — cancel background tasks and close HTTP client."""
    global _poll_task, _end_alert_task
    for task in (_poll_task, _end_alert_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await close_http_client()
    logger.info("Shutting down background tasks…")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_bot():
    """Build, configure, and run the bot with the background polling loop."""
    global _poll_task, _end_alert_task
    logger.info("Starting Rainor bot\u2026")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("closing", cmd_closing))
    app.add_handler(CommandHandler("protocoldata", cmd_protocoldata))

    # Register the callback query handler for ALL inline buttons
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Register the global error handler
    app.add_error_handler(global_error_handler)

    logger.info("Handlers registered. Starting polling\u2026")

    async with app:
        await app.start()

        _poll_task = asyncio.create_task(poll_new_markets(app), name="poll_new_markets")
        logger.info("Background market polling task launched.")

        _end_alert_task = asyncio.create_task(
            poll_ended_markets(app), name="poll_ended_markets"
        )
        logger.info("Background market end-time alert task launched.")

        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram update polling started. Bot is live!")

        # Keep running until interrupted
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()

        logger.info("Shutdown signal received.")
        await app.updater.stop()
        await app.stop()


def main():
    """Entry point with auto-restart on unexpected crashes."""
    while True:
        try:
            asyncio.run(run_bot())
            break  # Clean exit (e.g. SIGTERM) — don't restart
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — exiting.")
            break
        except Exception as exc:
            logger.critical(
                "Bot crashed unexpectedly: %s — restarting in 5 seconds…",
                exc,
                exc_info=True,
            )
            import time
            time.sleep(5)


if __name__ == "__main__":
    main()
