# bull50-dhan

Read-only Dhan market data for SENSEX options scalping.

## Requirements
- Node.js 20+ on the machine running Claude (the MCP server is a local process).
- A Dhan client id and a daily access token.

## Setup
1. Set your Dhan client id once, then restart Claude:
   `setx DHAN_CLIENT_ID <your id>`   (Windows)
   `export DHAN_CLIENT_ID=<your id>` (macOS/Linux, in your shell profile)
2. Each morning, supply that day's token with `/dhan-token <token>`,
   or just say "set my Dhan token to ...".

## What it will NOT do
- No order placement, modification or cancellation. No positions, holdings,
  funds or margin. Order-shaped API paths are refused before a request is sent.
- Never uses the SENSEX index LTP as spot for option maths.
- Never uses vendor IV or vendor Greeks; they are quarantined for comparison only.
- Never mixes two timestamps in one calculation.
- Publishes nothing when the data-integrity gate blocks.
