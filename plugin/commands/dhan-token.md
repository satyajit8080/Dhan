---
description: Set today's Dhan access token and confirm the server is live
argument-hint: "[token] (paste the JWT; optionally: token client_id)"
allowed-tools: mcp__bull50-dhan__set_dhan_token, mcp__bull50-dhan__dhan_token_status, mcp__bull50-dhan__get_expiries
---

The user is starting their trading day and supplying a fresh Dhan access token.

Arguments given: $ARGUMENTS

Do this, in order:

1. If an argument was supplied, treat the first whitespace-separated value as the
   access token and the second, if present, as the client id. Call
   `set_dhan_token` with them.

   If no argument was supplied, call `dhan_token_status` instead and report
   whether a usable token is already present. Ask for one only if it is missing
   or expired — do not ask the user to re-paste a token that is still valid.

2. Report back, in this order and nothing more:
   - whether the token was accepted
   - the fingerprint (last 4 only) and client id
   - hours until expiry
   - the daily request budget remaining

   Never echo the token itself, not even partially beyond the fingerprint the
   tool returns.

3. Verify the token actually works by calling `get_expiries` for SENSEX. A token
   that parses but is rejected by Dhan is not a working token. Report the
   nearest two or three expiries as proof of life.

4. Remind the user once, briefly, that the token they just pasted is now in the
   conversation transcript, and that `DHAN_TOKEN_FILE` avoids that if they would
   rather not have it there. Say it once; do not repeat it on later turns.

If `set_dhan_token` fails, report the error code and what to fix. Do not retry
with a modified token.
