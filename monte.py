"""
Gold Trading Bot – Quantum Edition 10.0
========================================
Professional automated trading system for XAUUSD (Gold)


"""

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import logging
import os
import json
import threading
import sys
from datetime import datetime, timezone, timedelta
from typing import Tuple, Dict, Optional


# ==============================
# CONFIGURATION
# ==============================
class Config:
    # --- Core trading parameters (Monte Carlo validated) ---
    SYMBOL: str              = "XAUUSD"
    RISK_PER_TRADE: float    = 0.005   # 0.5% risk per trade
    MAX_DAILY_LOSS: float    = 0.04    # 4% daily loss limit
    MAX_DRAWDOWN: float      = 0.08    # 8% max drawdown from peak
    MAX_TRADES_PER_DAY: int  = 10      # Hard cap per day

    # --- Strategy parameters ---
    TIMEFRAME                = mt5.TIMEFRAME_M15
    MIN_SIGNAL_SCORE: float  = 5.5     # Raised from 4.5 — requires stronger confluence
    ATR_SL_MULT: float       = 2.0     # SL distance = 2.0x ATR
    ATR_TP_MULT: float       = 4.0     # TP distance = 4.0x ATR → 2:1 R:R
    ATR_SPIKE_MULT: float    = 2.5     # Block trades when current ATR > 2.5x avg ATR (news guard)
    VOLUME_LOOKBACK: int     = 20      # Candles for average volume calculation
    MIN_VOLUME_RATIO: float  = 1.1     # Current volume must be ≥ 110% of average (momentum needed)

    # --- Trading sessions (GMT) ---
    TRADING_SESSIONS_GMT: Dict[str, Tuple[int, int]] = {
        'london': (8, 16),   # 08:00–16:00 GMT
        'ny':     (13, 22),  # 13:00–22:00 GMT
    }

    # --- MT5 connection ---
    MT5_ACCOUNT: int   = 0
    MT5_PASSWORD: str  = ""
    MT5_SERVER: str    = ""

    # --- Order execution ---
    ORDER_DEVIATION: int    = 20    # Max slippage in points
    ORDER_RETRIES: int      = 3     # Retry count on transient failures
    ORDER_RETRY_DELAY: float = 1.5  # Seconds between retries
    MAGIC_NUMBER: int       = 234000

    # --- Logging ---
    LOG_LEVEL: str = "INFO"   # DEBUG | INFO | WARNING | ERROR

    # --- File paths ---
    BASE_DIR: str   = os.path.dirname(os.path.abspath(__file__))
    LOG_FILE: str   = os.path.join(BASE_DIR, "gold_trading.log")
    TRADE_LOG: str  = os.path.join(BASE_DIR, "trade_history.csv")
    CONFIG_FILE: str = os.path.join(BASE_DIR, "config.json")


# ==============================
# CONFIG MANAGER
# ==============================
class ConfigManager:
    """Handles loading/saving configuration (passwords never persisted)."""

    SERIALISABLE_FIELDS = [
        'SYMBOL', 'RISK_PER_TRADE', 'MAX_DAILY_LOSS', 'MAX_DRAWDOWN',
        'MAX_TRADES_PER_DAY', 'MIN_SIGNAL_SCORE', 'ATR_SL_MULT', 'ATR_TP_MULT',
        'ATR_SPIKE_MULT', 'VOLUME_LOOKBACK', 'MIN_VOLUME_RATIO',
        'MT5_ACCOUNT', 'MT5_SERVER', 'LOG_LEVEL',
    ]

    @classmethod
    def save(cls, config: Config, path: str = Config.CONFIG_FILE) -> None:
        data = {k: getattr(config, k) for k in cls.SERIALISABLE_FIELDS}
        with open(path, 'w') as f:
            json.dump(data, f, indent=4)
        logging.info(f"Config saved -> {path}")

    @classmethod
    def load(cls, path: str = Config.CONFIG_FILE) -> Config:
        config = Config()
        if os.path.exists(path):
            with open(path, 'r') as f:
                data: dict = json.load(f)
            for key in cls.SERIALISABLE_FIELDS:
                if key in data:
                    setattr(config, key, data[key])
            logging.info(f"Config loaded <- {path}")
        return config


