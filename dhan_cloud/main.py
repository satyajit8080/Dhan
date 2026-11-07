"""Dhan Cloud entry point: continuous loop (sleep-paced), read-only, no disk/ports/env access.
Every closed 5-minute bar of the front-month future: compute indicators + rules for each index,
print the card, and send a Telegram message when a new LONG/SHORT appears."""
import datetime as dt
import time
import traceback

import config
import data
import signal_card

OPEN, CLOSE = dt.time(9, 15), dt.time(15, 30)


def in_session(t):
    return t.weekday() < 5 and OPEN <= t.time() <= CLOSE


def cycle(state):
    now = time.time()
    if state.get("day") != data.now_ist().date():
        state["futs"] = data.front_futures()
        state["prev"] = {n: data.prev_day(v["sid"]) for n, v in config.INDICES.items()}
        state["day"] = data.now_ist().date()
        print("instruments:", {n: (f["display"], f["sid"], f["lot"]) for n, f in state["futs"].items()}, flush=True)
    vix_v = data.vix()
    for name, spec in config.INDICES.items():
        fut = state["futs"].get(name)
        if not fut:
            continue
        b1 = data.fut_1m(fut)
        expiry, legs = data.option_chain(spec["sid"])
        card, _ = signal_card.build(name, b1, state["prev"].get(name), legs, expiry or fut["expiry"], vix_v, fut["lot"], now)
        msg = signal_card.text(card)
        print(msg.replace("\n", " | "), flush=True)
        key = (name, card.get("bar"), card["signal"])
        if card["signal"] in ("LONG", "SHORT") and key not in state["sent"]:
            state["sent"].add(key)
            data.telegram(msg)


def run():
    if not config.configured():
        print("Set Dhan Cloud Variables DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN", flush=True)
    state = {"sent": set(), "last_bar": None}
    while True:
        t = data.now_ist()
        bar = (t.hour * 60 + t.minute) // config.BAR_MINUTES
        if config.configured() and in_session(t) and bar != state["last_bar"] and t.second >= 5:
            state["last_bar"] = bar
            try:
                cycle(state)
            except Exception:
                print("cycle error:", traceback.format_exc()[-800:], flush=True)
        time.sleep(config.LOOP_SLEEP_S)


if __name__ == "__main__":
    run()
