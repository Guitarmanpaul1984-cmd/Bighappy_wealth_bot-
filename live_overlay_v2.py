import base64, json, os, time, urllib.parse
from pathlib import Path
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

BASE = Path(__file__).with_name("bot(20260929-033135).py")
ns = {"__name__": "big_happy_base", "__file__": str(BASE)}
exec(compile(BASE.read_text(encoding="utf-8"), str(BASE), "exec"), ns)

WSOL = ns["WSOL"]
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

OWNER = os.getenv("OWNER_TELEGRAM_CHAT_ID", "").strip()
LIVE_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "0").lower() in {"1", "true", "on", "yes"}
PRIV = os.getenv("SOLANA_TRADING_PRIVATE_KEY_B58", "").strip()
JUP_KEY = os.getenv("JUPITER_API_KEY", "").strip()
TREASURY = os.getenv("TREASURY_WALLET", "").strip()

MAX_SIZE = float(os.getenv("MAX_LIVE_POSITION_SOL", "0.10"))
MIN_SIZE = float(os.getenv("MIN_LIVE_POSITION_SOL", "0.005"))
MIN_RESERVE = float(os.getenv("MIN_SOL_RESERVE", "0.05"))
MIN_TRADING = float(os.getenv("MIN_TRADING_BALANCE_SOL", "0.20"))
SWEEP_USD = float(os.getenv("PROFIT_SWEEP_USD", "20"))
LIVE_MAX_CONSECUTIVE_LOSSES = int(os.getenv("LIVE_MAX_CONSECUTIVE_LOSSES", "5"))

STATE_PATH = Path(os.getenv("LIVE_STATE_PATH", "/data/live_state.json"))
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
ALLOW_PUBLIC = os.getenv("ALLOW_PUBLIC_RPC_LIVE", "0").lower() in {"1", "true", "on", "yes"}

size_sol = min(float(os.getenv("LIVE_POSITION_SOL", "0.02")), MAX_SIZE)
mode = "off"
selected = os.getenv("RISK_PROFILE", "normal").lower()

# Simple profiles. All use the SAME five-losing-trades circuit breaker.
# The strategy prioritizes active volume + buy pressure instead of simply
# taking every token that passes minimum safety checks.
PROFILES = {
    "safe": dict(
        label="SAFE", risk=20, liq=30000, age=5, market=45,
        vol1=60000, vol5=7500, buys5=15, ratio=1.25, max_chase=25,
        stop=-0.08, be=0.06, be_stop=-0.002,
        tp=(0.08, 0.15, 0.25), parts=(0.40, 0.30, 0.20),
        trail=0.07, time=12, time_max=0.02, opens=1,
        slip=250, impact=0.02, wallet=0.03, priority=300000,
    ),
    "normal": dict(
        label="NORMAL", risk=30, liq=15000, age=3, market=28,
        vol1=35000, vol5=4000, buys5=10, ratio=1.15, max_chase=35,
        stop=-0.12, be=0.08, be_stop=-0.005,
        tp=(0.10, 0.20, 0.35), parts=(0.30, 0.30, 0.20),
        trail=0.10, time=15, time_max=0.03, opens=1,
        slip=400, impact=0.04, wallet=0.05, priority=600000,
    ),
    "fast": dict(
        label="FAST", risk=45, liq=8000, age=2, market=20,
        vol1=20000, vol5=2500, buys5=8, ratio=1.05, max_chase=50,
        stop=-0.16, be=0.10, be_stop=-0.008,
        tp=(0.15, 0.30, 0.55), parts=(0.25, 0.25, 0.25),
        trail=0.14, time=20, time_max=0.04, opens=2,
        slip=700, impact=0.07, wallet=0.08, priority=900000,
    ),
}
PROFILE_ALIASES = {
    "1": "safe", "safe": "safe", "safest": "safe",
    "2": "normal", "normal": "normal", "balanced": "normal",
    "3": "fast", "fast": "fast", "risky": "fast",
}
selected = PROFILE_ALIASES.get(selected, "normal")

positions = {}
events = []
profit_bucket = 0.0
consecutive_losses = 0
halt = None
start_balance = None


def money(x):
    try:
        x = float(x)
    except Exception:
        return "n/a"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.1f}K"
    return f"${x:,.0f}"


def state_storage_ready():
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        probe = STATE_PATH.parent / ".bhwb_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def save_state():
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "positions": positions,
            "events": events[-500:],
            "profit_bucket": profit_bucket,
            "consecutive_losses": consecutive_losses,
            "halt": halt,
            "start_balance": start_balance,
            "selected": selected,
            "size_sol": size_sol,
            "mode": mode,
            "saved_at": time.time(),
        }
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print(f"LIVE STATE SAVE ERROR {type(e).__name__}: {e}", flush=True)