# ==============================
# RISK MANAGER
# ==============================
class RiskManager:
    """
    Tracks daily P&L, drawdown, session windows, and position sizing.

    Bug fixes vs v9.5:
    - peak_equity resets to day_start on each new trading day so a prior
      bad day doesn't permanently lock out new trades.
    - market_time is always sourced from tick.time (broker clock) for
      accurate broker-day boundary detection.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.daily_pnl: float        = 0.0
        self.peak_equity: float      = 0.0
        self.current_drawdown: float = 0.0
        self.daily_trades: int       = 0
        self._last_reset_day: int    = -1
        self._day_start_equity: float = 0.0
        self.market_time: datetime   = datetime.now(timezone.utc)
        self.broker_gmt_offset: float = 0.0

    # ------------------------------------------------------------------
    def detect_broker_offset(self) -> None:
        """Derive broker's GMT offset from latest tick timestamp."""
        tick = mt5.symbol_info_tick(self.config.SYMBOL)
        if not tick:
            logging.warning("Cannot detect broker GMT offset — using 0.")
            return
        offset_s = tick.time - datetime.now(timezone.utc).timestamp()
        self.broker_gmt_offset = round(offset_s / 3600)
        logging.info(f"Broker GMT offset: {self.broker_gmt_offset:+.0f}h")

    # ------------------------------------------------------------------
    def calculate_position_size(self, account_balance: float,
                                 stop_loss_points: int) -> float:
        """Fixed-percentage position sizing rounded to broker lot step."""
        if stop_loss_points <= 0:
            return mt5.symbol_info(self.config.SYMBOL).volume_min

        info = mt5.symbol_info(self.config.SYMBOL)
        if not info:
            return 0.01

        risk_amount   = account_balance * self.config.RISK_PER_TRADE
        point_value   = info.trade_tick_value / info.trade_tick_size
        raw_lot       = risk_amount / (stop_loss_points * point_value)
        lot           = round(raw_lot / info.volume_step) * info.volume_step
        return max(info.volume_min, min(lot, info.volume_max))

    # ------------------------------------------------------------------
    def check_daily_limits(self, current_equity: float) -> bool:
        """
        Resets counters at the start of each broker day.
        FIX: peak_equity resets to today's starting equity so yesterday's
             losses don't carry forward into today's drawdown calculation.
        """
        broker_day = (self.market_time - timedelta(hours=self.broker_gmt_offset)).day

        if broker_day != self._last_reset_day:
            self._last_reset_day    = broker_day
            self._day_start_equity  = current_equity
            self.daily_pnl          = 0.0
            self.daily_trades       = 0
            self.peak_equity        = current_equity   # ← BUG FIX from v9.5
            logging.info(f"Day reset | starting equity: {current_equity:.2f}")

        # Daily P&L
        if self._day_start_equity > 0:
            self.daily_pnl = (current_equity - self._day_start_equity) / self._day_start_equity

        if self.daily_pnl < -self.config.MAX_DAILY_LOSS:
            logging.warning(f"Daily loss limit hit: {self.daily_pnl:.2%}")
            return False

        # Intra-day drawdown from today's peak
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        self.current_drawdown = (self.peak_equity - current_equity) / self.peak_equity \
                                 if self.peak_equity > 0 else 0.0

        if self.current_drawdown > self.config.MAX_DRAWDOWN:
            logging.warning(f"Max drawdown hit: {self.current_drawdown:.2%}")
            return False

        return True

    # ------------------------------------------------------------------
    def can_trade(self) -> bool:
        """Session window (GMT) + daily trade cap check."""
        if self.daily_trades >= self.config.MAX_TRADES_PER_DAY:
            logging.info("Daily trade cap reached.")
            return False

        gmt_hour = (self.market_time - timedelta(hours=self.broker_gmt_offset)).hour

        for name, (start, end) in self.config.TRADING_SESSIONS_GMT.items():
            if start <= gmt_hour < end:
                logging.debug(f"In {name} session (GMT {gmt_hour:02d}:xx)")
                return True

        logging.info(f"Outside sessions (GMT {gmt_hour:02d}:xx)")
        return False


