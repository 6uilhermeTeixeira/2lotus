# Production checklist

## Required before arming

- Dedicated Supabase project with the existing 2Lotus schema and only the four core bot rows.
- Unique ingest key and service-role key stored only in the runtime secret manager.
- MT5/VPS clock checked against UTC; strategy sessions are evaluated in UTC.
- Demo smoke test using the same broker symbol, contract size and VPS route intended for live.
- `core-portfolio` returning valid stop loss and take profit while execution remains disabled.
- MT5 journal showing the expected account login, spread and daily-entry guards.

## First canary window

- API: `DAYTRADE_ENABLE_LIVE_DECISIONS=true` and `LIVE_CANARY_ARMED=true`.
- EA: matching `AllowedAccountLogin`, `MaxLiveLot=0.01`, `MaxLiveEntriesPerDay=1`.
- Human monitoring of the terminal and server logs for the full position lifecycle.
- Stop immediately on symbol mismatch, stale candles, rejected stops, abnormal spread or API errors.

## Kill switch

Set `LIVE_CANARY_ARMED=false` and restart/redeploy the API. The EA will receive only `hold` even if
its local execution switch remains enabled. For immediate terminal isolation, also disable Algo Trading.
