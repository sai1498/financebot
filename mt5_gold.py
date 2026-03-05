import MetaTrader5 as mt5
import time

# Connect MT5
if not mt5.initialize():
    print("Initialize failed:", mt5.last_error())
    quit()

symbol = "XAUUSD"
lot = 0.01

# Get price
tick = mt5.symbol_info_tick(symbol)

if tick is None:
    print("Symbol not found")
    mt5.shutdown()
    quit()

price = tick.ask

# Calculate SL and TP
sl = price - 2.0
tp = price + 5.0

# BUY order request
request = {
    "action": mt5.TRADE_ACTION_DEAL,
    "symbol": symbol,
    "volume": lot,
    "type": mt5.ORDER_TYPE_BUY,
    "price": price,
    "sl": sl,
    "tp": tp,
    "deviation": 20,
    "magic": 123456,
    "comment": "Python Buy Order",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC,
}

# Send order
result = mt5.order_send(request)

print("Buy order result:", result)

# Check for error
if result.retcode != mt5.TRADE_RETCODE_DONE:
    print("Order failed:", result.retcode)
    mt5.shutdown()
    quit()

print("BUY order placed")

# Wait 5 seconds
time.sleep(5)

# Get open positions
positions = mt5.positions_get(symbol=symbol)

if positions:
    position = positions[0]
    ticket = position.ticket
    volume = position.volume

    print("Position found:", ticket)

    # Close position
    close_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL,
        "position": ticket,
        "price": mt5.symbol_info_tick(symbol).bid,
        "deviation": 20,
        "magic": 123456,
        "comment": "Close position",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    close_result = mt5.order_send(close_request)

    print("Close result:", close_result)

else:
    print("No open position found")

mt5.shutdown()