# ==============================
# TRADING STRATEGY
# ==============================
class GoldTradingStrategy:
    """
    Multi-factor signal scoring on CLOSED candles only.

    All logic is pure-Python / pandas — no MT5 dependency here.
    This makes the class fully unit-testable with synthetic DataFrames.

    Changes vs v9.5:
    - MIN_SIGNAL_SCORE moved to Config (was hardcoded at 4.5)
    - Volume confirmation filter added
    - ATR spike guard added (suppresses signals during news volatility)
    - RSI thresholds tightened (< 28 / > 72 vs < 30 / > 70)
    - SL/TP computed from live tick price (passed in), NOT candle close
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Adds EMAs, RSI, MACD, Bollinger Bands, ATR, tick_volume ratio."""
        # EMAs
        for span in (9, 21, 50, 100):
            df[f'EMA{span}'] = df['close'].ewm(span=span, adjust=False).mean()

        # RSI (14)
        delta = df['close'].diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        df['RSI'] = 100 - (100 / (1 + gain / loss))

        # MACD (12/26/9)
        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD']        = ema12 - ema26
        df['MACD_signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_hist']   = df['MACD'] - df['MACD_signal']

        # Bollinger Bands (20, 2σ)
        df['BB_mid']   = df['close'].rolling(20).mean()
        bb_std         = df['close'].rolling(20).std()
        df['BB_upper'] = df['BB_mid'] + 2 * bb_std
        df['BB_lower'] = df['BB_mid'] - 2 * bb_std

        # ATR (14)
        df['ATR'] = self._atr(df, 14)

        # ATR spike ratio (current ATR vs rolling average)
        df['ATR_ratio'] = df['ATR'] / df['ATR'].rolling(self.config.VOLUME_LOOKBACK).mean()

        # Tick volume ratio (vs rolling average)
        df['vol_ratio'] = (
            df['tick_volume'] /
            df['tick_volume'].rolling(self.config.VOLUME_LOOKBACK).mean()
        )

        return df

    # ------------------------------------------------------------------
    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series:
        hl  = df['high'] - df['low']
        hc  = (df['high'] - df['close'].shift()).abs()
        lc  = (df['low']  - df['close'].shift()).abs()
        tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    # ------------------------------------------------------------------
    def score_signal(self, df: pd.DataFrame) -> Tuple[str, float, Dict]:
        """
        Evaluate the SECOND-TO-LAST candle (index -2) — always closed.
        Returns (direction, score, detail_dict).
        """
        cur  = df.iloc[-2]
        prev = df.iloc[-3]

        # --- News/volatility guard ---
        if cur['ATR_ratio'] > self.config.ATR_SPIKE_MULT:
            logging.info(f"ATR spike guard: ratio={cur['ATR_ratio']:.2f} — skipping.")
            return 'NONE', 0.0, {'blocked': 'atr_spike'}

        # --- Volume confirmation ---
        if cur['vol_ratio'] < self.config.MIN_VOLUME_RATIO:
            logging.debug(f"Low volume: ratio={cur['vol_ratio']:.2f} — skipping.")
            return 'NONE', 0.0, {'blocked': 'low_volume'}

        bull = 0.0
        bear = 0.0
        details: Dict = {}

        # ── Trend alignment (all 4 EMAs stacked) ──────────────────────
        if cur['EMA9'] > cur['EMA21'] > cur['EMA50'] > cur['EMA100']:
            bull += 2.5; details['trend'] = '+2.5 bull'
        elif cur['EMA9'] < cur['EMA21'] < cur['EMA50'] < cur['EMA100']:
            bear += 2.5; details['trend'] = '+2.5 bear'

        # ── Price vs EMA9 ──────────────────────────────────────────────
        if cur['close'] > cur['EMA9']:
            bull += 1.5; details['price_ema9'] = '+1.5 bull'
        else:
            bear += 1.5; details['price_ema9'] = '+1.5 bear'

        # ── MACD momentum (histogram direction + sign) ─────────────────
        if cur['MACD_hist'] > 0 and cur['MACD_hist'] > prev['MACD_hist']:
            bull += 2.0; details['macd'] = '+2.0 bull'
        elif cur['MACD_hist'] < 0 and cur['MACD_hist'] < prev['MACD_hist']:
            bear += 2.0; details['macd'] = '+2.0 bear'

        # ── RSI reversal (tightened to 28/72 from 30/70) ───────────────
        if cur['RSI'] < 28 and cur['RSI'] > prev['RSI']:
            bull += 2.0; details['rsi'] = '+2.0 oversold-reversal'
        elif cur['RSI'] > 72 and cur['RSI'] < prev['RSI']:
            bear += 2.0; details['rsi'] = '+2.0 overbought-reversal'

        # ── Bollinger Band bounce ──────────────────────────────────────
        if cur['close'] <= cur['BB_lower'] and prev['close'] <= prev['BB_lower']:
            bull += 1.5; details['bb'] = '+1.5 lower-band bounce'
        elif cur['close'] >= cur['BB_upper'] and prev['close'] >= prev['BB_upper']:
            bear += 1.5; details['bb'] = '+1.5 upper-band bounce'

        # ── Decision ──────────────────────────────────────────────────
        threshold = self.config.MIN_SIGNAL_SCORE
        if bull >= threshold and bull > bear:
            return 'BUY',  bull, details
        elif bear >= threshold and bear > bull:
            return 'SELL', bear, details
        return 'NONE', 0.0, details

    # ------------------------------------------------------------------
    def calculate_sl_tp(self, df: pd.DataFrame,
                         direction: str,
                         live_price: float) -> Tuple[float, float]:
        """
        SL/TP anchored to LIVE tick price (not stale candle close).

        FIX vs v9.5: using live_price instead of cur['close'] eliminates
        the gap between signal candle and actual fill price.
        R:R upgraded to 2:1 (ATR_SL_MULT=2.0, ATR_TP_MULT=4.0).
        """
        atr = df.iloc[-2]['ATR']
        sl_dist = atr * self.config.ATR_SL_MULT
        tp_dist = atr * self.config.ATR_TP_MULT

        if direction == 'BUY':
            return live_price - sl_dist, live_price + tp_dist
        else:
            return live_price + sl_dist, live_price - tp_dist


