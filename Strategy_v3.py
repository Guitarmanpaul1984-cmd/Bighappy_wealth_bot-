import json
import os
import time
import traceback
from pathlib import Path

# ---------------------------------------------------------
# BIG HAPPY WEALTH BOT — STRATEGY V3
# Quality-first paper validation strategy
# ---------------------------------------------------------

GATE = Path(__file__).with_name("Live_gate.py")

gate = {
    "__name__": "big_happy_gate",
    "__file__": str(GATE),
}

exec(
    compile(
        GATE.read_text(encoding="utf-8"),
        str(GATE),
        "exec",
    ),
    gate,
)

v2 = gate["v2"]
ns = gate["ns"]

STRATEGY_NAME = "V3 QUALITY MOMENTUM"

PAPER_RISK_PER_TRADE = float(
    os.getenv("PAPER_RISK_PER_TRADE_PCT", "0.25")
) / 100.0

MIN_ACCEL = float(
    os.getenv("PAPER_MIN_VOLUME_ACCEL", "1.00")
)

MIN_POSITIVE_5M = float(
    os.getenv("PAPER_MIN_5M_MOVE_PCT", "0.50")
)

MAX_5M_MOVE = float(
    os.getenv("PAPER_MAX_5M_MOVE_PCT", "15.0")
)

CONFIRM_SCANS = int(
    os.getenv("PAPER_CONFIRM_SCANS", "2")
)

DISCOVERY_SECONDS = int(
    os.getenv("V3_DISCOVERY_SECONDS", "30")
)

EXIT_SECONDS = int(
    os.getenv("V3_EXIT_SECONDS", "6")
)

POLL_SECONDS = int(
    os.getenv("V3_TELEGRAM_POLL_SECONDS", "5")
)

MIN_PROFIT_FACTOR = float(
    os.getenv("PAPER_MIN_PROFIT_FACTOR", "1.20")
)

quality_seen = {}

_original_base_scan = v2["base_scan"]


# ---------------------------------------------------------
# FRESH V3 VALIDATION
# ---------------------------------------------------------

gate["VALIDATION_PATH"] = Path(
    os.getenv(
        "PAPER_VALIDATION_PATH_V3",
        "/data/paper_validation_v3.json",
    )
)

gate["validation"] = []
gate["load_validation"]()


# ---------------------------------------------------------
# RISK-BASED PAPER POSITION SIZE
# ---------------------------------------------------------

def paper_quality_size():

    bankroll = max(
        0.0,
        float(ns["paper_bankroll_sol"]()),
    )

    available = max(
        0.0,
        float(ns["available_paper_balance"]()),
    )

    stop = abs(
        float(v2["p"]()["stop"])
    )

    if stop <= 0:
        return 0.0

    risk_sized = (
        bankroll
        * PAPER_RISK_PER_TRADE
        / stop
    )

    requested = float(
        ns.get("POSITION_SOL") or 0.0
    )

    return max(
        0.0,
        min(
            requested,
            risk_sized,
            available,
        ),
    )


# ---------------------------------------------------------
# TELEGRAM ANNOUNCEMENT
# ---------------------------------------------------------

def announce(text):

    try:
        ns["_announce_trade_event"](text)

    except Exception:

        for cid in list(
            ns.get("known_chats") or []
        ):
            try:
                ns["send_message"](
                    cid,
                    text,
                )
            except Exception:
                pass


# ---------------------------------------------------------
# OPEN QUALITY PAPER POSITION
# ---------------------------------------------------------