def load_state():
    global positions, events, profit_bucket, consecutive_losses
    global halt, start_balance, selected, size_sol, mode
    if not STATE_PATH.exists():
        return
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        positions = data.get("positions") or {}
        events = data.get("events") or []
        profit_bucket = float(data.get("profit_bucket") or 0.0)
        consecutive_losses = int(data.get("consecutive_losses") or 0)
        halt = data.get("halt")
        start_balance = data.get("start_balance")
        selected = PROFILE_ALIASES.get(str(data.get("selected") or selected).lower(), selected)
        saved_size = float(data.get("size_sol") or size_sol)
        if MIN_SIZE <= saved_size <= MAX_SIZE:
            size_sol = saved_size
        saved_mode = str(data.get("mode") or "off").lower()
        if saved_mode in {"off", "paper", "live"}:
            mode = saved_mode
        print(
            f"STATE positions={len(positions)} events={len(events)} "
            f"losses={consecutive_losses} mode={mode}",
            flush=True,
        )
    except Exception as e:
        print(f"LIVE STATE LOAD ERROR {type(e).__name__}: {e}", flush=True)


load_state()

# Old versions could halt SAFE after two losses. Preserve the streak, but remove
# any old halt until the streak reaches the new five-full-trade rule.
if halt and consecutive_losses < LIVE_MAX_CONSECUTIVE_LOSSES:
    print(
        f"CLEAR LEGACY HALT {consecutive_losses}/{LIVE_MAX_CONSECUTIVE_LOSSES}",
        flush=True,
    )
    halt = None
    save_state()

kp = None
if PRIV:
    try:
        kp = Keypair.from_base58_string(PRIV)
    except Exception:
        print("LIVE WALLET CONFIG ERROR: invalid secret", flush=True)


def p():
    return PROFILES[selected]


def owner_ok(cid):
    try:
        return int(cid) == int(OWNER)
    except Exception:
        return False


def tell(text):
    if OWNER:
        try:
            ns["send_message"](int(OWNER), text)
        except Exception:
            pass


def apply_profile():
    q = p()
    ns.update(
        RISK_MAX=q["risk"],
        MIN_LIQUIDITY_USD=q["liq"],
        MIN_PAIR_AGE_MIN=q["age"],
        MARKET_MIN=q["market"],
        HARD_STOP_NET=q["stop"],
        BREAKEVEN_ARM_NET=q["be"],
        BREAKEVEN_STOP_NET=q["be_stop"],
        TP1_NET=q["tp"][0],
        TP2_NET=q["tp"][1],
        TP3_NET=q["tp"][2],
        TP1_FRACTION=q["parts"][0],
        TP2_FRACTION=q["parts"][1],
        TP3_FRACTION=q["parts"][2],
        RUNNER_TRAIL=q["trail"],
        TIME_STOP_MIN=q["time"],
        TIME_STOP_MAX_NET=q["time_max"],
        MAX_OPEN=q["opens"],
        MAX_CONSECUTIVE_LOSSES=LIVE_MAX_CONSECUTIVE_LOSSES,
    )


apply_profile()
save_state()


def rpc(method, params):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 9, "method": method, "params": params}
    ).encode()
    out = ns["http_json"](
        ns["SOLANA_RPC"],
        data=body,
        headers={"Content-Type": "application/json"},
        timeout=25,
    )
    return None if not out or out.get("error") else out.get("result")


def pub():
    return str(kp.pubkey()) if kp else None


def balance():
    r = rpc("getBalance", [pub(), {"commitment": "confirmed"}]) if pub() else None
    return None if not r else float(r.get("value", 0)) / 1e9


