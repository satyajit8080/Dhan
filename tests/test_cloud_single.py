"""Single-file Dhan Cloud strategy: self-test passes and results match the full engine exactly."""
import importlib.util
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import main as M  # noqa: E402
import test_indicators as TI  # noqa: E402

spec = importlib.util.spec_from_file_location("cloud_single", ROOT / "dhan_cloud_single" / "main.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def test_selftest_passes():
    assert S.selftest()


def test_indicator_and_signal_parity():
    for b in (TI.rnd_bars(700, 4), TI.rnd_bars(700, 8), TI.load_fixture()):
        a, c = M.compute_indicators(b), S.compute(b)
        for k in a:
            TI.same(a[k], c[k], 1e-7)
        lv = M.ind_pivots(24641.6, 24330, 24580.2)
        for i in range(1, len(b)):
            s1, s2 = M.tech_signal_at(a, i, lv, M.TechParams()), S.signal_at(c, i, lv)
            assert (s1["signal"], s1["setup"]) == (s2["signal"], s2["setup"])


def test_backtest_parity():
    b = TI.rnd_bars(3000, 11)
    dl = [{"ts": TI.T0 + d * 86400 - 86400, "high": 24300, "low": 23700, "close": 24000} for d in range(60)]
    r1, r2 = M.backtest_tech(b, dl, 65, "NSE"), S.backtest(b, dl, 65, "NSE")
    assert r1["trades"] == r2["trades"] > 0
    for t1, t2 in zip(r1["trades_list"], r2["trades_list"]):
        assert t1["entry_ts"] == t2["entry_ts"] and t1["why"] == t2["why"] and abs(t1["net"] - t2["net"]) < 1e-6
    assert abs(r1["net_rupees"] - r2["net_rupees"]) < 1e-6


def test_dhan_cloud_restrictions():
    src = (ROOT / "dhan_cloud_single" / "main.py").read_text()
    for bad in ("os.getenv", "os.path", "subprocess", "open(", "socket", "sqlite", ".write(", "eval(", "exec("):
        assert bad not in src, bad
    assert "{{DHAN_ACCESS_TOKEN}}" in src


def test_pull_and_backtest_with_mocked_dhan(monkeypatch):
    bars = TI.rnd_bars(1500, 3)

    def fake_post(path, payload, retries=3):
        if path == "/charts/historical":
            return {"timestamp": [TI.T0 + d * 86400 - 86400 for d in range(30)], "open": [24000] * 30,
                    "high": [24300] * 30, "low": [23700] * 30, "close": [24000] * 30, "volume": [0] * 30}
        sel = [x for x in bars if payload["fromDate"][:10] <= S.dt.datetime.fromtimestamp(x["ts"], S.IST).date().isoformat() <= payload["toDate"][:10]]
        return {k: [x[k2] for x in sel] for k, k2 in (("timestamp", "ts"), ("open", "open"), ("high", "high"),
                                                      ("low", "low"), ("close", "close"), ("volume", "volume"))}
    monkeypatch.setattr(S, "post", fake_post)
    monkeypatch.setattr(S, "all_futures", lambda: {n: [{"sid": 1, "expiry": "2099-01-01", "lot": 65.0, "segment": "NSE_FNO",
                                                        "display": "X"}] for n in S.INDICES})
    monkeypatch.setattr(S, "now_ist", lambda: S.dt.datetime.fromtimestamp(bars[-1]["ts"], S.IST))
    out = S.pull_and_backtest(1)
    assert set(out) == set(S.INDICES) and all(v["trades"] > 0 for v in out.values())
