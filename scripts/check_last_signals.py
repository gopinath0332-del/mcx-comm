"""
scripts/check_last_signals.py
=============================
Fetches recent 1H historical candles for the active MCX Gold contract,
runs the exact strategy logic, and reports the latest signals generated.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time, timedelta

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from config.settings import get_settings
from src.candle_builder import HeikinAshiConverter, stitch_back_adjusted_candles
from src.contract_selector import ContractSelector
from src.indicators import apply_all
from src.kite_auth import build_authenticated_kite
from src.lot_sizer import LotSizer
from src.signal_engine import SignalEngine

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


def main():
    settings = get_settings()
    print("=" * 60)
    print(f"MCX Gold Strategy Back-scanner: {settings.strategy_name}")
    print(f"Capital: ₹{settings.capital:,.0f} | Risk: {settings.base_risk_pct}%")
    print("=" * 60)

    # 1. Connect to Kite
    try:
        kite = build_authenticated_kite(settings.kite_api_key, settings.kite_access_token)
        print(" Connected to Kite successfully.")
    except Exception as exc:
        print(f"❌ Failed to authenticate Kite: {exc}")
        return

    # 2. Select execution & signal contracts
    exec_selector = ContractSelector(instrument_prefix=settings.instrument_prefix)
    sig_selector = ContractSelector(instrument_prefix=settings.effective_signal_prefix)
    try:
        exec_symbol, _ = exec_selector.get_active_contract(
            kite, tender_cutoff_days=settings.tender_cutoff_days
        )
        symbol, token = sig_selector.get_active_contract(
            kite, tender_cutoff_days=settings.tender_cutoff_days
        )
        print(f" Execution Contract: {exec_symbol} | Signal Contract: {symbol} (Token: {token})")
    except Exception as exc:
        print(f"❌ Failed to resolve active contract: {exc}")
        return

    # 3. Fetch historical 60minute candles (with 2-contract B-ADJ stitch if back-month available)
    now_ist = datetime.now(tz=IST)
    from_date = now_ist.date() - timedelta(days=90)
    print(f" Fetching 60minute candles from {from_date} to {now_ist.date()}...")

    try:
        records = kite.historical_data(
            instrument_token=token,
            from_date=from_date,
            to_date=now_ist,
            interval="60minute",
        )
    except Exception as exc:
        print(f"❌ Failed to fetch historical data: {exc}")
        return

    back_info = sig_selector.get_back_month_contract(kite, symbol)
    if back_info and records:
        back_sym, back_tok, back_exp = back_info
        rollover_dt = datetime.combine(
            back_exp - timedelta(days=settings.tender_period_days),
            time(0, 0),
            tzinfo=IST,
        )
        try:
            back_records = kite.historical_data(
                instrument_token=back_tok,
                from_date=from_date,
                to_date=now_ist,
                interval="60minute",
            )
            if back_records:
                records, offset = stitch_back_adjusted_candles(back_records, records, rollover_dt)
                print(
                    f" Stitched B-ADJ continuous feed: {back_sym} -> {symbol} "
                    f"(rollover={rollover_dt.strftime('%Y-%m-%d')}, offset={offset:+.2f})"
                )
        except Exception as exc:
            print(f"⚠️ Could not fetch back-month {back_sym}: {exc}")

    if not records:
        print("❌ No historical candles returned from Kite.")
        return

    print(f" Loaded {len(records)} raw 1H candles.")

    # 4. Build DataFrame & compute Heikin-Ashi + Indicators
    df = pd.DataFrame(records)
    # Ensure datetime with IST tz
    df["timestamp"] = pd.to_datetime(df["date"])
    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("UTC").dt.tz_convert(IST)
    else:
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)

    df = HeikinAshiConverter.convert(df)
    df = apply_all(df, settings)

    # 5. Run bar-by-bar through SignalEngine
    engine = SignalEngine(settings)
    lot_sizer = LotSizer(
        capital=settings.capital,
        base_risk_pct=settings.base_risk_pct,
        lot_multiplier=settings.lot_multiplier,
    )

    signals = []

    for idx, row in df.iterrows():
        # Skip if warmup NaN
        if pd.isna(row.get("ha_close")) or pd.isna(row.get("upper")) or pd.isna(row.get("lower")) or pd.isna(row.get("ema_trend")) or pd.isna(row.get("atr")):
            continue

        candle_time = row["timestamp"]
        res = engine.evaluate(row, candle_time)

        if res.action != "NONE":
            lots = lot_sizer.calculate(res.atr) if res.atr > 0 else 1
            signals.append({
                "time": candle_time,
                "action": res.action,
                "ha_close": row["ha_close"],
                "raw_close": row["close"],
                "upper": row["upper"],
                "lower": row["lower"],
                "ema200": row["ema_trend"],
                "atr": res.atr,
                "trail_sl": res.trail_sl,
                "lots": lots,
                "reason": res.exit_reason,
            })

    print(f"\n Total signals generated in lookback: {len(signals)}\n")

    if not signals:
        print("No signals triggered in the fetched historical window.")
        last_row = df.iloc[-1]
        print("\nLatest Bar Status:")
        print(f"  Time: {last_row['timestamp']}")
        print(f"  HA Close: ₹{last_row['ha_close']:.2f} (Raw Close: ₹{last_row['close']:.2f})")
        print(f"  Upper Channel: ₹{last_row['upper']:.2f}")
        print(f"  Lower Channel: ₹{last_row['lower']:.2f}")
        print(f"  EMA 200: ₹{last_row['ema_trend']:.2f}")
        print(f"  ATR (20): ₹{last_row['atr']:.2f}")
        print(f"  Current Position: {engine.position or 'FLAT'}")
        return

    # Print all signals
    print("--- All Signals (Chronological) ---")
    for s in signals:
        time_str = s['time'].strftime('%Y-%m-%d %H:%M IST')
        if s['action'] in ('BUY', 'SELL'):
            trail_str = f"Trail SL: ₹{s['trail_sl']:.2f}" if s['trail_sl'] else ""
            print(f"[{time_str}] 👉 {s['action']} at HA C=₹{s['ha_close']:.2f} (Raw C=₹{s['raw_close']:.2f}) | {s['lots']} lots | ATR=₹{s['atr']:.2f} | {trail_str}")
        else:
            print(f"[{time_str}] 🛑 {s['action']} at HA C=₹{s['ha_close']:.2f} (Raw C=₹{s['raw_close']:.2f}) | Reason: {s['reason']}")

    # Print past 4 trades (paired entry/exit) for easy TradingView cross-check
    trades = []
    open_trade = None
    for s in signals:
        if s["action"] in ("BUY", "SELL"):
            open_trade = {
                "dir": "LONG" if s["action"] == "BUY" else "SHORT",
                "entry_time": s["time"],
                "entry_price": s["ha_close"],
                "lots": s["lots"],
            }
        elif s["action"] in ("EXIT_BUY", "EXIT_SELL") and open_trade is not None:
            open_trade["exit_time"] = s["time"]
            open_trade["exit_price"] = s["ha_close"]
            open_trade["reason"] = s["reason"]
            trades.append(open_trade)
            open_trade = None
    if open_trade is not None:
        open_trade["exit_time"] = None
        open_trade["exit_price"] = None
        open_trade["reason"] = "OPEN"
        trades.append(open_trade)

    print("\n--- Past 4 Trades (Entry / Exit) ---")
    for i, t in enumerate(trades[-4:], 1):
        ent = t["entry_time"].strftime("%Y-%m-%d %H:%M IST")
        if t["exit_time"]:
            ext = t["exit_time"].strftime("%Y-%m-%d %H:%M IST")
            print(
                f"{i}. {t['dir']:5s} | Entry: {ent} @ ₹{t['entry_price']:.2f} "
                f"| Exit: {ext} @ ₹{t['exit_price']:.2f} ({t['reason']})"
            )
        else:
            print(
                f"{i}. {t['dir']:5s} | Entry: {ent} @ ₹{t['entry_price']:.2f} | Exit: OPEN"
            )

    print("\n" + "=" * 60)
    last_sig = signals[-1]
    print(f"🚨 LAST SIGNAL GENERATED:")
    print(f"  Action:      {last_sig['action']}")
    print(f"  Date & Time: {last_sig['time'].strftime('%Y-%m-%d %H:%M IST')}")
    print(f"  HA Close:    ₹{last_sig['ha_close']:.2f}")
    print(f"  Raw Close:   ₹{last_sig['raw_close']:.2f}")
    print(f"  EMA(200):    ₹{last_sig['ema200']:.2f}")
    print(f"  Upper:       ₹{last_sig['upper']:.2f} | Lower: ₹{last_sig['lower']:.2f}")
    print(f"  ATR(20):     ₹{last_sig['atr']:.2f}")
    if last_sig['action'] in ('BUY', 'SELL'):
        print(f"  Lots:        {last_sig['lots']}")
        print(f"  Trail SL:    ₹{last_sig['trail_sl']:.2f}" if last_sig['trail_sl'] else "  Trail SL: N/A")
    else:
        print(f"  Exit Reason: {last_sig['reason']}")
    print(f"  Current State at end of data: {engine.position or 'FLAT'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