def token_bal(mint):
    r = (
        rpc(
            "getTokenAccountsByOwner",
            [pub(), {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        if pub()
        else None
    )
    if r is None:
        return None
    total = 0
    for x in r.get("value", []):
        try:
            total += int(
                x["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]
            )
        except Exception:
            pass
    return total


def ready():
    if not LIVE_ENABLED:
        return False, "LIVE_TRADING_ENABLED is OFF"
    if not OWNER:
        return False, "OWNER_TELEGRAM_CHAT_ID missing"
    if not kp:
        return False, "trading-wallet secret missing"
    if not state_storage_ready():
        return False, "persistent /data state storage is unavailable"
    if ns["SOLANA_RPC"] == PUBLIC_RPC and not ALLOW_PUBLIC:
        return False, "dedicated SOLANA_RPC_URL required"
    return True, "READY"


def jheaders(content=False):
    h = {"Accept": "application/json", "User-Agent": "BigHappyWealthBot/Live"}
    if JUP_KEY:
        h["x-api-key"] = JUP_KEY
    if content:
        h["Content-Type"] = "application/json"
    return h


def jbase():
    return "https://api.jup.ag" if JUP_KEY else "https://lite-api.jup.ag"


def quote(inp, out, amt, slip=None):
    q = urllib.parse.urlencode(
        {
            "inputMint": inp,
            "outputMint": out,
            "amount": str(int(amt)),
            "slippageBps": str(int(slip or p()["slip"])),
            "swapMode": "ExactIn",
            "restrictIntermediateTokens": "true",
            "instructionVersion": "V2",
        }
    )
    return ns["http_json"](
        f"{jbase()}/swap/v1/quote?{q}", headers=jheaders(), timeout=20
    )


def confirm(sig):
    end = time.time() + 45
    while time.time() < end:
        r = rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        row = (r.get("value") or [None])[0] if r else None
        if row:
            if row.get("err") is not None:
                return False
            if row.get("confirmationStatus") in {"confirmed", "finalized"}:
                return True
        time.sleep(1.2)
    return False


def swap(inp, out, amt):
    ok, why = ready()
    if not ok:
        return None, why

    q = quote(inp, out, amt)
    if not q or q.get("error") or not q.get("outAmount"):
        return None, "quote failed"

    try:
        impact = abs(float(q.get("priceImpactPct") or 0))
    except Exception:
        impact = 1

    if impact > p()["impact"]:
        return None, f"price impact {impact*100:.1f}% too high"

    body = {
        "userPublicKey": pub(),
        "quoteResponse": q,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "priorityLevel": "veryHigh",
                "maxLamports": p()["priority"],
            }
        },
    }
    built = ns["http_json"](
        f"{jbase()}/swap/v1/swap",
        data=json.dumps(body).encode(),
        headers=jheaders(True),
        timeout=30,
    )
    if not built or not built.get("swapTransaction"):
        return None, "swap build failed"

    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(built["swapTransaction"]))
        signed = VersionedTransaction(tx.message, [kp])
        b64 = base64.b64encode(bytes(signed)).decode()
    except Exception as e:
        return None, f"signing failed: {type(e).__name__}"

    sim = rpc(
        "simulateTransaction",
        [b64, {"encoding": "base64", "sigVerify": True, "commitment": "confirmed"}],
    )
    if sim is None or (sim.get("value") or {}).get("err") is not None:
        return None, "simulation rejected"

    before = balance()
    out_before = token_bal(out) if out != WSOL else None

    sig = rpc(
        "sendTransaction",
        [
            b64,
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            },
        ],
    )
    if not isinstance(sig, str) or not confirm(sig):
        return None, "transaction not confirmed"

    after = balance()
    out_after = token_bal(out) if out != WSOL else None
    return {
        "sig": sig,
        "quote": q,
        "impact": impact,
        "before": before,
        "after": after,
        "out_before": out_before,
        "out_after": out_after,
    }, None


def strict_safety(mint):
    old = ns["paper_test_mode"]
    ns["paper_test_mode"] = False
    try:
        return ns["onchain_safety"](mint)
    finally:
        ns["paper_test_mode"] = old


def pair_stats(pair):
    pair = pair or {}
    vol = pair.get("volume") or {}
    tx5 = (pair.get("txns") or {}).get("m5") or {}
    price = pair.get("priceChange") or {}

    def f(v):
        try:
            return float(v or 0)
        except Exception:
            return 0.0

    def i(v):
        try:
            return int(v or 0)
        except Exception:
            return 0

    liq = f((pair.get("liquidity") or {}).get("usd"))
    v5 = f(vol.get("m5"))
    v1 = f(vol.get("h1"))
    buys = i(tx5.get("buys"))
    sells = i(tx5.get("sells"))
    ratio = buys / max(sells, 1)
    mc = f(pair.get("marketCap") or pair.get("fdv"))
    p5 = f(price.get("m5"))
    created = i(pair.get("pairCreatedAt"))
    age = (time.time() * 1000 - created) / 60000 if created else 0.0
    accel = v5 / max(v1 / 12.0, 1.0)
    return {
        "liq": liq,
        "v5": v5,
        "v1": v1,
        "buys": buys,
        "sells": sells,
        "ratio": ratio,
        "mc": mc,
        "p5": p5,
        "age": age,
        "accel": accel,
    }