# ==============================
# TRADE EXECUTOR
# ==============================
class TradeExecutor:
    """
    Handles order dispatch with filling-mode detection, retry logic,
    slippage logging, and thread-safe trade journaling.
    """

    def __init__(self, config: Config, risk_manager: RiskManager) -> None:
        self.config       = config
        self.risk_manager = risk_manager
        self._filling_mode: Optional[int] = None
        self._csv_lock    = threading.Lock()

    # ------------------------------------------------------------------
    def _get_filling_mode(self) -> int:
        """Bitmask detection — cached after first call."""
        if self._filling_mode is not None:
            return self._filling_mode
        info = mt5.symbol_info(self.config.SYMBOL)
        if not info:
            return mt5.ORDER_FILLING_IOC
        fm = info.filling_mode
        if fm & 2:
            self._filling_mode = mt5.ORDER_FILLING_IOC
        elif fm & 1:
            self._filling_mode = mt5.ORDER_FILLING_FOK
        else:
            self._filling_mode = mt5.ORDER_FILLING_RETURN
        return self._filling_mode

    # ------------------------------------------------------------------
    def execute_signal(self, signal: str, score: float,
                        sl: float, tp: float) -> bool:
        if signal == 'NONE':
            return False

        # One-position safety
        if mt5.positions_get(symbol=self.config.SYMBOL):
            logging.debug("Position already open — skipping.")
            return False

        account = mt5.account_info()
        if not account:
            return False
        if not self.risk_manager.check_daily_limits(account.equity):
            return False
        if not self.risk_manager.can_trade():
            return False

        info = mt5.symbol_info(self.config.SYMBOL)
        tick = mt5.symbol_info_tick(self.config.SYMBOL)
        if not info or not tick:
            return False

        price    = tick.ask if signal == 'BUY' else tick.bid
        sl_pts   = int(abs(price - sl) / info.point)
        lot      = self.risk_manager.calculate_position_size(account.balance, sl_pts)

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       self.config.SYMBOL,
            "volume":       lot,
            "type":         mt5.ORDER_TYPE_BUY if signal == 'BUY' else mt5.ORDER_TYPE_SELL,
            "price":        price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    self.config.ORDER_DEVIATION,
            "magic":        self.config.MAGIC_NUMBER,
            "comment":      f"QE10 S:{score:.1f}",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(),
        }

        result = self._send_with_retry(request)
        if result is None:
            return False

        # Slippage logging
        slippage_pts = abs(result.price - price) / info.point
        if slippage_pts > 0:
            logging.info(f"Slippage: {slippage_pts:.1f} pts "
                         f"(requested {price:.2f}, filled {result.price:.2f})")

        logging.info(f"OK {signal} {lot} lots @ {result.price:.2f} | "
                     f"SL={sl:.2f} TP={tp:.2f} | score={score:.1f}")
        self.risk_manager.daily_trades += 1
        self._log_trade(result, signal, score, sl, tp)
        return True

    # ------------------------------------------------------------------
    def _send_with_retry(self, request: dict) -> Optional[object]:
        """Retry order send up to ORDER_RETRIES times on transient failures."""
        retryable = {
            mt5.TRADE_RETCODE_REQUOTE,
            mt5.TRADE_RETCODE_PRICE_CHANGED,
            mt5.TRADE_RETCODE_TIMEOUT,
            mt5.TRADE_RETCODE_CONNECTION,
            mt5.TRADE_RETCODE_PRICE_OFF,
        }
        for attempt in range(1, self.config.ORDER_RETRIES + 1):
            result = mt5.order_send(request)
            if result is None:
                logging.error(f"order_send returned None (attempt {attempt})")
            elif result.retcode == mt5.TRADE_RETCODE_DONE:
                return result
            elif result.retcode in retryable:
                logging.warning(f"Retryable error {result.retcode} "
                                 f"'{result.comment}' — retry {attempt}/{self.config.ORDER_RETRIES}")
                # Refresh price for requote
                tick = mt5.symbol_info_tick(self.config.SYMBOL)
                if tick:
                    request['price'] = tick.ask if request['type'] == mt5.ORDER_TYPE_BUY else tick.bid
                time.sleep(self.config.ORDER_RETRY_DELAY)
            else:
                logging.error(f"Order failed (non-retryable): "
                               f"{result.comment} (code {result.retcode})")
                return None
        logging.error("Order failed after all retries.")
        return None

    # ------------------------------------------------------------------
    def _log_trade(self, result, signal: str, score: float,
                   sl: float, tp: float) -> None:
        """Thread-safe CSV append."""
        row = {
            'timestamp': self.risk_manager.market_time.isoformat(),
            'signal':    signal,
            'volume':    result.volume,
            'price':     result.price,
            'sl':        sl,
            'tp':        tp,
            'score':     round(score, 2),
        }
        df = pd.DataFrame([row])
        with self._csv_lock:
            header = not os.path.exists(self.config.TRADE_LOG)
            df.to_csv(self.config.TRADE_LOG, mode='a', index=False, header=header)


