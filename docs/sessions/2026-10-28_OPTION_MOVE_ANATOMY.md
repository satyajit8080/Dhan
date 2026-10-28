# How, when and why SENSEX ATM options move (28 Oct 2026; `tools/option_move_anatomy.py`)

Data: 1 year of real expired nearest-weekly ATM CALL/PUT 1-min prices (Dhan rolling option), 247 days. 15-min windows
with the same strike throughout; DTE = trading days to expiry (0 = expiry day). The rolling files carry no IV, so the
IV effect is inside "decay+". Full output: `docs/exports/option_move_anatomy_output.txt`.

**How (per 15 min, per side):** the delta part averages ≈ 0 (index direction is a coin flip), the non-directional part
is always negative: −6.1 pts on expiry day, −3.1 one day before, −1.5 to −2.4 further out.

| DTE | ATM premium | Decay per hour | Still-index straddle loss / 15 min | Move needed in your direction (incl. ~3 spread) | Happens |
|---|---|---|---|---|---|
| 0 (expiry) | ~136 | **17.9 %** | −13.3 | > 16 pts | ~31 % |
| 1 | ~283 | 4.3 % | −7.7 | > 11 pts | ~38 % |
| 2 | ~386 | 2.2 % | −4.6 | > 8 pts | ~41 % |
| 3 | ~469 | 1.3 % | −3.7 | > 7 pts | ~41 % |

**When:** the index moves most at 09:15–10:00 (avg |15-min move| 88 pts) and 14:00–15:00 (70); least 11:00–13:30 (~58).
Option loss is biggest at the open (overnight premium released: −10 to −24 pts in the first 15 min) and on expiry day
after 14:00 (−13 to −17 per 15 min). The CE rose in only 34–47 % of 15-min windows and the PE in 40–48 %.

**Why buyers lose:** premium = time value; it bleeds every minute (fastest on expiry day and at the open), and the
index has no persistent direction, so the delta gain averages zero while decay and spread are always paid. A buyer
needs a move bigger than the bleed in the right direction, which happens in roughly a third of windows.