def volume_score(item):
    s = pair_stats(item.get("pair"))
    q = p()
    # Recent 5m volume gets the largest weight. 1h volume, acceleration,
    # buy pressure and liquidity keep us focused on liquid, active tokens.
    return (
        35 * min(s["v5"] / max(q["vol5"], 1), 3)
        + 25 * min(s["v1"] / max(q["vol1"], 1), 3)
        + 15 * min(s["accel"], 3)
        + 15 * min(s["ratio"] / max(q["ratio"], 0.01), 2)
        + 10 * min(s["liq"] / max(q["liq"], 1), 3)
    )


def candidate_ok(item):
    pair = item["pair"]
    q = p()
    s = pair_stats(pair)

    _, market, age = ns["market_scores"](pair)

    if s["liq"] < q["liq"]:
        return False, "liquidity"
    if age < q["age"]:
        return False, "age"
    if market < q["market"]:
        return False, "market score"

    # Volume-first gate: both sustained hourly activity and current five-minute
    # activity must be present. This avoids dead tokens with old volume.
    if s["v1"] < q["vol1"]:
        return False, "1h volume"
    if s["v5"] < q["vol5"]:
        return False, "5m volume"
    if s["buys"] < q["buys5"]:
        return False, "5m buyers"
    if s["ratio"] < q["ratio"]:
        return False, "buy/sell pressure"
    if s["p5"] < -5:
        return False, "5m price falling"
    if s["p5"] > q["max_chase"]:
        return False, "5m move too extended"

    safe, why = strict_safety(item["mint"])
    if not safe:
        return False, why or "strict safety"
    if safe.get("risk", 999) > q["risk"]:
        return False, "risk score"

    return True, {"safety": safe, "stats": s}


def entry_size():
    b = balance()
    if b is None:
        return 0
    return max(
        0,
        min(
            size_sol,
            MAX_SIZE,
            b * p()["wallet"],
            b - MIN_RESERVE - MIN_TRADING,
        ),
    )


def day_pnl():
    d = time.strftime("%Y-%m-%d", time.gmtime())
    return sum(e.get("pnl", 0.0) for e in events if e.get("day") == d)


def breaker():
    global halt
    if halt:
        return halt
    if consecutive_losses >= LIVE_MAX_CONSECUTIVE_LOSSES:
        halt = f"{consecutive_losses} consecutive losing trades"
        tell(
            "🛑 REAL CIRCUIT BREAKER\n"
            f"{halt}\n"
            "New REAL entries are blocked. Existing REAL positions remain protected."
        )
        save_state()
    return halt


def axiom_url(mint):
    return f"https://axiom.trade/t/{mint}"


def open_live(item):
    mint = item["mint"]
    ok, detail = candidate_ok(item)
    if not ok:
        return False, detail

    amt = entry_size()
    if amt < MIN_SIZE:
        return False, "not enough spendable SOL after reserves/profile cap"

    stats = detail["stats"]
    before_tok = token_bal(mint) or 0
    r, err = swap(WSOL, mint, int(amt * 1e9))
    if not r:
        return False, err

    after_tok = token_bal(mint)
    got = max(0, int(after_tok or 0) - int(before_tok))
    if got <= 0:
        got = int(r["quote"].get("outAmount") or 0)
    if got <= 0:
        return False, "could not measure tokens received"

    spent = amt
    if (
        r["before"] is not None
        and r["after"] is not None
        and r["before"] > r["after"]
    ):
        spent = r["before"] - r["after"]

    positions[mint] = {
        "mint": mint,
        "symbol": item.get("symbol", "?"),
        "initial_raw": got,
        "raw": got,
        "cost": spent,
        "remaining_cost": spent,
        "opened": time.time(),
        "entry_liq": stats["liq"],
        "entry_mc": stats["mc"],
        "entry_v5": stats["v5"],
        "entry_v1": stats["v1"],
        "entry_buys": stats["buys"],
        "entry_sells": stats["sells"],
        "trade_realized_pnl": 0.0,
        "peak": -999.0,
        "be": False,
        "tp": [False, False, False],
    }
    save_state()

    tell(
        "🟣 REAL BUY\n"
        f"{item.get('symbol','?')}\n"
        f"Spent: {spent:.6f} SOL\n"
        f"Entry MC: {money(stats['mc'])}\n"
        f"Liquidity: {money(stats['liq'])}\n"
        f"Volume: 5m {money(stats['v5'])} | 1h {money(stats['v1'])}\n"
        f"5m flow: {stats['buys']} buys / {stats['sells']} sells ({stats['ratio']:.2f}x)\n"
        f"5m move: {stats['p5']:+.1f}% | Age: {stats['age']:.0f}m\n"
        f"Price impact: {r['impact']*100:.2f}%\n"
        f"Profile: {p()['label']}\n"
        f"CA: {mint}\n"
        f"Axiom: {axiom_url(mint)}\n"
        f"Tx: {r['sig']}"
    )
    return True, r["sig"]


