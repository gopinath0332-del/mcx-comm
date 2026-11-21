"""
scripts/check_last_signals.py
=============================
Fetches recent 1H historical candles for the active MCX Gold contract,
runs the exact strategy logic, and reports the latest signals generated.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
from config.settings import get_settings
from src.candle_builder import HeikinAshiConverter
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

    # 2. Select active contract
    selector = ContractSelector(instrument_prefix=settings.instrument_prefix)
    try:
        symbol, token = selector.get_active_contract(kite)
        print(f" Active Contract: {symbol} (Token: {token})")
    except Exception as exc:
        print(f"❌ Failed to resolve active contract: {exc}")
        return

    # 3. Fetch historical 60minute candles
    now_ist = datetime.now(tz=IST)
    from_date = now_ist.date() - timedelta(days=60)
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

    # Print the last 5 signals
    print("--- Recent Signals ---")
    for s in signals[-5:]:
        time_str = s['time'].strftime('%Y-%m-%d %H:%M IST')
        if s['action'] in ('BUY', 'SELL'):
            trail_str = f"Trail SL: ₹{s['trail_sl']:.2f}" if s['trail_sl'] else ""
            print(f"[{time_str}] 👉 {s['action']} at HA C=₹{s['ha_close']:.2f} (Raw C=₹{s['raw_close']:.2f}) | {s['lots']} lots | ATR=₹{s['atr']:.2f} | {trail_str}")
        else:
            print(f"[{time_str}] 🛑 {s['action']} at HA C=₹{s['ha_close']:.2f} (Raw C=₹{s['raw_close']:.2f}) | Reason: {s['reason']}")

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
