import os
import runpy
import time

# BIG HAPPY WEALTH BOT — PROFIT V5.2 PAPER
# Profit-protection experiment. PAPER ONLY. No wallet or real-order code.
#
# Main changes from V5.1:
# - Reject 5m chase above 10%
# - 3 qualifying scans instead of 2
# - Stronger liquidity/volume/buy-pressure gates
# - Smaller -6.5% hard stop
# - Bank 60% at TP1
# - Protect the remainder immediately after TP1
# - Dynamic post-TP profit floor follows peak net P&L
# - Faster time stop
# - Tighter loss circuit breaker

if os.getenv("LIVE_TRADING_ENABLED", "0").strip().lower() in {"1", "true", "on", "yes"}:
    raise SystemExit("PROFIT V5.2 PAPER REFUSES TO START: LIVE_TRADING_ENABLED must be 0.")

# Fresh ledgers so V5.2 can be measured separately from V5.1.
os.environ["PAPER_VALIDATION_PATH_V3"] = "/data/paper_validation_profit_v5_2.json"
os.environ["PAPER_UX_LEDGER_PATH"] = "/data/paper_ux_profit_v5_2.json"

# Entry quality. Continuous scanning is fine; forced trading is not.
os.environ["PAPER_CONFIRM_SCANS"] = "3"
os.environ["PAPER_MIN_VOLUME_ACCEL"] = "1.50"
os.environ["PAPER_MIN_5M_MOVE_PCT"] = "2.5"
os.environ["PAPER_MAX_5M_MOVE_PCT"] = "10.0"
os.environ["PAPER_MIN_PROFIT_FACTOR"] = "1.50"

# Simulated sizing + API pressure kept conservative.
os.environ["PAPER_POSITION_SOL"] = "0.05"
os.environ["PAPER_RISK_PER_TRADE_PCT"] = "0.30"
os.environ["V3_DISCOVERY_SECONDS"] = "20"
os.environ["V3_EXIT_SECONDS"] = "2"
os.environ["V3_TELEGRAM_POLL_SECONDS"] = "4"
os.environ["DISCOVERY_LIMIT"] = "8"

d = runpy.run_path("/app/Strategy_v3.py", run_name="big_happy_profit_v5_2")
v = d["v2"]
ns = d["ns"]

# Hard PAPER lock.
v["LIVE_ENABLED"] = False
v["mode"] = "paper"
ns["paper_test_mode"] = False
# Scan continuously, but do not force-fill a slot with a non-trigger candidate.
ns["auto_trade_paper"] = False
ns["POSITION_SOL"] = 0.05

# Quality-first profile.
q = v["PROFILES"]["fast"]
q.update(
    label="PROFIT V5.2 PAPER",
    risk=25,
    liq=35000,
    age=3,
    market=30,
    vol1=75000,
    vol5=10000,
    buys5=20,
    ratio=1.50,
    max_chase=10,
    stop=-0.065,
    be=0.040,
    be_stop=0.005,
    tp=(0.075, 0.13, 0.22),
    parts=(0.60, 0.20, 0.10),
    trail=0.05,
    time=5,
    time_max=0.015,
    opens=1,
    slip=500,
    impact=0.04,
    wallet=0.05,
    priority=700000,
)

v["selected"] = "fast"
v["apply_profile"]()

# PAPER exit/risk settings.
ns["HARD_STOP_NET"] = -0.065
ns["BREAKEVEN_ARM_NET"] = 0.040
ns["BREAKEVEN_STOP_NET"] = 0.005

ns["TP1_NET"] = 0.075
ns["TP2_NET"] = 0.13
ns["TP3_NET"] = 0.22
ns["TP1_FRACTION"] = 0.60
ns["TP2_FRACTION"] = 0.20
ns["TP3_FRACTION"] = 0.10
ns["RUNNER_TRAIL"] = 0.05

ns["TIME_STOP_MIN"] = 5.0
ns["TIME_STOP_MAX_NET"] = 0.015
ns["MAX_OPEN"] = 1

# Fail sooner when the trade never develops.
FAIL_FAST_MIN = 2.5
FAIL_FAST_PEAK_MAX_NET = 0.025
FAIL_FAST_EXIT_NET = -0.025

# Once a TP hits, a winner should not casually become a full loser.
POST_TP1_STATIC_FLOOR = 0.010   # +1.0% net minimum target on remainder
POST_TP2_STATIC_FLOOR = 0.060   # +6.0% after TP2
POST_TP3_STATIC_FLOOR = 0.120   # +12.0% after TP3
POST_TP1_PEAK_GIVEBACK = 0.050  # protect peak net minus 5 percentage points
POST_TP2_PEAK_GIVEBACK = 0.040
POST_TP3_PEAK_GIVEBACK = 0.035