def record_fill_pnl(x):
    global profit_bucket
    events.append(
        {"day": time.strftime("%Y-%m-%d", time.gmtime()), "pnl": x, "time": time.time()}
    )
    profit_bucket += x


def record_closed_trade(total_pnl):
    global consecutive_losses
    if total_pnl < 0:
        consecutive_losses += 1
    else:
        consecutive_losses = 0
    breaker()
    save_state()


def sell(mint, raw, reason):
    pos = positions.get(mint)
    if not pos:
        return False

    raw = min(int(raw), int(pos["raw"]))
    frac = raw / max(pos["raw"], 1)
    basis = pos["remaining_cost"] * frac

    r, err = swap(mint, WSOL, raw)
    if not r:
        tell(f'⚠️ REAL EXIT FAILED {pos["symbol"]}: {err}')
        return False

    if r["before"] is not None and r["after"] is not None:
        received = max(0, r["after"] - r["before"])
    else:
        received = int(r["quote"]["outAmount"]) / 1e9

    pnl = received - basis
    pos["raw"] -= raw
    pos["remaining_cost"] -= basis
    pos["trade_realized_pnl"] = float(pos.get("trade_realized_pnl") or 0.0) + pnl
    record_fill_pnl(pnl)

    exit_pair = ns["best_pumpswap_pair"](mint)
    exit_stats = pair_stats(exit_pair) if exit_pair else {"mc": 0}

    is_closed = pos["raw"] <= max(1, int(pos["initial_raw"] * 0.002))
    total_trade_pnl = pos["trade_realized_pnl"]

    tell(
        "🟣 REAL SELL\n"
        f"{pos['symbol']} — {reason}\n"
        f"Received: {received:.6f} SOL\n"
        f"Fill P&L: {pnl:+.6f} SOL\n"
        f"Trade P&L so far: {total_trade_pnl:+.6f} SOL\n"
        f"Entry MC: {money(pos.get('entry_mc',0))}\n"
        f"Exit MC: {money(exit_stats.get('mc',0))}\n"
        f"Loss streak: {consecutive_losses}/{LIVE_MAX_CONSECUTIVE_LOSSES}"
        + (" (updates when trade fully closes)" if not is_closed else "")
        + f"\nCA: {mint}\n"
        f"Axiom: {axiom_url(mint)}\n"
        f"Tx: {r['sig']}"
    )

    if is_closed:
        positions.pop(mint, None)
        record_closed_trade(total_trade_pnl)

    save_state()
    maybe_sweep()
    return True


def current_net(pos):
    q = quote(pos["mint"], WSOL, pos["raw"])
    if not q or not q.get("outAmount") or pos["remaining_cost"] <= 0:
        return None
    return (int(q["outAmount"]) / 1e9 - pos["remaining_cost"]) / pos["remaining_cost"]


def exits():
    for mint, pos in list(positions.items()):
        net = current_net(pos)
        if net is None:
            continue

        q = p()
        pos["peak"] = max(float(pos.get("peak", -999.0)), net)
        age = (time.time() - pos["opened"]) / 60

        pair = ns["best_pumpswap_pair"](mint)
        if pair:
            try:
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            except Exception:
                liq = 0
            if pos.get("entry_liq") and liq < pos["entry_liq"] * ns["LIQUIDITY_COLLAPSE_RATIO"]:
                sell(mint, pos["raw"], "liquidity collapse")
                continue

        if net <= q["stop"]:
            sell(mint, pos["raw"], f"hard stop {net*100:.1f}%")
            continue

        if not pos.get("be") and net >= q["be"]:
            pos["be"] = True

        if pos.get("be") and net <= q["be_stop"]:
            sell(mint, pos["raw"], "breakeven protection")
            continue

        tp_flags = pos.get("tp") or [False, False, False]
        pos["tp"] = tp_flags
        for i, target in enumerate(q["tp"]):
            if not tp_flags[i] and net >= target:
                amt = min(
                    pos["raw"],
                    max(1, int(pos["initial_raw"] * q["parts"][i])),
                )
                if sell(mint, amt, f"TP{i+1}") and mint in positions:
                    positions[mint]["tp"][i] = True
                break
        else:
            if all(tp_flags) and net <= pos["peak"] - q["trail"]:
                sell(mint, pos["raw"], "runner trail")
            elif age >= q["time"] and net <= q["time_max"]:
                sell(mint, pos["raw"], "time stop")


def maybe_entries():
    if mode != "live" or breaker() or not ready()[0] or len(positions) >= p()["opens"]:
        return

    ranked = sorted(
        ns.get("candidates") or [],
        key=volume_score,
        reverse=True,
    )

    for item in ranked[:8]:
        if len(positions) >= p()["opens"]:
            break
        if item["mint"] in positions:
            continue
        ok, _ = open_live(item)
        if ok and p()["opens"] == 1:
            break


