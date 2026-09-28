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

POSITION_SOL = float(os.getenv("PAPER_POSITION_SOL", "0.10"))
NET_TARGET = float(os.getenv("NET_TARGET_PCT", "10")) / 100.0
FEE_SIDE = float(os.getenv("SIM_FEE_PCT_SIDE", "1.25")) / 100.0
SLIPPAGE_SIDE = float(os.getenv("SIM_SLIPPAGE_PCT_SIDE", "0.80")) / 100.0
MAX_OPEN = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
RISK_MAX = int(os.getenv("RISK_MAX", "20"))
SOCIAL_MIN = int(os.getenv("SOCIAL_MIN", "55"))
MARKET_MIN = int(os.getenv("MARKET_MIN", "65"))
DIP_MIN = float(os.getenv("DIP_MIN_PCT", "15")) / 100.0
DIP_MAX = float(os.getenv("DIP_MAX_PCT", "35")) / 100.0
REBOUND = float(os.getenv("REBOUND_PCT", "3")) / 100.0
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "60"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "25000"))
MIN_PAIR_AGE_MIN = float(os.getenv("MIN_PAIR_AGE_MIN", "10"))

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
        print(f"HTTP {e.code} {url}: {body[:300]}", flush=True)
    except Exception as e:
        print(f"HTTP ERROR {type(e).__name__}: {e}", flush=True)
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
        return None, "holder data unavailable"

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

    if top1 > 35 or top10 > 80:
        return None, f"holder concentration {top1:.1f}/{top10:.1f}%"

    risk = 0
    if top1 > 20:
        risk += 15
    if top10 > 60:
        risk += 10

    return {"risk": risk, "top1": top1, "top10": top10}, None


def discover_mints(limit=20):
    out = []
    seen = set()
    for endpoint in ("token-boosts/latest/v1", "token-profiles/latest/v1"):
        data = http_json(f"{DEX_API}/{endpoint}", timeout=15)
        if not isinstance(data, list):
            continue
        for item in data:
            mint = item.get("tokenAddress")
            if item.get("chainId") == "solana" and mint and mint not in seen:
                seen.add(mint)
                out.append(mint)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
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
            broadcast(
                "🎯 PAPER TARGET HIT\n"
                f"{position['symbol']} ({mint[:6]}…{mint[-4:]})\n"
                f"Net P&L: +{pnl:.4f} SOL\n"
                "No real trade was placed."
            )


def run_scan():
    global candidates, last_scan_at
    last_scan_at = time.time()
    check_exits()

    qualified = []
    rejected = 0

    for mint in discover_mints():
        # Pump.fun mints conventionally end in "pump". This is only a candidate gate,
        # not proof of origin or safety.
        if not mint.lower().endswith("pump"):
            rejected += 1
            continue

        pair = best_pumpswap_pair(mint)
        if not pair:
            rejected += 1
            continue

        try:
            price = float(pair.get("priceNative") or 0)
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except Exception:
            rejected += 1
            continue

        if price <= 0 or liquidity < MIN_LIQUIDITY_USD:
            rejected += 1
            continue

        social_score, market_score, age_min = market_scores(pair)
        if (
            age_min < MIN_PAIR_AGE_MIN
            or social_score < SOCIAL_MIN
            or market_score < MARKET_MIN
        ):
            rejected += 1
            continue

        safety, reason = onchain_safety(mint)
        if not safety:
            print(f"REJECT {mint}: {reason}", flush=True)
            rejected += 1
            continue
        if safety["risk"] > RISK_MAX:
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
            broadcast(
                "🧪 PAPER ENTRY\n"
                f"{symbol} ({mint[:6]}…{mint[-4:]})\n"
                f"Size: {POSITION_SOL:.2f} SOL simulated\n"
                f"Dip: {drop*100:.1f}% | Rebound: {rebound*100:.1f}%\n"
                f"Scores R/S/M: {safety['risk']}/{social_score}/{market_score}\n"
                f"Target: {NET_TARGET*100:.0f}% NET "
                f"(~{(TARGET_MULTIPLIER-1)*100:.1f}% modeled market move)\n"
                "NO wallet or real order used."
            )

    qualified.sort(
        key=lambda x: (x["trigger"], x["market"], x["social"], -x["risk"]),
        reverse=True,
    )
    candidates = qualified[:8]
    print(
        f"SCAN qualified={len(qualified)} rejected={rejected} open={len(open_positions)}",
        flush=True,
    )
    return len(qualified), rejected


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
            "/positions - open paper positions\n"
            "/performance - paper results\n"
            "/pause - pause auto scanning\n"
            "/resume - resume auto scanning\n"
            "/id - your chat ID\n"
            "/help - this menu",
        )
    elif command == "/status":
        send_message(chat_id, status_text())
    elif command == "/candidates":
        send_message(chat_id, candidates_text())
    elif command == "/positions":
        send_message(chat_id, positions_text())
    elif command == "/performance":
        send_message(chat_id, performance_text())
    elif command == "/pause":
        auto_scan_enabled = False
        send_message(chat_id, "Auto scanner PAUSED. Paper mode only.")
    elif command == "/resume":
        auto_scan_enabled = True
        send_message(chat_id, "Auto scanner RUNNING. Paper mode only.")
    elif command == "/scan":
        send_message(chat_id, "Running a paper scan now…")
        qualified, rejected = run_scan()
        send_message(
            chat_id,
            f"Scan complete. Qualified: {qualified}. Rejected: {rejected}.\n\n"
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