def open_quality_paper(item, detail):

    mint = item["mint"]
    pair = item["pair"]
    stats = detail["stats"]

    size = paper_quality_size()

    minimum = float(
        ns.get("MIN_PAPER_TRADE_SOL")
        or 0.01
    )

    if size < minimum:
        return False

    try:
        price = float(
            pair.get("priceNative") or 0
        )

        liquidity = float(
            (
                pair.get("liquidity")
                or {}
            ).get("usd") or 0
        )

    except Exception:
        return False

    if price <= 0 or liquidity <= 0:
        return False

    symbol = item.get(
        "symbol",
        "?",
    )

    ns["open_positions"][mint] = {

        "symbol": symbol,

        "pair_address":
            pair.get("pairAddress"),

        "entry_price":
            price,

        "size_sol":
            size,

        "initial_size_sol":
            size,

        "entry_liquidity_usd":
            liquidity,

        "target_price":
            price
            * ns["TARGET_MULTIPLIER"],

        "opened_at":
            time.time(),

        "peak_price":
            price,

        "peak_net_pct":
            -999.0,

        "realized_pnl_sol":
            0.0,

        "breakeven_armed":
            False,

        "tp1_done":
            False,

        "tp2_done":
            False,

        "tp3_done":
            False,

        "runner_active":
            False,

        "strategy":
            STRATEGY_NAME,

        "entry_v5":
            stats["v5"],

        "entry_v1":
            stats["v1"],

        "entry_buys":
            stats["buys"],

        "entry_sells":
            stats["sells"],

        "entry_ratio":
            stats["ratio"],

        "entry_p5":
            stats["p5"],

        "entry_age":
            stats["age"],

        "entry_accel":
            stats["accel"],
    }

    announce(

        "🧪 V3 QUALITY PAPER ENTRY\n"

        f"{symbol} "
        f"({mint[:6]}…{mint[-4:]})\n"

        f"Size: "
        f"{size:.4f} SOL simulated\n"

        f"Liquidity: "
        f"${stats['liq']:,.0f}\n"

        f"Volume 5m: "
        f"${stats['v5']:,.0f}\n"

        f"Volume 1h: "
        f"${stats['v1']:,.0f}\n"

        f"Flow: "
        f"{stats['buys']} buys / "
        f"{stats['sells']} sells "
        f"({stats['ratio']:.2f}x)\n"

        f"5m move: "
        f"{stats['p5']:+.1f}%\n"

        f"Volume acceleration: "
        f"{stats['accel']:.2f}x\n"

        f"Risk/trade: "
        f"{PAPER_RISK_PER_TRADE*100:.2f}% "
        "of bankroll\n"

        "REAL order: OFF"
    )

    return True


# ---------------------------------------------------------
# V3 STRICT PAPER SCANNER
# ---------------------------------------------------------

def strict_paper_scan():

    saved_available = (
        ns["available_paper_balance"]
    )

    saved_test = (
        ns.get("paper_test_mode")
    )

    saved_auto = (
        ns.get("auto_trade_paper")
    )

    try:

        # Block the old paper engine
        # from automatically opening trades.
        ns["available_paper_balance"] = (
            lambda: 0.0
        )

        ns["paper_test_mode"] = False
        ns["auto_trade_paper"] = False

        # Still use the existing scanner
        # for discovery and exit management.
        out = _original_base_scan()

    finally:

        ns["available_paper_balance"] = (
            saved_available
        )

        ns["paper_test_mode"] = (
            saved_test
        )

        ns["auto_trade_paper"] = (
            saved_auto
        )

    gate["sync_validation"]()

    if v2.get("mode") != "paper":
        return out

    if ns.get("risk_halt_reason"):
        return out

    # One position at a time while validating.
    if len(
        ns.get("open_positions")
        or {}
    ) >= 1:
        return out

    now = time.time()

    passed = []

    for item in (
        ns.get("candidates")
        or []
    ):

        try:

            ok, detail = (
                v2["candidate_ok"](item)
            )

        except Exception:
            continue

        if not ok:
            continue

        stats = detail["stats"]

        # Require accelerating volume.
        if (
            stats["accel"]
            < MIN_ACCEL
        ):
            continue

        # Require positive momentum.
        if (
            stats["p5"]
            < MIN_POSITIVE_5M
        ):
            continue

        # Never chase a candle that
        # has already run too far.
        max_allowed_move = min(
            MAX_5M_MOVE,
            v2["p"]()["max_chase"],
        )

        if (
            stats["p5"]
            > max_allowed_move
        ):
            continue

        # Require profile buy pressure.
        if (
            stats["ratio"]
            < v2["p"]()["ratio"]
        ):
            continue

        rec = quality_seen.get(
            item["mint"]
        )

        if (
            rec
            and now
            - rec.get("last", 0)
            <= max(
                90,
                DISCOVERY_SECONDS * 3,
            )
        ):

            count = (
                int(
                    rec.get(
                        "count",
                        0,
                    )
                )
                + 1
            )

        else:

            count = 1

        quality_seen[item["mint"]] = {
            "count": count,
            "last": now,
        }

        # Must remain qualified across
        # multiple consecutive scans.
        if count < CONFIRM_SCANS:
            continue

        score = (
            v2["volume_score"](item)
        )

        passed.append(
            (
                score,
                item,
                detail,
            )
        )

    # Remove stale confirmations.
    for mint in list(
        quality_seen
    ):

        if (
            now
            - quality_seen[
                mint
            ].get("last", 0)
            > max(
                180,
                DISCOVERY_SECONDS * 5,
            )
        ):

            quality_seen.pop(
                mint,
                None,
            )

    if not passed:
        return out

    # Strongest candidate only.
    passed.sort(
        key=lambda row: row[0],
        reverse=True,
    )

    _, item, detail = passed[0]

    open_quality_paper(
        item,
        detail,
    )

    return out