def sol_usd():
    q = quote(WSOL, USDC, 50_000_000, 100)
    return (int(q["outAmount"]) / 1e6) / 0.05 if q and q.get("outAmount") else None


def send_sol(lamports):
    if not TREASURY:
        return None
    try:
        to = Pubkey.from_string(TREASURY)
        bh = rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"]
    except Exception:
        return None

    ix = transfer(
        TransferParams(
            from_pubkey=kp.pubkey(),
            to_pubkey=to,
            lamports=int(lamports),
        )
    )
    msg = MessageV0.try_compile(
        kp.pubkey(), [ix], [], Hash.from_string(bh)
    )
    tx = VersionedTransaction(msg, [kp])
    b64 = base64.b64encode(bytes(tx)).decode()
    sig = rpc(
        "sendTransaction",
        [
            b64,
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 3,
            },
        ],
    )
    return sig if isinstance(sig, str) and confirm(sig) else None


def maybe_sweep():
    global profit_bucket
    if not TREASURY or profit_bucket <= 0:
        return

    px = sol_usd()
    if not px:
        return

    need = SWEEP_USD / px
    if profit_bucket < need:
        return

    b = balance()
    if b is None or b - need < MIN_RESERVE + MIN_TRADING:
        return

    sig = send_sol(int(need * 1e9))
    if sig:
        profit_bucket = max(0, profit_bucket - need)
        save_state()
        tell(
            f"🏦 PROFIT SWEEP\nSent ~${SWEEP_USD:.0f} "
            f"({need:.6f} SOL) to treasury.\nTx {sig}"
        )


def clear_paper_positions():
    count = len(ns.get("open_positions") or {})
    try:
        ns["open_positions"].clear()
    except Exception:
        ns["open_positions"] = {}
    ns["candidates"] = []
    return count


def profile_text():
    q = p()
    return (
        f"{q['label']} | Liq {money(q['liq'])}+ | "
        f"Vol 5m {money(q['vol5'])}+ | 1h {money(q['vol1'])}+ | "
        f"Buy/sell {q['ratio']:.2f}x+ | Stop {q['stop']*100:.0f}%"
    )


def status_text():
    ok, why = ready()
    b = balance() if kp else None
    paper_open = len(ns.get("open_positions") or {})
    return (
        "⚙️ STATUS\n"
        f"Mode: {mode.upper()}\n"
        f"Paper: {'ON' if mode == 'paper' else 'OFF'}"
        + (f" ({paper_open} open)" if mode == "paper" else "")
        + "\n"
        f"Real entries: {'ON' if mode == 'live' else 'OFF'}\n"
        f"Real positions: {len(positions)}/{p()['opens']}\n"
        f"Profile: {profile_text()}\n"
        f"Size request: {size_sol:.4f} SOL\n"
        f"Real loss streak: {consecutive_losses}/{LIVE_MAX_CONSECUTIVE_LOSSES}\n"
        f"Live plumbing: {'READY' if ok else 'LOCKED — ' + why}"
        + (f"\nWallet: {b:.6f} SOL" if b is not None else "")
        + f"\nProfit sweep: ${SWEEP_USD:.0f}"
    )


base_handle = ns["handle_message"]
base_scan = ns["run_scan"]


def live_discovery_scan():
    # Reuse the proven discovery code, but make paper positions/trades impossible.
    saved_open = ns.get("open_positions")
    saved_risk = ns.get("risk_halt_reason")
    saved_test = ns.get("paper_test_mode")
    saved_auto = ns.get("auto_trade_paper")
    try:
        ns["open_positions"] = {}
        ns["risk_halt_reason"] = "PAPER ENGINE DISABLED IN REAL MODE"
        ns["paper_test_mode"] = False
        ns["auto_trade_paper"] = False
        return base_scan()
    finally:
        ns["open_positions"] = saved_open
        ns["risk_halt_reason"] = saved_risk
        ns["paper_test_mode"] = saved_test
        ns["auto_trade_paper"] = saved_auto


def dual_scan():
    # Safety exception: any already-open REAL position is always exit-managed,
    # even when entry mode is OFF.
    if positions:
        exits()

    if mode == "paper":
        return base_scan()

    if mode == "live":
        out = live_discovery_scan()
        maybe_entries()
        return out

    # OFF: no paper scan and no new real-entry scan.
    return 0, 0


ns["run_scan"] = dual_scan


