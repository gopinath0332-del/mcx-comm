"""
src/discord_notifier.py
========================
Rich Discord webhook embeds for all MCX Gold trade events.

Message types:
  🟢 BUY ENTRY       — symbol, qty, entry price, ATR, trailing SL
  🔴 SELL ENTRY      — same fields
  ✅ EXIT BUY/SELL   — reason, PnL estimate
  📊 Morning Briefing — contract, capital, margin, MCX open
  📈 EOD Summary      — trades, PnL, next session
  ⚠️ Error / Info     — operational alerts

All sends are non-blocking via an internal queue + background thread.
"""

from __future__ import annotations

import logging
import queue
import threading
import time as _time
from datetime import datetime
from typing import Any

try:
    import zoneinfo
    IST = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

import requests

logger = logging.getLogger(__name__)

# Embed colour constants
_COLOR = {
    "buy":    0x00C851,   # green
    "sell":   0xFF4444,   # red
    "exit_win":  0x10B981,  # emerald
    "exit_loss": 0xF43F5E,  # rose
    "eod":    0x33B5E5,   # blue
    "error":  0x8B0000,   # dark red
    "info":   0x888888,   # grey
    "warn":   0xFFBB33,   # amber
}

_MODE_LABEL = {True: "🔴 LIVE", False: "🟡 PAPER"}