# PAPER circuit breakers.
ns["MAX_CONSECUTIVE_LOSSES"] = 4
ns["DAILY_LOSS_LIMIT"] = 0.03
ns["risk_halt_reason"] = None
ns["risk_halt_day"] = None


def _post_tp_floor(position):
    """Return the protected net-P&L floor for the remaining position."""
    peak = float(position.get("peak_net_pct", -999.0))

    if position.get("tp3_done"):
        return max(POST_TP3_STATIC_FLOOR, peak - POST_TP3_PEAK_GIVEBACK)
    if position.get("tp2_done"):
        return max(POST_TP2_STATIC_FLOOR, peak - POST_TP2_PEAK_GIVEBACK)
    if position.get("tp1_done"):
        return max(POST_TP1_STATIC_FLOOR, peak - POST_TP1_PEAK_GIVEBACK)
    return None


def check_exits_v52():
    """V5.2 paper exit engine with immediate post-TP profit protection."""
    ns["_maybe_reset_daily_breaker"]()

    for mint, position in list(ns["open_positions"].items()):
        pair = ns["fetch_pair"](position["pair_address"])
        if not pair:
            position["missing_pair_checks"] = position.get("missing_pair_checks", 0) + 1
            if position["missing_pair_checks"] >= 3 and not position.get("missing_pair_alerted"):
                position["missing_pair_alerted"] = True
                ns["_announce_trade_event"](
                    "⚠️ PAPER MARKET DATA ALERT\n"
                    f"{position.get('symbol', '?')} ({mint[:6]}…{mint[-4:]})\n"
                    "Pair data has been unavailable for 3 checks. "
                    "No fake exit price was assumed."
                )
            continue

        position["missing_pair_checks"] = 0

        try:
            current_price = float(pair.get("priceNative") or 0)
            current_liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except Exception:
            continue

        if current_price <= 0:
            continue

        position["peak_price"] = max(
            float(position.get("peak_price") or position["entry_price"]),
            current_price,
        )

        remaining_size = float(position.get("size_sol", 0.0))
        if remaining_size <= 0:
            continue

        unrealized = ns["net_pnl_sol"](position, current_price)
        net_pct = unrealized / remaining_size
        position["peak_net_pct"] = max(
            float(position.get("peak_net_pct", -999.0)),
            net_pct,
        )

        if net_pct >= ns["BREAKEVEN_ARM_NET"]:
            position["breakeven_armed"] = True

        # 1) Market-structure emergency exits.
        entry_liquidity = max(0.0, float(position.get("entry_liquidity_usd") or 0.0))
        collapse_floor = max(1000.0, entry_liquidity * ns["LIQUIDITY_COLLAPSE_RATIO"])
        if (
            current_liquidity > 0
            and entry_liquidity > 0
            and current_liquidity < collapse_floor
        ):
            ns["execute_paper_exit"](
                mint, position, current_price, remaining_size,
                f"liquidity collapse (${current_liquidity:,.0f})",
            )
            continue

        buys5, sells5 = ns["_pair_sell_pressure"](pair)
        if (
            sells5 >= max(3, int(max(1, buys5) * ns["PANIC_SELL_RATIO"]))
            and net_pct <= ns["PANIC_SELL_MAX_NET"]
        ):
            ns["execute_paper_exit"](
                mint, position, current_price, remaining_size,
                f"sell-pressure emergency ({sells5} sells / {buys5} buys in 5m)",
            )
            continue

        # 2) Protect profits FIRST after any TP.
        protected_floor = _post_tp_floor(position)
        if protected_floor is not None and net_pct <= protected_floor:
            ns["execute_paper_exit"](
                mint, position, current_price, remaining_size,
                f"post-TP profit protection {protected_floor*100:.1f}% net",
            )
            continue

        # 3) Breakeven protection before the full hard stop.
        if position.get("breakeven_armed") and net_pct <= ns["BREAKEVEN_STOP_NET"]:
            ns["execute_paper_exit"](
                mint, position, current_price, remaining_size,
                f"breakeven protection {ns['BREAKEVEN_STOP_NET']*100:.1f}% net",
            )
            continue

        # 4) Catastrophic hard stop.
        if net_pct <= ns["HARD_STOP_NET"]:
            ns["execute_paper_exit"](
                mint, position, current_price, remaining_size,
                f"hard stop {ns['HARD_STOP_NET']*100:.1f}% net",
            )
            continue

        age_min = max(
            0.0,
            (time.time() - position.get("opened_at", time.time())) / 60.0,
        )

        # 5) Fail-fast: a momentum entry that never produces momentum is suspect.
        if (
            age_min >= FAIL_FAST_MIN
            and float(position.get("peak_net_pct", -999.0)) < FAIL_FAST_PEAK_MAX_NET
            and net_pct <= FAIL_FAST_EXIT_NET
        ):
            ns["execute_paper_exit"](
                mint, position, current_price, remaining_size,
                f"failed momentum after {age_min:.1f}m at {net_pct*100:+.1f}% net",
            )
            continue

        # 6) Time stop for dead money.
        if age_min >= ns["TIME_STOP_MIN"] and net_pct <= ns["TIME_STOP_MAX_NET"]:
            ns["execute_paper_exit"](
                mint, position, current_price, remaining_size,
                f"time stop after {age_min:.0f}m at {net_pct*100:+.1f}% net",
            )
            continue

        initial_size = float(position.get("initial_size_sol") or remaining_size)

        # 7) Bank profits aggressively. Fractions are of original position.
        if not position.get("tp1_done") and net_pct >= ns["TP1_NET"]:
            ns["execute_paper_exit"](
                mint, position, current_price,
                min(initial_size * ns["TP1_FRACTION"], position.get("size_sol", 0.0)),
                f"TP1 at {net_pct*100:.1f}% net",
            )
            position["tp1_done"] = True
            position["breakeven_armed"] = True

        if mint not in ns["open_positions"]:
            continue

        if not position.get("tp2_done") and net_pct >= ns["TP2_NET"]:
            ns["execute_paper_exit"](
                mint, position, current_price,
                min(initial_size * ns["TP2_FRACTION"], position.get("size_sol", 0.0)),
                f"TP2 at {net_pct*100:.1f}% net",
            )
            position["tp2_done"] = True

        if mint not in ns["open_positions"]:
            continue

        if not position.get("tp3_done") and net_pct >= ns["TP3_NET"]:
            ns["execute_paper_exit"](
                mint, position, current_price,
                min(initial_size * ns["TP3_FRACTION"], position.get("size_sol", 0.0)),
                f"TP3 at {net_pct*100:.1f}% net",
            )
            position["tp3_done"] = True
            position["runner_active"] = True
            position["peak_price"] = max(
                float(position.get("peak_price") or current_price),
                current_price,
            )

        if mint not in ns["open_positions"]:
            continue

        # Final 10% runner. Post-TP net floor above remains the primary protection.
        if position.get("runner_active"):
            peak_price = max(float(position.get("peak_price") or current_price), current_price)
            position["peak_price"] = peak_price
            trail_price = peak_price * (1.0 - ns["RUNNER_TRAIL"])
            if current_price <= trail_price:
                ns["execute_paper_exit"](
                    mint, position, current_price, position.get("size_sol", 0.0),
                    f"{ns['RUNNER_TRAIL']*100:.0f}% runner trailing stop",
                )