def set_off(cid):
    global mode
    mode = "off"
    ns["auto_scan_enabled"] = True
    save_state()
    ns["send_message"](
        cid,
        "⏹ OFF\nPaper scanning/trading: OFF\nNew REAL entries: OFF\n"
        "Any already-open REAL position stays protected until it closes."
    )


def set_paper(cid):
    global mode
    if positions:
        ns["send_message"](
            cid,
            "Paper mode cannot start while a REAL position is open.\n"
            "Use /positions to check it, /real off to stop new entries, "
            "or /panic to close it."
        )
        return
    mode = "paper"
    ns["auto_scan_enabled"] = True
    save_state()
    ns["send_message"](
        cid,
        "🧪 PAPER ON\nPaper scanner/trades: ON\nREAL entries: OFF"
    )


def set_real(cid):
    global mode, start_balance, halt
    ok, why = ready()
    b = balance()

    if not ok:
        ns["send_message"](cid, "🔒 REAL NOT READY\n" + why)
        return

    if b is None or b < MIN_RESERVE + MIN_TRADING + MIN_SIZE:
        ns["send_message"](cid, "🔒 REAL NOT READY\nWallet balance too low for configured reserves.")
        return

    cleared = clear_paper_positions()
    # If the old code stopped at 2/3/4 losses, release that old halt.
    if halt and consecutive_losses < LIVE_MAX_CONSECUTIVE_LOSSES:
        halt = None

    mode = "live"
    ns["auto_scan_enabled"] = True
    start_balance = start_balance or b
    save_state()

    extra = f"\nCleared {cleared} old paper position(s)." if cleared else ""
    ns["send_message"](
        cid,
        "🟣 REAL ON\nPaper scanner/trades: OFF\n"
        f"Profile: {profile_text()}\n"
        f"Loss breaker: {LIVE_MAX_CONSECUTIVE_LOSSES} full losing trades in a row."
        + extra
    )


