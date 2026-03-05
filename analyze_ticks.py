import pandas as pd

file_path = "C:/questdata/EURUSD_1year_ticks.csv"
df = pd.read_csv(file_path)

print("Converting timestamp...")

df['datetime'] = pd.to_datetime(
    df['Timestamp'],
    format='%Y%m%d %H:%M:%S:%f'
)

df.set_index('datetime', inplace=True)

print("Done.")
ticks_per_day = df.resample('D').size()

print("Average ticks per day:", int(ticks_per_day.mean()))
print("Max ticks in a day:", ticks_per_day.max())
print("Min ticks in a day:", ticks_per_day.min())
df['hour'] = df.index.hour

# Asian session (00–06 UTC approx)
asian = df[(df['hour'] >= 0) & (df['hour'] < 6)]

# London session (07–16 UTC approx)
london = df[(df['hour'] >= 7) & (df['hour'] < 16)]

print("Asian ticks:", len(asian))
print("London ticks:", len(london))
price = df['Bid price']

ohlc = price.resample('5min').ohlc()

volume = df['Bid volume'].resample('5min').sum()

bars_5m = ohlc.copy()
bars_5m['volume'] = volume

print(bars_5m.head())