# ==============================
# MAIN BOT
# ==============================
class GoldTradingBot:
    """
    Main control loop.

    Improvements vs v9.5:
    - Reconnect loop: if MT5 terminal disconnects, waits and retries
      instead of crashing.
    - Passes live tick price to calculate_sl_tp (SL/TP fix).
    - Clear startup banner with all live config values.
    """

    def __init__(self, config: Config) -> None:
        self.config       = config
        self.risk_manager = RiskManager(config)
        self.strategy     = GoldTradingStrategy(config)
        self.executor     = TradeExecutor(config, self.risk_manager)
        self.running      = False

    # ------------------------------------------------------------------
    def _initialize_mt5(self) -> bool:
        if not mt5.initialize():
            logging.error(f"MT5 init failed: {mt5.last_error()}")
            return False

        if self.config.MT5_ACCOUNT:
            pwd = (os.getenv("MT5_PASSWORD") or
                   self.config.MT5_PASSWORD or
                   input(f"MT5 password for account {self.config.MT5_ACCOUNT}: ").strip())
            if not mt5.login(self.config.MT5_ACCOUNT,
                             password=pwd,
                             server=self.config.MT5_SERVER):
                logging.error(f"MT5 login failed: {mt5.last_error()}")
                mt5.shutdown()
                return False

        self.risk_manager.detect_broker_offset()
        return True

    # ------------------------------------------------------------------
    def _print_banner(self) -> None:
        cfg = self.config
        lines = [
            "=" * 64,
            " GOLD TRADING BOT  ─  QUANTUM EDITION 10.0",
            "=" * 64,
            f"  Symbol     : {cfg.SYMBOL}",
            f"  Risk/trade : {cfg.RISK_PER_TRADE*100:.2f}%",
            f"  Daily loss : {cfg.MAX_DAILY_LOSS*100:.0f}%  |  Max DD: {cfg.MAX_DRAWDOWN*100:.0f}%",
            f"  SL mult    : {cfg.ATR_SL_MULT}x ATR  |  TP mult: {cfg.ATR_TP_MULT}x ATR  (R:R {cfg.ATR_TP_MULT/cfg.ATR_SL_MULT:.1f}:1)",
            f"  Min score  : {cfg.MIN_SIGNAL_SCORE}  |  ATR spike guard: {cfg.ATR_SPIKE_MULT}x",
            f"  Sessions   : London 08-16 GMT  /  NY 13-22 GMT",
            f"  GMT offset : {self.risk_manager.broker_gmt_offset:+.0f}h",
            "=" * 64,
        ]
        print("\n" + "\n".join(lines) + "\n")

    # ------------------------------------------------------------------
    def run(self) -> None:
        if not self._initialize_mt5():
            return

        self.running = True
        self._print_banner()

        last_candle_time = None
        reconnect_delay  = 10

        try:
            while self.running:
                # ── Terminal health check ──────────────────────────────
                if not mt5.terminal_info():
                    logging.warning(f"MT5 terminal lost — reconnecting in {reconnect_delay}s…")
                    mt5.shutdown()
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 120)
                    if self._initialize_mt5():
                        reconnect_delay = 10
                        logging.info("Reconnected.")
                    continue

                reconnect_delay = 10  # reset on healthy tick

                # ── Market time from tick ──────────────────────────────
                tick = mt5.symbol_info_tick(self.config.SYMBOL)
                if tick:
                    self.risk_manager.market_time = datetime.fromtimestamp(
                        tick.time, tz=timezone.utc)

                # ── Fetch candles ──────────────────────────────────────
                rates = mt5.copy_rates_from_pos(
                    self.config.SYMBOL, self.config.TIMEFRAME, 0, 150)
                if rates is None or len(rates) < 100:
                    time.sleep(5)
                    continue

                df = pd.DataFrame(rates)
                df['time'] = pd.to_datetime(df['time'], unit='s')

                # Wait for a new closed candle
                newest = df['time'].iloc[-1]
                if newest == last_candle_time:
                    time.sleep(10)
                    continue
                last_candle_time = newest

                # ── Indicators + signal ────────────────────────────────
                df = self.strategy.calculate_indicators(df)
                signal, score, details = self.strategy.score_signal(df)

                if signal != 'NONE':
                    live_price = tick.ask if signal == 'BUY' else tick.bid
                    sl, tp = self.strategy.calculate_sl_tp(df, signal, live_price)
                    fired  = self.executor.execute_signal(signal, score, sl, tp)
                    ts     = self.risk_manager.market_time.strftime('%H:%M UTC')
                    status = "→ ORDER SENT" if fired else "→ blocked"
                    print(f"[{ts}] {signal} signal  score={score:.1f}  {status}")
                    logging.info(f"Signal {signal} score={score:.1f} details={details}")

                # ── Status line ────────────────────────────────────────
                price_now = df['close'].iloc[-2]
                pnl_pct   = self.risk_manager.daily_pnl * 100
                dd_pct    = self.risk_manager.current_drawdown * 100
                print(f"\r  Price: {price_now:.2f}  |  P&L: {pnl_pct:+.2f}%  |  "
                      f"DD: {dd_pct:.2f}%  |  Trades today: {self.risk_manager.daily_trades}",
                      end="", flush=True)

                time.sleep(15)

        except KeyboardInterrupt:
            print("\nShutdown requested.")
            self.running = False
        finally:
            mt5.shutdown()
            logging.info("Bot shut down cleanly.")


# ==============================
# ENTRY POINT
# ==============================
if __name__ == "__main__":
    # Setup logging immediately before any other logging calls
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(Config.LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )

    config = ConfigManager.load()
    
    # Reload logging with configured level if necessary
    if config.LOG_LEVEL != "INFO":
        logging.getLogger().setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    bot = GoldTradingBot(config)
    bot.run()