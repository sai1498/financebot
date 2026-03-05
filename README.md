# Gold Trading Bot - Quantum Edition 10.0

An automated trading system for XAUUSD (Gold) using MetaTrader 5 (MT5).

## Features
- **Quantum Strategy**: Uses a multi-factor scoring system (EMA, RSI, MACD, Bollinger Bands).
- **Risk Management**: ATR-based SL/TP (2:1 Reward-to-Risk), daily loss limits, and max drawdown protection.
- **Volatility Guard**: ATR spike filter prevents trading during high-impact news.
- **Automatic Session Sync**: Detecting broker GMT offset for accurate London/New York session trading.

## Setup
1. Install Python 3.10+
2. Install dependencies: `pip install MetaTrader5 pandas numpy`
3. Configure `config.json` with your MT5 account details.
4. Run: `python monte.py`

## Project Structure
- `monte.py`: Main engine (Quantum Edition 10.0).
- `config.json`: Persistent configuration (Account info, Risk parameters).
- `mt5_gold.py`: MT5 session management and connectivity tests.
- `analyze_ticks.py`: Market data analysis utilities.

---
*Powered by CodeRabbit AI*
