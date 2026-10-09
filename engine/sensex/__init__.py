"""
Read-only Python port of the bull50-dhan calculation layer.

Modules map one-to-one to the TypeScript sources (docs/PHASE4_PORT_PLAN.md):
  jscompat        JS numeric/formatting semantics needed for exact parity
  pricing         normal.ts, black76.ts, time.ts, forward.ts
  gate            pricing/gate.ts
  normalize       normalize.ts (+ historical.ts toCandles)
  integrity       integrity.ts
  pricing_bridge  pricingBridge.ts (fairPrice, attachPricing)
  liquidity       liquidity.ts
  levels          levels.ts (+ indicators.ts / structure.ts helpers it uses)
  expiries        instruments/expiries.ts (pure selection only)
  scan            refresh scan composed with the existing refresh_table.py

No network, no credentials, no order or account code anywhere in this package.
"""