# Patch only the paper exit function. All real trading remains disabled.
ns["check_exits"] = check_exits_v52

# Keep Strategy_v3 strict_paper_scan. Do NOT use a direct force-fill scanner.
ux = os.getenv("PAPER_UX_OVERLAY", "")
if ux:
    exec(ux, {"d": d, "v": v, "ns": ns})

v["save_state"]()

cid_raw = os.getenv("OWNER_TELEGRAM_CHAT_ID", "").strip()
if cid_raw:
    cid = int(cid_raw)
    ns["known_chats"].add(cid)
    ns["live_feed_chats"].add(cid)
    ns["send_message"](
        cid,
        "🧠 PROFIT V5.2 PAPER ACTIVE\n"
        "REAL trading: HARD LOCKED\n"
        "Size: 0.05 SOL simulated | 1 position max\n"
        "3-scan confirmation | accel 1.50x+ | 5m move 2.5%..10%\n"
        "Liquidity: $35k+ | 5m vol: $10k+ | 1h vol: $75k+\n"
        "Buy pressure: 20+ buys | buy/sell ratio 1.50+\n"
        "Stop: -6.5% net | TP: 7.5/13/22%\n"
        "TP1 banks 60%; remainder gets immediate dynamic profit protection\n"
        "Circuit: 4 losses or -3% bankroll/day\n"
        "PAPER ONLY."
    )

print(
    "PROFIT V5.2 PAPER READY "
    "real_enabled=%s mode=%s size=0.05 opens=1 "
    "liq=35000 vol5=10000 vol1=75000 buys5=20 ratio=1.50 "
    "confirm=3 accel=1.50 p5=2.5..10 stop=-6.5 tp=7.5/13/22 "
    "parts=60/20/10 runner=10 circuit=4 daily=3%%"
    % (v["LIVE_ENABLED"], v["mode"]),
    flush=True,
)

d["main_v3"]()