def handle(cid, text):
    global mode, size_sol, selected, halt, consecutive_losses

    raw = (text or "").strip()
    a = raw.split()
    cmd = a[0].lower() if a else ""

    protected = {
        "/paper", "/real", "/off", "/on", "/status", "/settings",
        "/size", "/risk", "/riskprofile", "/balance", "/positions",
        "/livepositions", "/performance", "/liveperformance",
        "/panic", "/resume", "/liveresume", "/sweep", "/scan",
    }
    if cmd in protected and not owner_ok(cid):
        ns["send_message"](cid, "🔒 Trading controls are owner-locked.")
        return

    if cmd == "/paper":
        opt = a[1].lower() if len(a) > 1 else "status"
        if opt in {"on", "start", "1"}:
            set_paper(cid)
        elif opt in {"off", "stop", "0"}:
            set_off(cid)
        else:
            ns["send_message"](
                cid, f"Paper: {'ON' if mode == 'paper' else 'OFF'}\nUse /paper on or /paper off"
            )
        return

    if cmd == "/real":
        opt = a[1].lower() if len(a) > 1 else "status"
        if opt in {"on", "start", "1"}:
            set_real(cid)
        elif opt in {"off", "stop", "0"}:
            set_off(cid)
        else:
            ns["send_message"](
                cid, f"Real entries: {'ON' if mode == 'live' else 'OFF'}\nUse /real on or /real off"
            )
        return

    if cmd in {"/off", "/pause"}:
        set_off(cid)
        return

    if cmd == "/on":
        ns["send_message"](cid, "Choose one:\n/paper on\n/real on")
        return

    if cmd in {"/status", "/settings"}:
        ns["send_message"](cid, status_text())
        return

    if cmd == "/size":
        if len(a) < 2:
            ns["send_message"](cid, f"Trade size request: {size_sol:.4f} SOL\nExample: /size 0.01")
            return
        try:
            x = float(a[1])
        except Exception:
            ns["send_message"](cid, "Invalid size.")
            return
        if not MIN_SIZE <= x <= MAX_SIZE:
            ns["send_message"](cid, f"Allowed size: {MIN_SIZE:.4f} to {MAX_SIZE:.4f} SOL")
            return
        size_sol = x
        ns["POSITION_SOL"] = x
        save_state()
        ns["send_message"](cid, f"✅ Size set to {x:.4f} SOL. Wallet/profile caps can reduce actual size.")
        return

    if cmd in {"/risk", "/riskprofile"}:
        if len(a) < 2:
            ns["send_message"](
                cid,
                "Risk profiles:\n"
                "SAFE — stricter, slower\n"
                "NORMAL — default volume strategy\n"
                "FAST — more opportunities, more risk\n\n"
                "Use /risk safe, /risk normal, or /risk fast"
            )
            return
        opt = PROFILE_ALIASES.get(a[1].lower())
        if not opt:
            ns["send_message"](cid, "Use /risk safe, /risk normal, or /risk fast")
            return
        selected = opt
        apply_profile()
        save_state()
        ns["send_message"](cid, "✅ " + profile_text())
        return

    if cmd == "/balance":
        b = balance() if kp else None
        paper_bank = ns["paper_bankroll_sol"]() if "paper_bankroll_sol" in ns else None
        text_out = "💰 BALANCE"
        if b is not None:
            text_out += f"\nReal wallet: {b:.6f} SOL"
        if paper_bank is not None:
            text_out += f"\nPaper bankroll: {paper_bank:.4f} SOL"
        ns["send_message"](cid, text_out)
        return

    if cmd in {"/positions", "/livepositions"}:
        if mode == "paper":
            pp = ns.get("open_positions") or {}
            if not pp:
                ns["send_message"](cid, "No open PAPER positions.")
            else:
                lines = ["🧪 PAPER POSITIONS"]
                for x in pp.values():
                    lines.append(f"{x.get('symbol','?')} | {x.get('size_sol',0):.4f} SOL")
                ns["send_message"](cid, "\n".join(lines))
            return

        if not positions:
            ns["send_message"](cid, "No open REAL positions.")
            return

        lines = ["🟣 REAL POSITIONS"]
        for x in positions.values():
            lines.append(
                f"{x['symbol']} | cost left {x['remaining_cost']:.4f} SOL | "
                f"entry MC {money(x.get('entry_mc',0))}"
            )
        ns["send_message"](cid, "\n".join(lines))
        return

    if cmd in {"/performance", "/liveperformance"}:
        pnl = sum(e.get("pnl", 0.0) for e in events)
        ns["send_message"](
            cid,
            "🟣 REAL PERFORMANCE\n"
            f"Realized P&L: {pnl:+.6f} SOL\n"
            f"Today: {day_pnl():+.6f} SOL\n"
            f"Loss streak: {consecutive_losses}/{LIVE_MAX_CONSECUTIVE_LOSSES}\n"
            f"Circuit: {halt or 'READY'}"
        )
        return

    if cmd in {"/resume", "/liveresume"}:
        halt = None
        consecutive_losses = 0
        save_state()
        ns["send_message"](
            cid,
            f"✅ Circuit reset. Loss streak 0/{LIVE_MAX_CONSECUTIVE_LOSSES}."
        )
        return

    if cmd == "/sweep":
        px = sol_usd() if kp else None
        usd = profit_bucket * px if px else None
        ns["send_message"](
            cid,
            f"🏦 SWEEP\nThreshold ${SWEEP_USD:.0f}\n"
            f"Bucket {profit_bucket:+.6f} SOL"
            + (f" (~${usd:.2f})" if usd is not None else "")
            + f"\nTreasury {'configured' if TREASURY else 'not configured'}"
        )
        return

    if cmd == "/panic":
        mode = "off"
        clear_paper_positions()
        save_state()
        ns["send_message"](cid, "🚨 PANIC — entries OFF; closing all REAL positions now.")
        for mint, x in list(positions.items()):
            sell(mint, x["raw"], "PANIC EXIT")
        return

    if cmd == "/scan":
        if mode == "off":
            ns["send_message"](cid, "Mode is OFF. Use /paper on or /real on first.")
            return
        ns["send_message"](cid, f"Scanning in {mode.upper()} mode now…")
        dual_scan()
        return

    if cmd in {"/autotrade", "/testmode"} and mode != "paper":
        ns["send_message"](cid, "Paper mode is OFF. Use /paper on first.")
        return

    if cmd == "/help":
        ns["send_message"](
            cid,
            "BIG HAPPY WEALTH BOT\n\n"
            "/paper on — paper only\n"
            "/paper off — stop paper\n"
            "/real on — real only\n"
            "/real off — stop new real entries\n"
            "/status — mode, profile, loss streak\n"
            "/risk safe|normal|fast\n"
            "/size 0.01\n"
            "/balance\n"
            "/positions\n"
            "/performance\n"
            "/scan\n"
            "/panic — close REAL positions + turn off\n"
            "/resume — reset 5-loss breaker\n\n"
            "Paper and REAL entry modes never run together."
        )
        return

    # Keep base informational commands available where useful.
    base_handle(cid, text)


ns["handle_message"] = handle

print(
    f"EXCLUSIVE ENGINE STARTUP mode={mode} profile={selected} "
    f"live_ready={ready()[0]} loss_limit={LIVE_MAX_CONSECUTIVE_LOSSES} "
    f"state={STATE_PATH}",
    flush=True,
)

if __name__ == "__main__":
    ns["main"]()