# IMPORTANT:
# dual_scan() resolves base_scan
# through the live_overlay_v2 namespace.
v2["base_scan"] = strict_paper_scan


# ---------------------------------------------------------
# V3 VALIDATION
#
# REAL unlock requires ALL:
# 20 completed trades
# >=70% win rate
# positive net P&L
# profit factor >=1.20
# ---------------------------------------------------------

def v3_validation_stats():

    sample = (
        gate["validation"]
        [-gate["WINDOW"]:]
    )

    total = len(sample)

    wins = sum(
        1
        for x in sample
        if float(
            x.get("pnl")
            or 0
        ) > 0
    )

    rate = (
        wins / total
        if total
        else 0.0
    )

    net = sum(
        float(
            x.get("pnl")
            or 0
        )
        for x in sample
    )

    gross_win = sum(
        max(
            0.0,
            float(
                x.get("pnl")
                or 0
            ),
        )
        for x in sample
    )

    gross_loss = abs(
        sum(
            min(
                0.0,
                float(
                    x.get("pnl")
                    or 0
                ),
            )
            for x in sample
        )
    )

    if gross_loss > 0:

        pf = (
            gross_win
            / gross_loss
        )

    elif gross_win > 0:

        pf = 999.0

    else:

        pf = 0.0

    passed = (

        total >= gate["WINDOW"]

        and rate >= gate["TARGET"]

        and net > 0

        and pf >= MIN_PROFIT_FACTOR
    )

    gate["v3_last_net"] = net
    gate["v3_last_pf"] = pf

    return (
        total,
        wins,
        rate,
        passed,
    )


def v3_validation_text():

    (
        total,
        wins,
        rate,
        passed,
    ) = v3_validation_stats()

    net = float(
        gate.get(
            "v3_last_net"
        )
        or 0.0
    )

    pf = float(
        gate.get(
            "v3_last_pf"
        )
        or 0.0
    )

    pf_text = (
        "∞"
        if pf >= 999
        else f"{pf:.2f}"
    )

    if (
        total
        < gate["WINDOW"]
    ):

        return (

            f"Paper hit rate: "
            f"{rate*100:.1f}% "
            f"({wins}/{total})\n"

            f"Net P&L: "
            f"{net:+.4f} SOL\n"

            f"Profit factor: "
            f"{pf_text}\n"

            f"Validation: need "
            f"{gate['WINDOW']} "
            "completed V3 trades"
        )

    return (

        f"Paper hit rate: "
        f"{rate*100:.1f}% "
        f"({wins}/{total})\n"

        f"Net P&L: "
        f"{net:+.4f} SOL\n"

        f"Profit factor: "
        f"{pf_text}\n"

        f"Validation: "
        f"{'PASSED ✅' if passed else 'NOT PASSED ❌'}\n"

        f"Targets: "
        f"{gate['TARGET']*100:.0f}%+ wins, "
        "positive net P&L, "
        f"PF {MIN_PROFIT_FACTOR:.2f}+"
    )


# Live_gate functions resolve these
# dynamically, so /hitrate and
# /real on now use V3 validation.
gate["validation_stats"] = (
    v3_validation_stats
)

gate["validation_text"] = (
    v3_validation_text
)


# ---------------------------------------------------------
# STATUS
# ---------------------------------------------------------

_original_status = (
    v2["status_text"]
)


def v3_status():

    return (

        _original_status()

        + f"\nStrategy: "
        f"{STRATEGY_NAME}"

        + f"\nPaper entry: "
        f"{CONFIRM_SCANS}-scan "
        "confirmation"

        + f" | volume accel "
        f"{MIN_ACCEL:.2f}x+"

        + f"\n5m momentum: "
        f"{MIN_POSITIVE_5M:.1f}% "
        f"to {MAX_5M_MOVE:.1f}%"

        + f"\nPaper risk/trade: "
        f"{PAPER_RISK_PER_TRADE*100:.2f}% "
        "bankroll"

        + f"\nExit checks: "
        f"every {EXIT_SECONDS}s"

        + f"\nValidation: "
        "70%+ wins + positive P&L "
        f"+ PF {MIN_PROFIT_FACTOR:.2f}+"
    )


