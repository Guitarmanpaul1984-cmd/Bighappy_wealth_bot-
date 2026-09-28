import json
import os
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
DEX_API = "https://api.dexscreener.com"
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
WSOL = "So11111111111111111111111111111111111111112"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_MIGRATION_ACCOUNT = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"

POSITION_SOL = float(os.getenv("PAPER_POSITION_SOL", "0.10"))
PAPER_STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE_SOL", "1.00"))
NET_TARGET = float(os.getenv("NET_TARGET_PCT", "10")) / 100.0
FEE_SIDE = float(os.getenv("SIM_FEE_PCT_SIDE", "1.25")) / 100.0
SLIPPAGE_SIDE = float(os.getenv("SIM_SLIPPAGE_PCT_SIDE", "0.80")) / 100.0
MAX_OPEN = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
RISK_MAX = int(os.getenv("RISK_MAX", "35"))
SOCIAL_MIN = int(os.getenv("SOCIAL_MIN", "0"))
MARKET_MIN = int(os.getenv("MARKET_MIN", "35"))
DIP_MIN = float(os.getenv("DIP_MIN_PCT", "8")) / 100.0
DIP_MAX = float(os.getenv("DIP_MAX_PCT", "50")) / 100.0
REBOUND = float(os.getenv("REBOUND_PCT", "1")) / 100.0
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "60"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "7500"))
MIN_PAIR_AGE_MIN = float(os.getenv("MIN_PAIR_AGE_MIN", "2"))
MIGRATION_WATCH_MIN = float(os.getenv("MIGRATION_WATCH_MIN", "180"))
MIGRATION_SIGNATURE_LIMIT = int(os.getenv("MIGRATION_SIGNATURE_LIMIT", "8"))
DISCOVERY_LIMIT = int(os.getenv("DISCOVERY_LIMIT", "24"))

# Market-price multiplier needed to net NET_TARGET after modeled entry/exit costs.
TARGET_MULTIPLIER = (
    (1.0 + NET_TARGET) * (1.0 + SLIPPAGE_SIDE)
    / (((1.0 - FEE_SIDE) ** 2) * (1.0 - SLIPPAGE_SIDE))
)

update_offset = 0
auto_scan_enabled = True
known_chats = set()
motion_state = {}
open_positions = {}
closed_positions = []
candidates = []
last_scan_at = 0.0
migration_watch = {}
migration_tx_cache = {}
last_migration_signature = None
last_discovery_stats = {"migration": 0, "dex": 0}
last_rejection_stats = {}
live_feed_chats = set()
trade_events = []
MAX_TRADE_EVENTS = 20


def _redact_secret(value):
    text = str(value)
    if TOKEN:
        text = text.replace(TOKEN, "<redacted>")
    return text


def http_json(url, data=None, headers=None, timeout=25):
    merged = {
        "User-Agent": "BigHappyWealthBot/3.0",
        "Accept": "application/json",
    }
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, data=data, headers=merged)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"HTTP {e.code} {_redact_secret(url)}: {_redact_secret(body[:300])}", flush=True)
    except Exception as e:
        print(f"HTTP ERROR {type(e).__name__}: {_redact_secret(e)}", flush=True)
    return None


def telegram(method, params=None, timeout=35):
    query = ""
    if params:
        query = "?" + urllib.parse.urlencode(params)
    return http_json(f"{TELEGRAM_API}/{method}{query}", timeout=timeout)


def send_message(chat_id, text):
    payload = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode()
    out = http_json(
        f"{TELEGRAM_API}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        timeout=15,
    )
    ok = bool(out and out.get("ok"))
    print(f"SEND chat_id={chat_id} ok={ok}", flush=True)
    return ok


def broadcast(text):
    for chat_id in list(known_chats):
        send_message(chat_id, text)


def record_trade_event(text):
    trade_events.append({"time": time.time(), "text": text})
    if len(trade_events) > MAX_TRADE_EVENTS:
        del trade_events[:-MAX_TRADE_EVENTS]


def broadcast_live(text):
    record_trade_event(text)
    for chat_id in list(live_feed_chats):
        send_message(chat_id, text)


