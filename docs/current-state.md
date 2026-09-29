# Estado técnico atual — 3lotus Operations

Atualizado em 2026-09-10 e validado contra o commit `71d11e2`.

## Estado atual dos serviços

Há uma diferença importante entre a separação do código e a implantação atual:

| Serviço | Estado atual | Conta atual | Estado-alvo |
| --- | --- | --- | --- |
| `2lotus` | executa hoje o Live Core | demo já registrada | permanecer como laboratório/forward demo |
| `live-core` | ocioso | nenhuma conta operacional | receber exclusivamente a conta real |

Este repositório descreve o runtime de destino do serviço `live-core`; ele ainda não prova que a
migração operacional foi concluída. Até a troca formal, toda evidência produzida no serviço
`2lotus` deve ser classificada como demo, mesmo quando executada pelo código do Live Core.

## Missão e fronteira

O Live Core é o ambiente operacional isolado que recebe estratégias aprovadas no laboratório
2Lotus. Ele não é o ambiente de pesquisa. Sua responsabilidade é acompanhar quatro estratégias V3,
preservar auditoria e permitir um canário de lote mínimo somente após aprovação de todas as travas.

## Componentes atuais

- FastAPI/Python 3.12: portal, ingestão, decisão, replay, telemetria e readiness.
- `LiveCoreBridgeEA.mq5`: consulta a API, reporta resultados e reaplica controles antes da ordem.
- Supabase: persistência exigida pelo guardião do canário e fonte remota do estado operacional.
- SQLite: persistência local de candles, ticks, decisões e resultados; não habilita o canário sozinho.
- JSON: catálogo e snapshots estáticos/fallback.
- Registro de promoções: importa bundles com validação SHA-256 e sempre os registra desativados.

## Catálogo e execução

O catálogo contém somente `core-zigzag-v3`, `core-stochastic-v3`, `core-multitimeframe-v3` e
`core-combined-v3`. O EA consulta os três componentes com execução proibida e reserva a chamada
executável ao combinado. A API somente promove uma resposta se o ID corresponder a
`LIVE_CANARY_BOT_ID`, todas as chaves estiverem armadas, houver estado persistente, conta correta,
candidato válido, spread aceitável, nenhuma posição e ordens protetivas válidas.

O estado padrão é seguro: live desativado, canário desarmado, login zero, allowlist vazia, EA sem
execução e lote máximo de 0,01.

Nenhuma conta real deve ser conectada ao serviço dedicado antes de completar o preflight, isolar
segredos e persistência, validar o kill switch e executar o plano de migração do PRD.

## Superfície operacional

- Portal: `/overview`, `/bots`, `/trades`, `/security`, `/compare`, `/growth-simulator`, `/operations`.
- API autenticada: catálogo, comparação, gráficos, replays, operações, métricas e configuração.
- Ingestão com chave: `/decision`, `/api/candles`, `/api/ticks`, `/api/trades`.
- Diagnóstico: `/health` mede processo; `/ready` mede configuração e dependências.

## Qualidade conhecida

Em 2026-09-10, 81 testes unitários passaram, Ruff passou e a cobertura total medida foi 56%. As
áreas menos cobertas são o guardião live, rotas principais, histórico, SQLite e resultados. O script
`coverage` do virtualenv possui shebang antigo; `python -m coverage` funciona.

## Fontes de verdade

O catálogo `python-service/data/bots.json`, `app/config.py`, `app/live_guard.py`, `app/main.py` e o
EA são autoritativos para comportamento. Documentos e resultados de replay descrevem evidência,
mas não substituem a configuração efetivamente carregada.
