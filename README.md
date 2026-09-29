# 3lotus Operations

Registro operacional, telemetria, auditoria e resultados do runner 2lotus. O serviço novo usa a porta `7773`; durante a migração, as rotas históricas de decisão permanecem apenas para compatibilidade e continuam desarmadas por padrão.

O endpoint `POST /api/v1/events/batch` recebe lotes idempotentes autenticados por `LOTUS_OPERATIONAL_EVENT_TOKEN`. A chave `event_id` impede duplicação durante retries da outbox da VPS.

Operação simultânea demo/real: consulte [docs/two-account-runtime.md](docs/two-account-runtime.md).

Núcleo operacional isolado para acompanhar estratégias promovidas pelo 2Lotus em XAUUSD/M5.
O dashboard protegido é servido localmente em `http://127.0.0.1:7773`.

O código está preparado, mas o serviço dedicado `live-core` está atualmente ocioso. O Live Core em
uso ainda roda no serviço `2lotus`, junto da conta demo já registrada. O estado-alvo é mover o Live
Core para seu serviço dedicado e reservar o serviço `2lotus` exclusivamente à conta demo.

- `core-zigzag-v10`: ZigZag Scalper V4.0 — roteador de contexto (shadow)
- `core-zigzag-v8`: ZigZag Scalper V5.1 — filtro temporal (shadow)

## Estado de seguranca

O projeto nasce com execucao real desativada em duas camadas. A API exige simultaneamente
`DAYTRADE_ENABLE_LIVE_DECISIONS=true`, `LIVE_CANARY_ARMED=true`, Supabase disponivel e o login
MT5 exato. O EA exige `EnableOrderExecution=true` e repete as travas de login, spread, posicao,
lote maximo de `0.01`. O limite diario de entradas do EA fica desativado por padrao
(`MaxLiveEntriesPerDay=0`).

A matriz completa de protecoes, incluindo camada responsavel, valor inicial e comportamento de
bloqueio, esta em [`docs/safety-controls.md`](docs/safety-controls.md).
As regras, prioridades e limitações das duas estratégias estão em
[`docs/strategies.md`](docs/strategies.md).
O retrato técnico validado do sistema e o backlog priorizado estão em
[`docs/current-state.md`](docs/current-state.md) e [`docs/PRD-improvements.md`](docs/PRD-improvements.md).
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
docker run --rm --env-file .env -p 7773:7773 live-core
```

Depois, adicione `http://127.0.0.1:7773` na allowlist de WebRequest do MT5, compile
`mt5/experts/LiveCoreBridgeEA.mq5` e mantenha `EnableOrderExecution=false` no primeiro ciclo.

## Promocao controlada

Um bundle aprovado e versionado produzido no 2Lotus pode ser registrado com:

```bash
cd python-service
.venv/bin/python ../scripts/import_strategy_bundle.py --bundle /caminho/core-portfolio-1.0.0.tar.gz
```

A importacao valida contrato, nomes e SHA-256, rejeita path traversal e sempre grava
`active=false` e `live_canary_armed=false`. Ela nao habilita execucao real.

1. Validar `/health` e duas consultas sem ordens.
2. Confirmar que os dois IDs ZigZag registram candidatos e estado diário no Supabase isolado.
3. Executar em demo com os mesmos simbolo, contrato, spread e VPS da conta real.
4. Preencher o login nos dois lados e armar API e EA apenas durante uma janela monitorada.
5. Desarmar primeiro `LIVE_CANARY_ARMED` para o kill switch do servidor.