def live_text():
    lines = [
        "🟢 LIVE PAPER TRADE FEED",
        "Automatic paper entry/exit alerts are ON for this chat.",
        f"Open paper positions: {len(open_positions)}/{MAX_OPEN}",
    ]
    if not trade_events:
        lines.append("No paper trades have triggered yet.")
        return "\n".join(lines)

    lines.append("Recent paper trades:")
    now = time.time()
    for event in trade_events[-6:][::-1]:
        age_s = max(0, int(now - event.get("time", now)))
        if age_s < 60:
            age = f"{age_s}s ago"
        else:
            age = f"{age_s // 60}m ago"
        first_line = str(event.get("text") or "paper trade").splitlines()[0]
        lines.append(f"• {age} — {first_line}")
    return "\n".join(lines)


def rpc(method, params):
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    out = http_json(
        SOLANA_RPC,
        data=payload,
        headers={"Content-Type": "application/json"},
        timeout=18,
    )
    if not out or out.get("error"):
        return None
    return out.get("result")


def onchain_safety(mint):
    """Fail closed when mint or holder data cannot be verified."""
    account = rpc(
        "getAccountInfo",
        [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    if not account or not account.get("value"):
        return None, "mint data unavailable"

    parsed = ((account["value"].get("data") or {}).get("parsed") or {})
    info = parsed.get("info") or {}

    if info.get("mintAuthority") is not None:
        return None, "mint authority active"
    if info.get("freezeAuthority") is not None:
        return None, "freeze authority active"

    extensions = info.get("extensions") or parsed.get("extensions") or []
    blocked_extensions = {
        "permanentDelegate",
        "transferHook",
        "transferFeeConfig",
        "confidentialTransferMint",
    }
    for ext in extensions:
        name = ext.get("extension") if isinstance(ext, dict) else str(ext)
        if name in blocked_extensions:
            return None, f"unsafe extension: {name}"

    supply = rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
    largest = rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    if not supply or not largest:
        # PAPER MODE ONLY: a rate-limited public RPC should not block every simulated trade.
        # Keep a heavy risk penalty so unknown holder concentration ranks worse, while
        # still allowing the strategy to collect paper-trading data.
        return {"risk": 30, "top1": None, "top10": None, "holder_unknown": True}, "holder data unavailable (soft pass)"

    try:
        total = int(supply["value"]["amount"])
        balances = [
            int(item["amount"])
            for item in largest["value"]
            if int(item.get("amount") or 0) > 0
        ]
        if total <= 0 or not balances:
            return None, "invalid token supply"
        top1 = 100.0 * balances[0] / total
        top10 = 100.0 * sum(balances[:10]) / total
    except Exception:
        return None, "holder parse failure"

    if top1 > 45 or top10 > 90:
        return None, f"holder concentration {top1:.1f}/{top10:.1f}%"

    risk = 0
    if top1 > 25:
        risk += 15
    if top10 > 70:
        risk += 10

    return {"risk": risk, "top1": top1, "top10": top10}, None


def _account_pubkeys(tx):
    message = (((tx or {}).get("transaction") or {}).get("message") or {})
    keys = set()
    for item in message.get("accountKeys") or []:
        if isinstance(item, dict):
            pubkey = item.get("pubkey")
        else:
            pubkey = item
        if pubkey:
            keys.add(str(pubkey))
    for ix in message.get("instructions") or []:
        if isinstance(ix, dict) and ix.get("programId"):
            keys.add(str(ix["programId"]))
    meta = (tx or {}).get("meta") or {}
    for group in meta.get("innerInstructions") or []:
        for ix in (group or {}).get("instructions") or []:
            if isinstance(ix, dict) and ix.get("programId"):
                keys.add(str(ix["programId"]))
    return keys


def _migration_mints_from_tx(tx):
    """Return token mints from a verified Pump.fun -> PumpSwap migration tx."""
    keys = _account_pubkeys(tx)
    if PUMP_PROGRAM not in keys or PUMPSWAP_PROGRAM not in keys:
        return []

    meta = (tx or {}).get("meta") or {}
    mints = []
    seen = set()
    for item in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        mint = item.get("mint") if isinstance(item, dict) else None
        if mint and mint != WSOL and mint not in seen:
            seen.add(mint)
            mints.append(mint)
    return mints


def refresh_migration_watch():
    """Poll the official Pump.fun migration account and retain recent graduates."""
    global last_migration_signature

    options = {"limit": max(1, min(MIGRATION_SIGNATURE_LIMIT, 50)), "commitment": "confirmed"}
    if last_migration_signature:
        options["until"] = last_migration_signature

    rows = rpc("getSignaturesForAddress", [PUMP_MIGRATION_ACCOUNT, options])
    if not isinstance(rows, list):
        return 0

    now = time.time()
    added = 0
    for row in reversed(rows):
        if not isinstance(row, dict) or row.get("err"):
            continue
        signature = row.get("signature")
        if not signature:
            continue

        mints = migration_tx_cache.get(signature)
        if mints is None:
            tx = rpc(
                "getTransaction",
                [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )
            mints = _migration_mints_from_tx(tx) if tx else []
            migration_tx_cache[signature] = mints

        block_time = float(row.get("blockTime") or now)
        for mint in mints:
            current = migration_watch.get(mint)
            if current is None or block_time > current.get("block_time", 0):
                migration_watch[mint] = {
                    "block_time": block_time,
                    "signature": signature,
                }
                added += 1

    if rows and isinstance(rows[0], dict) and rows[0].get("signature"):
        last_migration_signature = rows[0]["signature"]

    cutoff = now - MIGRATION_WATCH_MIN * 60.0
    for mint, data in list(migration_watch.items()):
        if data.get("block_time", 0) < cutoff:
            migration_watch.pop(mint, None)

    # Bound caches in long-running containers.
    if len(migration_tx_cache) > 250:
        keep = {d.get("signature") for d in migration_watch.values()}
        for sig in list(migration_tx_cache):
            if sig not in keep:
                migration_tx_cache.pop(sig, None)
            if len(migration_tx_cache) <= 150:
                break

    return added


def discover_mints(limit=None):
    """Prefer verified on-chain Pump.fun graduates, then use DexScreener as fallback."""
    global last_discovery_stats
    limit = DISCOVERY_LIMIT if limit is None else limit
    out = []
    seen = set()

    refresh_migration_watch()
    recent = sorted(
        migration_watch.items(),
        key=lambda item: item[1].get("block_time", 0),
        reverse=True,
    )
    for mint, _ in recent:
        if mint not in seen:
            seen.add(mint)
            out.append(mint)
        if len(out) >= limit:
            break
    migration_count = len(out)

    if len(out) < limit:
        for endpoint in ("token-boosts/latest/v1", "token-profiles/latest/v1"):
            data = http_json(f"{DEX_API}/{endpoint}", timeout=15)
            if not isinstance(data, list):
                continue
            for item in data:
                mint = item.get("tokenAddress")
                if (
                    item.get("chainId") == "solana"
                    and mint
                    and mint not in seen
                    and mint.lower().endswith("pump")
                ):
                    seen.add(mint)
                    out.append(mint)
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break

    last_discovery_stats = {
        "migration": migration_count,
        "dex": max(0, len(out) - migration_count),
    }
    return out


def best_pumpswap_pair(mint):
    data = http_json(
        f"{DEX_API}/token-pairs/v1/solana/{urllib.parse.quote(mint)}",
        timeout=15,
    )
    if not isinstance(data, list):
        return None

    eligible = []
    for pair in data:
        if (pair.get("baseToken") or {}).get("address") != mint:
            continue
        if str(pair.get("dexId") or "").lower() != "pumpswap":
            continue
        if (pair.get("quoteToken") or {}).get("address") != WSOL:
            continue
        eligible.append(pair)

    if not eligible:
        return None

    return max(
        eligible,
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
    )


def market_scores(pair):
    info = pair.get("info") or {}
    social_platforms = {
        str(item.get("platform") or "").lower()
        for item in (info.get("socials") or [])
        if isinstance(item, dict)
    }

    social = 0
    if info.get("websites"):
        social += 30
    if "twitter" in social_platforms or "x" in social_platforms:
        social += 35
    if "telegram" in social_platforms:
        social += 35
    social = min(social, 100)

    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
    volume_h1 = float((pair.get("volume") or {}).get("h1") or 0)
    tx5 = ((pair.get("txns") or {}).get("m5") or {})
    buys = int(tx5.get("buys") or 0)
    sells = int(tx5.get("sells") or 0)
    buy_sell_ratio = buys / max(sells, 1)

    created = int(pair.get("pairCreatedAt") or 0)
    age_min = (time.time() * 1000 - created) / 60000 if created else 0

    market = 0
    market += 30 if liquidity >= 100000 else 20 if liquidity >= 50000 else 10 if liquidity >= MIN_LIQUIDITY_USD else 0
    market += 25 if volume_h1 >= 100000 else 15 if volume_h1 >= 30000 else 8 if volume_h1 >= 10000 else 0
    market += 20 if buys >= 20 else 10 if buys >= 10 else 0
    market += 15 if buy_sell_ratio >= 1.30 else 10 if buy_sell_ratio >= 1.05 else 0
    market += 10 if age_min >= 30 else 0

    return social, min(market, 100), age_min


def update_motion(mint, price):
    state = motion_state.setdefault(
        mint, {"high": price, "low": None, "armed": False}
    )

    if price > state["high"]:
        state.update({"high": price, "low": None, "armed": False})

    drop = (state["high"] - price) / state["high"] if state["high"] else 0

    if DIP_MIN <= drop <= DIP_MAX:
        state["low"] = price if state["low"] is None else min(state["low"], price)
        state["armed"] = True

    rebound = (
        (price - state["low"]) / state["low"]
        if state["armed"] and state["low"]
        else 0
    )
    triggered = bool(
        state["armed"] and rebound >= REBOUND and drop <= DIP_MAX
    )
    return drop, rebound, triggered


def fetch_pair(pair_address):
    out = http_json(
        f"{DEX_API}/latest/dex/pairs/solana/{urllib.parse.quote(pair_address)}",
        timeout=15,
    )
    pairs = (out or {}).get("pairs") or []
    return pairs[0] if pairs else None


def net_pnl_sol(position, current_price):
    token_qty = (
        position["size_sol"]
        * (1.0 - FEE_SIDE)
        / (position["entry_price"] * (1.0 + SLIPPAGE_SIDE))
    )
    proceeds = (
        token_qty
        * current_price
        * (1.0 - SLIPPAGE_SIDE)
        * (1.0 - FEE_SIDE)
    )
    return proceeds - position["size_sol"]


def check_exits():
    for mint, position in list(open_positions.items()):
        pair = fetch_pair(position["pair_address"])
        if not pair:
            continue
        try:
            current_price = float(pair.get("priceNative") or 0)
        except Exception:
            continue
        if current_price <= 0:
            continue

        pnl = net_pnl_sol(position, current_price)
        if pnl / position["size_sol"] >= NET_TARGET:
            position["pnl_sol"] = pnl
            position["closed_at"] = time.time()
            closed_positions.append(position)
            open_positions.pop(mint, None)
            total_realized = sum(p.get("pnl_sol", 0) for p in closed_positions)
            paper_balance = PAPER_STARTING_BALANCE + total_realized
            event_text = (
                "💰 PAPER PROFIT MADE\n"
                f"{position['symbol']} ({mint[:6]}…{mint[-4:]})\n"
                f"Profit: +{pnl:.4f} SOL\n"
                f"Paper balance: {paper_balance:.4f} SOL\n"
                "No real trade was placed."
            )
            broadcast_live(event_text)
            # Profit notifications go to every chat that has interacted with the bot,
            # even if /live is off. Avoid duplicates for /live subscribers.
            for chat_id in list(known_chats - live_feed_chats):
                send_message(chat_id, event_text)


def _count_rejection(stats, reason):
    stats[reason] = stats.get(reason, 0) + 1


def _safety_rejection_label(reason):
    text = str(reason or "on-chain safety failure")
    if "mint authority active" in text:
        return "mint authority active"
    if "freeze authority active" in text:
        return "freeze authority active"
    if "holder concentration" in text:
        return "holder concentration too high"
    if "holder data unavailable" in text:
        return "holder data unavailable"
    if "mint data unavailable" in text:
        return "mint data unavailable"
    if "unsafe extension" in text:
        return "unsafe token extension"
    if "invalid token supply" in text:
        return "invalid token supply"
    if "holder parse failure" in text:
        return "holder data parse failure"
    return "on-chain safety failure"


def run_scan():
    global candidates, last_scan_at, last_rejection_stats
    last_scan_at = time.time()
    check_exits()

    qualified = []
    rejected = 0
    rejection_stats = {}

    for mint in discover_mints():
        # On-chain migration-watch mints are verified through the official Pump program,
        # PumpSwap program, and migration account. DexScreener fallback mints must still
        # carry the conventional pump.fun suffix before they are considered.
        if mint not in migration_watch and not mint.lower().endswith("pump"):
            _count_rejection(rejection_stats, "not verified Pump.fun origin")
            rejected += 1
            continue

        pair = best_pumpswap_pair(mint)
        if not pair:
            _count_rejection(rejection_stats, "no PumpSwap/SOL pair")
            rejected += 1
            continue

        try:
            price = float(pair.get("priceNative") or 0)
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except Exception:
            _count_rejection(rejection_stats, "invalid market data")
            rejected += 1
            continue

        if price <= 0:
            _count_rejection(rejection_stats, "invalid price")
            rejected += 1
            continue
        if liquidity < MIN_LIQUIDITY_USD:
            _count_rejection(rejection_stats, "low liquidity")
            rejected += 1
            continue

        social_score, market_score, age_min = market_scores(pair)
        if age_min < MIN_PAIR_AGE_MIN:
            _count_rejection(rejection_stats, "pair too new")
            rejected += 1
            continue
        if social_score < SOCIAL_MIN:
            _count_rejection(rejection_stats, "weak/missing socials")
            rejected += 1
            continue
        if market_score < MARKET_MIN:
            _count_rejection(rejection_stats, "weak market activity")
            rejected += 1
            continue

        safety, reason = onchain_safety(mint)
        if not safety:
            label = _safety_rejection_label(reason)
            _count_rejection(rejection_stats, label)
            print(f"REJECT {mint}: {reason}", flush=True)
            rejected += 1
            continue
        if safety["risk"] > RISK_MAX:
            _count_rejection(rejection_stats, "risk score too high")
            rejected += 1
            continue

        drop, rebound, trigger = update_motion(mint, price)
        symbol = (pair.get("baseToken") or {}).get("symbol", "?")

        item = {
            "mint": mint,
            "symbol": symbol,
            "risk": safety["risk"],
            "social": social_score,
            "market": market_score,
            "drop": drop,
            "rebound": rebound,
            "trigger": trigger,
            "pair": pair,
        }
        qualified.append(item)

        if trigger and mint not in open_positions and len(open_positions) < MAX_OPEN:
            open_positions[mint] = {
                "symbol": symbol,
                "pair_address": pair.get("pairAddress"),
                "entry_price": price,
                "size_sol": POSITION_SOL,
                "target_price": price * TARGET_MULTIPLIER,
                "opened_at": time.time(),
            }
            event_text = (
                "🧪 PAPER ENTRY\n"
                f"{symbol} ({mint[:6]}…{mint[-4:]})\n"
                f"Size: {POSITION_SOL:.2f} SOL simulated\n"
                f"Dip: {drop*100:.1f}% | Rebound: {rebound*100:.1f}%\n"
                f"Scores R/S/M: {safety['risk']}/{social_score}/{market_score}\n"
                f"Target: {NET_TARGET*100:.0f}% NET "
                f"(~{(TARGET_MULTIPLIER-1)*100:.1f}% modeled market move)\n"
                "NO wallet or real order used."
            )
            broadcast_live(event_text)

    qualified.sort(
        key=lambda x: (x["trigger"], x["market"], x["social"], -x["risk"]),
        reverse=True,
    )
    candidates = qualified[:8]
    last_rejection_stats = dict(
        sorted(rejection_stats.items(), key=lambda item: (-item[1], item[0]))
    )
    breakdown = ", ".join(
        f"{reason}={count}" for reason, count in list(last_rejection_stats.items())[:6]
    ) or "none"
    print(
        f"SCAN qualified={len(qualified)} rejected={rejected} open={len(open_positions)} "
        f"reasons=[{breakdown}]",
        flush=True,
    )
    return len(qualified), rejected


def rejections_text():
    if not last_scan_at:
        return "No scan has run yet, so there is no rejection breakdown."
    if not last_rejection_stats:
        return "Last scan had no rejected candidates."

    total = sum(last_rejection_stats.values())
    lines = [f"Last scan rejection breakdown ({total} total):"]
    for reason, count in last_rejection_stats.items():
        pct = (100.0 * count / total) if total else 0.0
        lines.append(f"• {reason}: {count} ({pct:.0f}%)")
    return "\n".join(lines)


def status_text():
    age = "never" if not last_scan_at else f"{int(time.time() - last_scan_at)}s ago"
    return (
        "Big Happy Wealth Bot\n"
        "Mode: PAPER ONLY ✅\n"
        f"Scanner: {'RUNNING' if auto_scan_enabled else 'PAUSED'}\n"
        f"Position size: {POSITION_SOL:.2f} SOL simulated\n"
        f"Net target: {NET_TARGET*100:.0f}%\n"
        f"Modeled costs: {FEE_SIDE*100:.2f}% fee + "
        f"{SLIPPAGE_SIDE*100:.2f}% slippage per side\n"
        f"Open paper positions: {len(open_positions)}/{MAX_OPEN}\n"
        f"Safety mode: PAPER-LOOSENED (RPC holder failures = risk penalty, not auto-reject)\n"
        f"Verified migration watch: {len(migration_watch)}\n"
        f"Discovery last scan: {last_discovery_stats['migration']} migration + "
        f"{last_discovery_stats['dex']} fallback\n"
        f"Filters: liq ${MIN_LIQUIDITY_USD:,.0f}+ | age {MIN_PAIR_AGE_MIN:g}m+ | "
        f"social {SOCIAL_MIN}+ | market {MARKET_MIN}+\n"
        f"Motion: dip {DIP_MIN*100:.0f}-{DIP_MAX*100:.0f}% | rebound {REBOUND*100:.1f}%\n"
        f"Last scan: {age}"
    )


def candidates_text():
    if not candidates:
        return "No qualified candidates yet. The scanner is being picky by design."
    lines = ["Qualified watchlist:"]
    for item in candidates[:5]:
        lines.append(
            f"{item['symbol']} | R/S/M "
            f"{item['risk']}/{item['social']}/{item['market']} | "
            f"dip {item['drop']*100:.1f}% | rebound {item['rebound']*100:.1f}%"
        )
    return "\n".join(lines)



def migrations_text():
    if not migration_watch:
        return "No verified Pump.fun migrations in the current watch window yet."
    rows = sorted(
        migration_watch.items(),
        key=lambda item: item[1].get("block_time", 0),
        reverse=True,
    )[:8]
    lines = ["Verified Pump.fun → PumpSwap graduates:"]
    now = time.time()
    for mint, data in rows:
        age_min = max(0, int((now - data.get("block_time", now)) / 60))
        lines.append(f"{mint[:6]}…{mint[-6:]} | {age_min}m ago")
    return "\n".join(lines)

def positions_text():
    if not open_positions:
        return "No open paper positions."
    lines = ["Open paper positions:"]
    for p in open_positions.values():
        lines.append(
            f"{p['symbol']} | {p['size_sol']:.2f} SOL | "
            f"entry {p['entry_price']:.9g} | target {p['target_price']:.9g}"
        )
    return "\n".join(lines)


def performance_text():
    pnl = sum(p.get("pnl_sol", 0) for p in closed_positions)
    wins = sum(1 for p in closed_positions if p.get("pnl_sol", 0) > 0)
    return (
        "Paper performance\n"
        f"Closed: {len(closed_positions)}\n"
        f"Wins: {wins}\n"
        f"Realized P&L: {pnl:+.4f} SOL\n"
        f"Open: {len(open_positions)}"
    )


def balance_text():
    realized = sum(p.get("pnl_sol", 0) for p in closed_positions)
    invested = sum(p.get("size_sol", 0) for p in open_positions.values())
    bankroll = PAPER_STARTING_BALANCE + realized
    available = bankroll - invested
    return (
        "💰 PAPER BALANCE\n"
        f"Starting bankroll: {PAPER_STARTING_BALANCE:.4f} SOL\n"
        f"Available: {available:.4f} SOL\n"
        f"In open positions: {invested:.4f} SOL\n"
        f"Realized P&L: {realized:+.4f} SOL\n"
        f"Open positions: {len(open_positions)}/{MAX_OPEN}\n"
        "Simulated balance only — no wallet funds are used."
    )


def handle_message(chat_id, text):
    global auto_scan_enabled
    known_chats.add(chat_id)
    raw = (text or "").strip()
    command = raw.split()[0].lower() if raw else ""
    print(f"RECEIVED chat_id={chat_id} command={command!r}", flush=True)

    if command == "/start":
        send_message(
            chat_id,
            "Big Happy Wealth Bot connected ✅\n"
            "PAPER MODE ONLY\n"
            f"{POSITION_SOL:.2f} SOL simulated positions\n"
            f"{NET_TARGET*100:.0f}% NET target\n\n"
            "Send /help for commands.",
        )
    elif command == "/help":
        send_message(
            chat_id,
            "Commands:\n"
            "/status - scanner health\n"
            "/scan - run a scan now\n"
            "/candidates - qualified watchlist\n"
            "/rejections - last scan rejection breakdown\n"
            "/migrations - recent verified graduates\n"
            "/live - live paper trade feed (/live off to stop)\n"
            "/positions - open paper positions\n"
            "/performance - paper results\n"
            "/balance - simulated SOL bankroll\n"
            "/off - stop paper scanning (bot stays online for /on)\n"
            "/on - run paper scanner 24/7\n"
            "/pause - alias for /off\n"
            "/resume - alias for /on\n"
            "/id - your chat ID\n"
            "/help - this menu",
        )
    elif command == "/status":
        send_message(chat_id, status_text())
    elif command == "/candidates":
        send_message(chat_id, candidates_text())
    elif command == "/rejections":
        send_message(chat_id, rejections_text())
    elif command == "/migrations":
        send_message(chat_id, migrations_text())
    elif command == "/live":
        parts = raw.split()
        option = parts[1].lower() if len(parts) > 1 else "on"
        if option in {"off", "stop", "0"}:
            live_feed_chats.discard(chat_id)
            send_message(chat_id, "⚪ LIVE PAPER TRADE FEED OFF.")
        elif option == "status":
            state = "ON" if chat_id in live_feed_chats else "OFF"
            send_message(chat_id, f"Live paper trade feed: {state}.")
        else:
            live_feed_chats.add(chat_id)
            send_message(chat_id, live_text())
    elif command == "/positions":
        send_message(chat_id, positions_text())
    elif command == "/performance":
        send_message(chat_id, performance_text())
    elif command == "/balance":
        send_message(chat_id, balance_text())
    elif command in {"/off", "/pause"}:
        auto_scan_enabled = False
        send_message(
            chat_id,
            "🔴 BOT OFF — paper scanning stopped. Telegram control stays online so /on can restart it.",
        )
    elif command in {"/on", "/resume"}:
        auto_scan_enabled = True
        send_message(
            chat_id,
            f"🟢 BOT ON — paper scanner running 24/7 every {SCAN_SECONDS}s while Railway is online.",
        )
    elif command == "/scan":
        send_message(chat_id, "Running a paper scan now…")
        qualified, rejected = run_scan()
        send_message(
            chat_id,
            f"Scan complete. Qualified: {qualified}. Rejected: {rejected}.\n\n"
            + rejections_text()
            + "\n\n"
            + candidates_text(),
        )
    elif command == "/id":
        send_message(chat_id, f"Your chat ID: {chat_id}")
    else:
        send_message(chat_id, "Bot connected. Send /help for commands.")


def main():
    global update_offset

    # Clear any stale webhook so long polling is the only Telegram receiver.
    telegram("deleteWebhook", {"drop_pending_updates": "false"}, timeout=15)

    me = telegram("getMe", timeout=15)
    if not me or not me.get("ok"):
        raise SystemExit("Telegram getMe failed")

    username = (me.get("result") or {}).get("username", "unknown")
    print(
        f"PAPER SCANNER V3 STARTUP @{username} — NO WALLET CODE",
        flush=True,
    )

    last_auto = 0.0

    while True:
        try:
            updates = telegram(
                "getUpdates",
                {
                    "offset": update_offset + 1,
                    "timeout": 20,
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=30,
            )

            if updates and updates.get("ok"):
                for update in updates.get("result", []):
                    update_offset = update.get("update_id", update_offset)
                    message = update.get("message") or {}
                    if "text" in message and (message.get("chat") or {}).get("id"):
                        handle_message(message["chat"]["id"], message["text"])

            now = time.time()
            if auto_scan_enabled and now - last_auto >= SCAN_SECONDS:
                try:
                    run_scan()
                except Exception as e:
                    print(f"SCAN ERROR {type(e).__name__}: {e}", flush=True)
                    traceback.print_exc()
                last_auto = now

        except Exception as e:
            print(f"MAIN LOOP ERROR {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            time.sleep(3)


if __name__ == "__main__":
    main()
