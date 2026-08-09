# 2Lotus Live Core

Subprojeto operacional isolado para XAUUSD/M5 com quatro perfis:

- `core-zigzag`: Happy Gold ZigZag Scalper (shadow)
- `core-stochastic`: Stochastic Sell 18 UTC (shadow)
- `core-multitimeframe`: Tendencia Multitemporal (shadow)
- `core-portfolio`: Happy Gold + Portfolio 7/6 - 100/100 (unico elegivel ao canario)

## Estado de seguranca

O projeto nasce com execucao real desativada em duas camadas. A API exige simultaneamente
`DAYTRADE_ENABLE_LIVE_DECISIONS=true`, `LIVE_CANARY_ARMED=true`, Supabase disponivel e o login
MT5 exato. O EA exige `EnableOrderExecution=true` e repete as travas de login, spread, posicao,
lote maximo de `0.01` e uma entrada por dia.

A matriz completa de protecoes, incluindo camada responsavel, valor inicial e comportamento de
bloqueio, esta em [`docs/safety-controls.md`](docs/safety-controls.md).
As regras, prioridades e limitacoes das quatro estrategias estao em
[`docs/strategies.md`](docs/strategies.md).

Nao ative as duas chaves antes do smoke test em conta demo. Backtest e forward test nao garantem
resultado real, especialmente porque estas operacoes duram segundos e dependem de ticks, spread,
latencia, slippage e especificacoes da corretora.

## Execucao local

```bash
cd python-service
cp ../.env.example .env
docker build -t 2lotus-live-core .
docker run --rm --env-file .env -p 8000:8000 2lotus-live-core
```

Depois, adicione `http://127.0.0.1:8000` na allowlist de WebRequest do MT5, compile
`mt5/experts/LiveCoreBridgeEA.mq5` e mantenha `EnableOrderExecution=false` no primeiro ciclo.

## Promocao controlada

1. Validar `/health` e quatro consultas sem ordens.
2. Confirmar que `core-portfolio` registra candidatos e estado diario no Supabase isolado.
3. Executar em demo com os mesmos simbolo, contrato, spread e VPS da conta real.
4. Preencher o login nos dois lados e armar API e EA apenas durante uma janela monitorada.
5. Desarmar primeiro `LIVE_CANARY_ARMED` para o kill switch do servidor.