class DiscordNotifier:
    """
    Formats and delivers Discord webhook embeds asynchronously.
    """

    def __init__(self, webhook_url: str, live_order_placement: bool = False) -> None:
        self._url  = webhook_url.strip() if webhook_url else ""
        self._live = live_order_placement
        self._q: queue.Queue[dict | None] = queue.Queue(maxsize=500)
        self._session = requests.Session()
        self._worker_thread: threading.Thread | None = None

        if self._url:
            self._worker_thread = threading.Thread(
                target=self._worker, name="DiscordWorker", daemon=True
            )
            self._worker_thread.start()

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    def buy_entry(
        self,
        symbol: str,
        entry_price: float,
        qty: int,
        atr: float,
        trail_sl: float | None,
        candle_time: datetime,
        sizing_desc: str,
        order_id: str,
        live: bool,
    ) -> None:
        tp_level = entry_price + atr * 4.0  # ATR TP Multiplier = 4.0
        fields = [
            ("Contract",    symbol,              True),
            ("Entry Price", f"₹{entry_price:.2f}", True),
            ("Qty (Lots)",  str(qty),            True),
            ("ATR (1H)",    f"₹{atr:.2f}",       True),
            ("Trailing SL", f"₹{trail_sl:.2f}" if trail_sl else "—", True),
            ("TP Level",    f"₹{tp_level:.2f}",  True),
            ("Candle Time", candle_time.strftime("%d %b %Y %H:%M IST"), False),
            ("Sizing",      sizing_desc,          False),
            ("Order ID",    order_id,             False),
        ]
        self._send_embed(
            title=f"🟢 BUY ENTRY — {symbol}",
            color=_COLOR["buy"],
            fields=fields,
            mode=live,
        )

    def sell_entry(
        self,
        symbol: str,
        entry_price: float,
        qty: int,
        atr: float,
        trail_sl: float | None,
        candle_time: datetime,
        sizing_desc: str,
        order_id: str,
        live: bool,
    ) -> None:
        tp_level = entry_price - atr * 4.0
        fields = [
            ("Contract",    symbol,              True),
            ("Entry Price", f"₹{entry_price:.2f}", True),
            ("Qty (Lots)",  str(qty),            True),
            ("ATR (1H)",    f"₹{atr:.2f}",       True),
            ("Trailing SL", f"₹{trail_sl:.2f}" if trail_sl else "—", True),
            ("TP Level",    f"₹{tp_level:.2f}",  True),
            ("Candle Time", candle_time.strftime("%d %b %Y %H:%M IST"), False),
            ("Sizing",      sizing_desc,          False),
            ("Order ID",    order_id,             False),
        ]
        self._send_embed(
            title=f"🔴 SELL ENTRY — {symbol}",
            color=_COLOR["sell"],
            fields=fields,
            mode=live,
        )

    def exit_trade(
        self,
        symbol: str,
        position: str,
        exit_price: float,
        entry_price: float,
        qty: int,
        reason: str,
        pnl_est: float,
        candle_time: datetime,
        order_id: str,
        live: bool,
    ) -> None:
        pnl_str = f"₹{pnl_est:+,.2f}" if pnl_est != 0 else "Estimating…"
        color   = _COLOR["exit_win"] if pnl_est >= 0 else _COLOR["exit_loss"]
        emoji   = "✅" if pnl_est >= 0 else "🔴"
        label   = "EXIT BUY" if position == "LONG" else "EXIT SELL"
        pnl_label = "Realized P&L" if live else "Est. P&L"
        fields  = [
            ("Contract",    symbol,              True),
            ("Exit Price",  f"₹{exit_price:.2f}" if exit_price > 0 else "Market", True),
            ("Entry Price", f"₹{entry_price:.2f}", True),
            ("Qty (Lots)",  str(qty),            True),
            ("Reason",      reason,              False),
            (pnl_label,     pnl_str,             True),
            ("Candle Time", candle_time.strftime("%d %b %Y %H:%M IST"), False),
            ("Order ID",    order_id,            False),
        ]
        self._send_embed(
            title=f"{emoji} {label} — {symbol}",
            color=color,
            fields=fields,
            mode=live,
        )

    def morning_briefing(
        self,
        contract: str,
        capital: float,
        available_cash: float | None,
        candle_interval: int,
        entry_cutoff: str,
        eod_exit: str,
        mode: str,
    ) -> None:
        fields = [
            ("Contract",       contract,                           False),
            ("Capital",        f"₹{capital:,.0f}",                True),
            ("Margin (Avail)", f"₹{available_cash:,.2f}" if available_cash else "N/A", True),
            ("Interval",       f"{candle_interval}H Heikin-Ashi",  True),
            ("Entry Cutoff",   f"{entry_cutoff} IST",              True),
            ("EOD Exit",       f"{eod_exit} IST",                  True),
            ("Mode",           mode,                               True),
        ]
        self._send_embed(
            title="📊 MCX Gold — Morning Briefing",
            color=_COLOR["info"],
            fields=fields,
            mode=self._live,
        )

    def eod_summary(
        self,
        date_str: str,
        trades_taken: int,
        candles_evaluated: int,
        next_session: str,
    ) -> None:
        fields = [
            ("Date",             date_str,          False),
            ("Trades Taken",     str(trades_taken), True),
            ("Candles Evaluated", str(candles_evaluated), True),
            ("Next Session",     next_session,      False),
        ]
        self._send_embed(
            title="📈 MCX Gold — EOD Summary",
            color=_COLOR["eod"],
            fields=fields,
            mode=self._live,
        )

    def error(self, message: str) -> None:
        self._send_embed(
            title="❌ MCX Engine Error",
            color=_COLOR["error"],
            fields=[("Details", message, False)],
            mode=self._live,
        )

    def warning(self, message: str) -> None:
        self._send_embed(
            title="⚠️ MCX Engine Warning",
            color=_COLOR["warn"],
            fields=[("Warning", message, False)],
            mode=self._live,
        )

    def info(self, message: str) -> None:
        self._send_embed(
            title="ℹ️ MCX Engine",
            color=_COLOR["info"],
            fields=[("Status", message, False)],
            mode=self._live,
        )

    def stop(self) -> None:
        """Signal the background worker to exit cleanly."""
        if self._worker_thread and self._worker_thread.is_alive():
            self._q.put(None)  # sentinel

    # ------------------------------------------------------------------
    # Internal send helpers
    # ------------------------------------------------------------------

    def _send_embed(
        self,
        title: str,
        color: int,
        fields: list[tuple[str, str, bool]],
        mode: bool,
    ) -> None:
        """Queue an embed payload for async delivery."""
        if not self._url:
            logger.debug("Discord URL not set; skipping embed: %s", title)
            return

        embed_fields = [
            {"name": name, "value": value, "inline": inline}
            for name, value, inline in fields
        ]
        payload = {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": embed_fields,
                "footer": {
                    "text": f"MCX Gold Engine • {_MODE_LABEL[mode]} • {datetime.now(tz=IST).strftime('%Y-%m-%d %H:%M IST')}"
                },
            }]
        }
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            logger.warning("Discord queue full; dropping message: %s", title)

    def _worker(self) -> None:
        """Background thread: drain queue and POST to Discord webhook."""
        while True:
            payload = self._q.get()
            if payload is None:
                break  # sentinel — clean shutdown
            try:
                resp = self._session.post(
                    self._url,
                    json=payload,
                    timeout=10,
                )
                if resp.status_code == 429:
                    retry_after = float(resp.json().get("retry_after", 1.0))
                    logger.warning("Discord rate-limited; sleeping %.1fs", retry_after)
                    _time.sleep(retry_after)
                elif resp.status_code not in (200, 204):
                    logger.error("Discord webhook error: %d %s", resp.status_code, resp.text[:200])
            except Exception as exc:
                logger.error("Discord send failed: %s", exc)
            finally:
                _time.sleep(0.5)  # polite rate-limit guard
