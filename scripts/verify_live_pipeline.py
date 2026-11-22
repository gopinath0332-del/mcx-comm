"""
scripts/verify_live_pipeline.py
===============================
Tests the full REST-based pipeline against live Zerodha Kite Connect endpoints.
Run this script whenever you have a fresh daily Zerodha access token.
"""

from __future__ import annotations

import datetime
import os
import sys

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings
from src.contract_selector import ContractSelector
from src.kite_auth import build_authenticated_kite
from src.session_manager import SessionManager

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")


def main() -> None:
    settings = get_settings()
    print("=" * 60)
    print("MCX Gold Engine — Live REST API Verification")
    print("=" * 60)

    try:
        kite = build_authenticated_kite(settings.kite_api_key, settings.kite_access_token)
    except Exception as exc:
        print(f"❌ Failed to initialize KiteConnect: {exc}")
        return

    print("\n1. Testing Kite Profile & Authentication...")
    try:
        profile = kite.profile()
        print(f"   ✅ Connected successfully! User: {profile.get('user_name')} (ID: {profile.get('user_id')})")
    except Exception as exc:
        print(f"   ❌ Authentication failed: {exc}")
        print("\n   ⚠️ Zerodha access tokens expire daily at midnight.")
        print("   Please refresh your token via your morning login flow or scripts/refresh_token.py.")
        return

    print("\n2. Testing Contract Selector (MCX Instruments Master)...")
    try:
        cs = ContractSelector(instrument_prefix=settings.instrument_prefix, exchange="MCX")
        symbol, token = cs.get_active_contract(kite)
        print(f"   ✅ Active contract selected: {symbol} (token={token})")
    except Exception as exc:
        print(f"   ❌ Contract selection failed: {exc}")
        return

    print("\n3. Testing Live LTP Fetch (REST)...")
    try:
        res = kite.ltp([f"MCX:{symbol}"])
        ltp = res.get(f"MCX:{symbol}", {}).get("last_price")
        print(f"   ✅ Live LTP for MCX:{symbol}: ₹{ltp}")
    except Exception as exc:
        print(f"   ❌ Live LTP fetch failed: {exc}")
        return

    print("\n4. Testing 60-Minute Historical Candle Fetch (REST)...")
    try:
        now_ist = datetime.datetime.now(tz=IST)
        from_dt = now_ist.date() - datetime.timedelta(days=15)
        records = kite.historical_data(
            instrument_token=token,
            from_date=from_dt,
            to_date=now_ist,
            interval="60minute",
        )
        print(f"   ✅ Fetched {len(records)} historical 1H candles from Zerodha.")
        if records:
            last_rec = records[-1]
            print(f"   Latest Candle: {last_rec['date']} | O={last_rec['open']} H={last_rec['high']} L={last_rec['low']} C={last_rec['close']}")
    except Exception as exc:
        print(f"   ❌ Historical candle fetch failed: {exc}")
        return

    print("\n5. Testing SessionManager Pre-seed & Indicator Computation...")
    try:
        sm = SessionManager(settings=settings, kite_client=kite)
        sm._active_symbol = symbol
        sm._active_token = token
        loaded = sm._preseed_historical_candles()
        print(f"   ✅ Pre-seeded {loaded} candles into buffer.")
        print(f"   Last processed candle time: {sm._last_processed_candle_time}")
        print(f"   Last LTP recorded: {sm._last_ltp}")
    except Exception as exc:
        print(f"   ❌ Pre-seed failed: {exc}")
        return

    print("\n6. Testing Scheduled Candle Polling Check...")
    try:
        new_count = sm._check_and_process_new_candles()
        print(f"   ✅ Polling check executed without errors. Newly closed candles: {new_count}")
    except Exception as exc:
        print(f"   ❌ Polling check failed: {exc}")
        return

    print("\n7. Testing Heikin-Ashi, Indicators & Signal Engine on Live Data...")
    try:
        import pandas as pd
        from src.candle_builder import HeikinAshiConverter
        from src.indicators import apply_all

        df = pd.DataFrame(list(sm._candle_buffer))
        df = HeikinAshiConverter.convert(df)
        df = apply_all(df, settings)
        last_row = df.iloc[-1]

        sig = sm._signal_engine.evaluate(last_row, last_row["timestamp"])
        print(f"   ✅ Heikin-Ashi Close: ₹{last_row['ha_close']:.2f}")
        print(f"   ✅ Donchian Channels : Upper(20)=₹{last_row['upper']:.2f} | Lower(10)=₹{last_row['lower']:.2f}")
        print(f"   ✅ Trend & Volatility: EMA(200)=₹{last_row['ema_trend']:.2f} | ATR(20)=₹{last_row['atr']:.2f}")
        print(f"   ✅ Signal Engine Eval: Action={sig.action} | Reason={sig.exit_reason or 'None'}")
    except Exception as exc:
        print(f"   ❌ Indicator/Signal evaluation failed: {exc}")
        return

    print("\n" + "=" * 60)
    print("🎉 ALL LIVE REST API CHECKS COMPLETED SUCCESSFULLY!")
    print("=" * 60)


if __name__ == "__main__":
    main()
