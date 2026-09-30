import os
import runpy

# BIG HAPPY WEALTH BOT — PROFIT V5.1 PAPER
# PAPER-ONLY experiment. Refuses to start if live trading is enabled.

if os.getenv("LIVE_TRADING_ENABLED", "0").strip().lower() in {"1", "true", "on", "yes"}:
    raise SystemExit("PROFIT V5.1 PAPER REFUSES TO START: LIVE_TRADING_ENABLED must be 0.")

# Fresh V5 validation and dashboard ledgers.
os.environ["PAPER_VALIDATION_PATH_V3"] = "/data/paper_validation_profit_v5.json"
os.environ["PAPER_UX_LEDGER_PATH"] = "/data/paper_ux_profit_v5.json"

# Stronger quality confirmation.
os.environ["PAPER_CONFIRM_SCANS"] = "2"
os.environ["PAPER_MIN_VOLUME_ACCEL"] = "1.25"
os.environ["PAPER_MIN_5M_MOVE_PCT"] = "2.0"
os.environ["PAPER_MAX_5M_MOVE_PCT"] = "15.0"
os.environ["PAPER_MIN_PROFIT_FACTOR"] = "1.30"

# Simulated sizing + reduced API pressure.
os.environ["PAPER_POSITION_SOL"] = "0.05"
os.environ["PAPER_RISK_PER_TRADE_PCT"] = "0.30"
os.environ["V3_DISCOVERY_SECONDS"] = "20"
os.environ["V3_EXIT_SECONDS"] = "2"
os.environ["V3_TELEGRAM_POLL_SECONDS"] = "4"
os.environ["DISCOVERY_LIMIT"] = "8"

d = runpy.run_path("/app/Strategy_v3.py", run_name="big_happy_profit_v5_1")
v = d["v2"]
ns = d["ns"]

# Hard PAPER lock.
v["LIVE_ENABLED"] = False
v["mode"] = "paper"
ns["paper_test_mode"] = False
ns["auto_trade_paper"] = False
ns["POSITION_SOL"] = 0.05

# Quality-first candidate profile.
q = v["PROFILES"]["fast"]
q.update(
    label="PROFIT V5.1 PAPER",
    risk=35,
    liq=20000,
    age=2,
    market=20,
    vol1=50000,
    vol5=5000,
    buys5=12,
    ratio=1.25,
    max_chase=15,
    stop=-0.08,
    be=0.055,
    be_stop=0.0,
    tp=(0.08, 0.15, 0.25),
    parts=(0.40, 0.30, 0.20),
    trail=0.06,
    time=8,
    time_max=0.015,
    opens=1,
    slip=500,
    impact=0.05,
    wallet=0.05,
    priority=700000,
)

v["selected"] = "fast"
v["apply_profile"]()

# PAPER exit/risk settings.
ns["HARD_STOP_NET"] = -0.08
ns["BREAKEVEN_ARM_NET"] = 0.055
ns["BREAKEVEN_STOP_NET"] = 0.0
ns["TP1_NET"] = 0.08
ns["TP2_NET"] = 0.15
ns["TP3_NET"] = 0.25
ns["TP1_FRACTION"] = 0.40
ns["TP2_FRACTION"] = 0.30
ns["TP3_FRACTION"] = 0.20
ns["RUNNER_TRAIL"] = 0.06
ns["TIME_STOP_MIN"] = 8.0
ns["TIME_STOP_MAX_NET"] = 0.015
ns["MAX_OPEN"] = 1

# PAPER circuit breakers.
ns["MAX_CONSECUTIVE_LOSSES"] = 6
ns["DAILY_LOSS_LIMIT"] = 0.10
ns["risk_halt_reason"] = None
ns["risk_halt_day"] = None

# Keep Strategy_v3 strict_paper_scan. Do NOT swap back to V4's direct auto-fill scanner.

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
        "🧠 PROFIT V5.1 PAPER ACTIVE\n"
        "REAL trading: HARD LOCKED\n"
        "Size: 0.05 SOL simulated | 1 quality position max\n"
        "2-scan confirmation | accel 1.25x+ | 5m move 2%..15%\n"
        "Liquidity: $20k+ | 5m vol: $5k+ | 1h vol: $50k+\n"
        "Buy pressure: 12+ buys | buy/sell ratio 1.25+\n"
        "Discovery: 20s | candidate limit: 8 | exits: 2s\n"
        "Fresh V5 validation/P&L ledgers."
    )

print(
    "PROFIT V5.1 PAPER READY "
    "real_enabled=%s mode=%s size=0.05 opens=1 "
    "liq=20000 vol5=5000 vol1=50000 buys5=12 ratio=1.25 "
    "confirm=2 accel=1.25 p5=2..15 stop=-8 tp=8/15/25 "
    "discovery=20s discovery_limit=8"
    % (v["LIVE_ENABLED"], v["mode"]),
    flush=True,
)

d["main_v3"]()