v2["status_text"] = v3_status


# ---------------------------------------------------------
# V3 MAIN LOOP
# ---------------------------------------------------------

def main_v3():

    ns["telegram"](
        "deleteWebhook",
        {
            "drop_pending_updates":
                "false"
        },
        timeout=15,
    )

    me = ns["telegram"](
        "getMe",
        timeout=15,
    )

    if (
        not me
        or not me.get("ok")
    ):
        raise SystemExit(
            "Telegram getMe failed"
        )

    username = (
        (
            me.get("result")
            or {}
        ).get(
            "username",
            "unknown",
        )
    )

    print(
        "STRATEGY V3 STARTUP "
        f"@{username} "
        f"strategy={STRATEGY_NAME} "
        f"discovery="
        f"{DISCOVERY_SECONDS}s "
        f"exits="
        f"{EXIT_SECONDS}s "
        f"validation="
        f"{gate['VALIDATION_PATH']}",
        flush=True,
    )

    last_scan = 0.0
    last_exit = 0.0

    while True:

        try:

            updates = (
                ns["telegram"](
                    "getUpdates",
                    {
                        "offset":
                            ns[
                                "update_offset"
                            ]
                            + 1,

                        "timeout":
                            POLL_SECONDS,

                        "allowed_updates":
                            json.dumps(
                                ["message"]
                            ),
                    },
                    timeout=
                        POLL_SECONDS
                        + 7,
                )
            )

            if (
                updates
                and updates.get("ok")
            ):

                for update in (
                    updates.get(
                        "result",
                        [],
                    )
                ):

                    ns[
                        "update_offset"
                    ] = update.get(
                        "update_id",
                        ns[
                            "update_offset"
                        ],
                    )

                    message = (
                        update.get(
                            "message"
                        )
                        or {}
                    )

                    chat_id = (
                        (
                            message.get(
                                "chat"
                            )
                            or {}
                        ).get("id")
                    )

                    if (
                        "text" in message
                        and chat_id
                    ):

                        ns[
                            "handle_message"
                        ](
                            chat_id,
                            message[
                                "text"
                            ],
                        )

            now = time.time()

            # -------------------------
            # FAST EXIT MONITORING
            # -------------------------

            if (
                now - last_exit
                >= EXIT_SECONDS
            ):

                if (
                    v2.get("mode")
                    == "paper"

                    and (
                        ns.get(
                            "open_positions"
                        )
                        or {}
                    )
                ):

                    ns[
                        "check_exits"
                    ]()

                    gate[
                        "sync_validation"
                    ]()

                elif (
                    v2.get("positions")
                ):

                    # Existing REAL
                    # positions remain
                    # protected even if
                    # real entries are off.
                    v2["exits"]()

                last_exit = now

            # -------------------------
            # DISCOVERY SCAN
            # -------------------------

            if (

                ns.get(
                    "auto_scan_enabled"
                )

                and now - last_scan
                >= DISCOVERY_SECONDS
            ):

                try:

                    ns[
                        "run_scan"
                    ]()

                except Exception as e:

                    print(
                        "V3 SCAN ERROR "
                        f"{type(e).__name__}: "
                        f"{e}",
                        flush=True,
                    )

                    traceback.print_exc()

                last_scan = now

        except Exception as e:

            print(
                "V3 MAIN LOOP ERROR "
                f"{type(e).__name__}: "
                f"{e}",
                flush=True,
            )

            traceback.print_exc()

            time.sleep(2)


# ---------------------------------------------------------
# STARTUP
# ---------------------------------------------------------

print(
    "V3 QUALITY STRATEGY READY "
    f"risk="
    f"{PAPER_RISK_PER_TRADE*100:.2f}% "
    f"confirm="
    f"{CONFIRM_SCANS} "
    f"accel="
    f"{MIN_ACCEL:.2f}x "
    f"5m="
    f"{MIN_POSITIVE_5M:.1f}%.."
    f"{MAX_5M_MOVE:.1f}% "
    f"PF="
    f"{MIN_PROFIT_FACTOR:.2f} "
    f"validation="
    f"{gate['VALIDATION_PATH']}",
    flush=True,
)


if __name__ == "__main__":
    main_v3()
