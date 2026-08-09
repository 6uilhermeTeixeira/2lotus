# Banco local

O Live Core pode usar SQLite para persistencia operacional nesta maquina. O arquivo padrao e
`python-service/data/runtime.db` e nao e versionado pelo Git.

## Conteudo

- `trade_results`: operacoes reais ou demo reportadas pelo MT5.
- `candles`: OHLCV por bot, simbolo, timeframe e timestamp.
- `ticks`: ticks deduplicados por coletor, simbolo, milissegundo e sequencia.
- `decisions`: request, resposta, latencia, acao e motivo de cada avaliacao.

O banco usa `WAL`, chaves primarias idempotentes e indices para consultas de dashboard.

## Inicializacao

```bash
cd python-service
DAYTRADE_RESULTS_STORE=SQLITE \
DAYTRADE_SQLITE_PATH=./data/runtime.db \
../.venv/bin/python scripts/init_local_db.py
```

Para iniciar a API local:

```bash
DAYTRADE_RESULTS_STORE=SQLITE \
DAYTRADE_SQLITE_PATH=./data/runtime.db \
../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Limites

- O filesystem da Vercel e efemero. A producao Vercel deve continuar em JSON ate receber um banco
  remoto, e nunca deve apontar para este arquivo.
- O SQLite local alimenta dashboard, ingestao e auditoria, mas ainda nao satisfaz sozinho a trava
  `persistent_risk_state` do canario real. A execucao continua fail-closed.
- Ticks crescem rapidamente. A fase seguinte deve exportar particoes diarias antigas para Parquet e
  manter no SQLite apenas a janela operacional recente.

## Backup consistente

Com a API parada, copie `runtime.db`. Com a API ativa, use o comando nativo de backup:

```bash
sqlite3 data/runtime.db ".backup 'data/backups/runtime-$(date -u +%F).db'"
```

Os arquivos `runtime.db-wal` e `runtime.db-shm` sao temporarios e nao devem ser copiados isoladamente.

## Sincronizacao remota

A sincronizacao futura enviara apenas candles consolidados, operacoes, decisoes resumidas e metricas
ao Supabase. Ticks brutos permanecerao locais/arquivados para evitar custo e volume desnecessarios.

## Reconstrucao do backtest M5

O replay consolidado de cinco anos usa candles M5 e nao exige ticks. Execute:

```bash
DAYTRADE_RESULTS_STORE=SQLITE \
DAYTRADE_SQLITE_PATH=./data/runtime.db \
../.venv/bin/python scripts/rebuild_local_backtests.py \
  --source ../.runtime/market-data/dukascopy/m5/xauusd-m5-bid-2021-08-09-2026-08-09T23-17.csv \
  --days 1827
```

Esse replay continua sendo uma aproximacao OHLC. Ticks ainda sao necessarios para validar com maior
fidelidade stops, trailing, spread variavel e operacoes do ZigZag encerradas dentro do candle M5.
