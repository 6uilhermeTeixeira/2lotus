# Live Core

Núcleo operacional isolado para acompanhar estratégias promovidas pelo 2Lotus em XAUUSD/M5.
O dashboard protegido é servido localmente em `http://127.0.0.1:1111`.

- `core-zigzag`: Happy Gold ZigZag Scalper (shadow)
- `core-stochastic`: Stochastic Sell 18 UTC (shadow)
- `core-multitimeframe`: Tendencia Multitemporal (shadow)
- `core-portfolio`: Happy Gold + Portfolio 7/6 - 100/100 (unico elegivel ao canario)

## Estado de seguranca

O projeto nasce com execucao real desativada em duas camadas. A API exige simultaneamente
`DAYTRADE_ENABLE_LIVE_DECISIONS=true`, `LIVE_CANARY_ARMED=true`, Supabase disponivel e o login
MT5 exato. O EA exige `EnableOrderExecution=true` e repete as travas de login, spread, posicao,
lote maximo de `0.01`. O limite diario de entradas do EA fica desativado por padrao
(`MaxLiveEntriesPerDay=0`).

A matriz completa de protecoes, incluindo camada responsavel, valor inicial e comportamento de
bloqueio, esta em [`docs/safety-controls.md`](docs/safety-controls.md).
As regras, prioridades e limitacoes das quatro estrategias estao em
[`docs/strategies.md`](docs/strategies.md).
O armazenamento SQLite local, inicializacao, limites e backup estao em
[`docs/local-database.md`](docs/local-database.md).

Nao ative as duas chaves antes do smoke test em conta demo. Backtest e forward test nao garantem
resultado real, especialmente porque estas operacoes duram segundos e dependem de ticks, spread,
latencia, slippage e especificacoes da corretora.

## Execucao local

```bash
cd python-service
cp ../.env.example .env
docker build -t live-core .
docker run --rm --env-file .env -p 1111:1111 live-core
```

Depois, adicione `http://127.0.0.1:1111` na allowlist de WebRequest do MT5, compile
`mt5/experts/LiveCoreBridgeEA.mq5` e mantenha `EnableOrderExecution=false` no primeiro ciclo.

## Promocao controlada

Um bundle aprovado e versionado produzido no 2Lotus pode ser registrado com:

```bash
cd python-service
.venv/bin/python ../scripts/import_strategy_bundle.py --bundle /caminho/core-portfolio-1.0.0.tar.gz
```

A importacao valida contrato, nomes e SHA-256, rejeita path traversal e sempre grava
`active=false` e `live_canary_armed=false`. Ela nao habilita execucao real.

1. Validar `/health` e quatro consultas sem ordens.
2. Confirmar que `core-portfolio` registra candidatos e estado diario no Supabase isolado.
3. Executar em demo com os mesmos simbolo, contrato, spread e VPS da conta real.
4. Preencher o login nos dois lados e armar API e EA apenas durante uma janela monitorada.
5. Desarmar primeiro `LIVE_CANARY_ARMED` para o kill switch do servidor.
