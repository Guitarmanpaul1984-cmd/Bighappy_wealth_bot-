import json, os, time
from pathlib import Path

V2 = Path(__file__).with_name("live_overlay_v2.py")
v2 = {"__name__": "big_happy_v2", "__file__": str(V2)}
exec(compile(V2.read_text(encoding="utf-8"), str(V2), "exec"), v2)

ns = v2["ns"]

TARGET = 0.70
WINDOW = 20
VALIDATION_PATH = Path("/data/paper_validation.json")
validation = []


def load_validation():
    global validation
    try:
        if VALIDATION_PATH.exists():
            data = json.loads(VALIDATION_PATH.read_text(encoding="utf-8"))
            validation = data.get("trades") or []
    except Exception as e:
        print(f"VALIDATION LOAD ERROR {type(e).__name__}: {e}", flush=True)
        validation = []


def save_validation():
    try:
        VALIDATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = VALIDATION_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"trades": validation[-100:], "saved_at": time.time()},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(tmp, VALIDATION_PATH)
    except Exception as e:
        print(f"VALIDATION SAVE ERROR {type(e).__name__}: {e}", flush=True)


def trade_key(p):
    return "|".join([
        str(p.get("pair_address") or ""),
        str(p.get("opened_at") or ""),
        str(p.get("closed_at") or ""),
        str(p.get("symbol") or "?"),
        f"{float(p.get('pnl_sol') or 0.0):.12f}",
    ])


def sync_validation():
    known = {str(x.get("key") or "") for x in validation}
    added = 0

    for p in ns.get("closed_positions") or []:
        key = trade_key(p)
        if key in known:
            continue

        pnl = float(p.get("pnl_sol") or 0.0)
        validation.append({
            "key": key,
            "win": pnl > 0,
            "pnl": pnl,
            "symbol": str(p.get("symbol") or "?"),
            "closed_at": float(p.get("closed_at") or time.time()),
        })
        known.add(key)
        added += 1

    if added:
        del validation[:-100]
        save_validation()
        print(f"PAPER VALIDATION +{added} total={len(validation)}", flush=True)


def validation_stats():
    sample = validation[-WINDOW:]
    total = len(sample)
    wins = sum(1 for x in sample if x.get("win"))
    rate = wins / total if total else 0.0
    passed = total >= WINDOW and rate >= TARGET
    return total, wins, rate, passed


def validation_text():
    total, wins, rate, passed = validation_stats()

    if total < WINDOW:
        return (
            f"Paper hit rate: {rate*100:.1f}% ({wins}/{total})\n"
            f"Validation: need {WINDOW} completed PAPER trades at "
            f"{TARGET*100:.0f}%+"
        )

    return (
        f"Paper hit rate: {rate*100:.1f}% ({wins}/{total})\n"
        f"Validation: {'PASSED ✅' if passed else 'NOT PASSED ❌'} "
        f"(target {TARGET*100:.0f}%+)"
    )


load_validation()

# Track completed PAPER trades after scanner cycles.
_original_scan = ns["run_scan"]

def gated_scan():
    out = _original_scan()
    sync_validation()
    return out

ns["run_scan"] = gated_scan


# Add hit-rate validation to /status.
_original_status = v2["status_text"]

def gated_status():
    sync_validation()
    return _original_status() + "\n" + validation_text()

v2["status_text"] = gated_status


# REAL mode requires at least 14 wins from the last 20 PAPER trades.
_original_set_real = v2["set_real"]

def gated_set_real(cid):
    sync_validation()
    total, wins, rate, passed = validation_stats()

    if not passed:
        if total < WINDOW:
            msg = (
                "🔒 REAL MODE LOCKED\n"
                f"Need {WINDOW} completed PAPER trades first.\n"
                f"Current: {wins}/{total} wins ({rate*100:.1f}%).\n"
                f"Target: {TARGET*100:.0f}%+."
            )
        else:
            msg = (
                "🔒 REAL MODE LOCKED\n"
                f"Paper hit rate: {rate*100:.1f}% ({wins}/{total}).\n"
                f"Target: {TARGET*100:.0f}%+ over the last {WINDOW} trades."
            )

        ns["send_message"](cid, msg)
        return

    _original_set_real(cid)

v2["set_real"] = gated_set_real


# Add /hitrate Telegram command.
_original_handle = ns["handle_message"]

def gated_handle(cid, text):
    cmd = (
        (text or "").strip().lower().split()[0]
        if (text or "").strip()
        else ""
    )

    if cmd in {"/hitrate", "/validation"}:
        sync_validation()
        ns["send_message"](
            cid,
            "🎯 PAPER VALIDATION\n" + validation_text(),
        )
        return

    _original_handle(cid, text)

ns["handle_message"] = gated_handle


print(
    f"70% HIT-RATE GATE READY target={TARGET*100:.0f}% "
    f"window={WINDOW} saved={len(validation)} "
    f"mode={v2.get('mode')}",
    flush=True,
)


if __name__ == "__main__":
    ns["main"]()
