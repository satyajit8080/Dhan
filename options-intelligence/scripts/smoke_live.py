"""Phase 1 exit-criteria smoke test against live Dhan (read-only).

For each of the five indices: resolve from the current instrument master,
fetch the expiry list, pull the nearest chain, and validate its schema.
Run during market hours:  python scripts/smoke_live.py   (env as in .env.example)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from doi.app.runtime import build_runtime
from doi.core.config import Settings
from doi.core.errors import DoiError
from doi.core.logging import configure_logging


async def main() -> int:
    configure_logging("WARNING")
    rt = await build_runtime(Settings.from_env())
    if rt.provider is None:
        print("DHAN_CLIENT_ID not set")
        return 2
    print(f"master {rt.master_version[:12]}  token {rt.token.peek_status().state if rt.token else '-'}")
    failed = 0
    for key in rt.configs:
        ref = rt.universe.get(key)
        if ref is None:
            print(f"FAIL {key:<10} resolve: {rt.universe_errors.get(key)}")
            failed += 1
            continue
        try:
            exps = await rt.provider.get_expiries(ref)
            master_exps = {p.expiry for p in ref.expiries}
            near = next((e for e in exps if e in master_exps), None)
            if near is None:
                raise DoiError(f"no expiry common to API {exps[:3]} and master")
            snap = await rt.provider.get_option_chain(ref, near)
            prof = ref.profile(near)
            two_sided = sum(1 for r in snap.rows for leg in (r.ce, r.pe) if leg and leg.two_sided)
            print(
                f"OK   {key:<10} id={ref.security_id} seg={ref.derivative_segment} exp={near} lot={prof.lot_size} "
                f"step={prof.strike_step} strikes={len(snap.rows)} two_sided_legs={two_sided} "
                f"ltp={snap.underlying_ltp} age={snap.age_s(datetime.now(UTC)):.1f}s"
            )
        except DoiError as e:
            print(f"FAIL {key:<10} {type(e).__name__}: {e}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
