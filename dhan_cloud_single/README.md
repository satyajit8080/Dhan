# Single-file Dhan Cloud strategy

Upload only `main.py`. Its only dependency is `requests==2.34.2`.
Set these Dhan Cloud Variables: `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

On start the file:
1. runs a self-test of every formula, printed in the logs;
2. pulls 6 months of 5-minute data from Dhan in memory (index, every live futures contract, daily bars),
   stitches the front-month futures series and prints the backtest: trades, net Rs after charges,
   win-rate CI, profit factor, drawdown, results by setup, and the last 20 trades;
3. runs live: after every closed 5-minute bar (09:30-15:10 IST) it prints the signal card for each
   index and sends Telegram on a new LONG/SHORT.

The file is 865 lines, while RULES.md suggests splitting files over ~200 lines. If the Dhan Cloud
code scanner rejects the size, upload the split version in `../dhan_cloud/` instead.
Futures history reaches back only ~3 months, because Dhan lists only live contracts.
