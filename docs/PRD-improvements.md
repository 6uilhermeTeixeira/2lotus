# PRD — endurecimento do Live Core

## Objetivo

Levar o núcleo de um canário tecnicamente possível para um canário operacionalmente verificável,
repetível e reversível, sem ampliar o universo de estratégias nem o risco máximo.

## P0 — segurança e evidência de liberação

- Ativar o serviço dedicado `live-core` sem conta real e validar health/readiness isoladamente.
- Manter a conta demo já registrada exclusivamente no serviço `2lotus`.
- Migrar a conta real somente depois de separar URLs, chaves, Supabase, logs e alertas por ambiente.
- Cobrir `apply_live_canary_guard` e sua integração em `/decision` em todos os ramos de bloqueio.
- Criar preflight automatizado para allowlist, conta, Supabase, spread, lote, chaves e versão do bundle.
- Persistir recibo imutável de armamento/desarmamento com operador, horário, commit e configuração sem segredos.
- Definir procedimento testado para posição aberta durante kill switch e indisponibilidade da API.

Aceite: nenhuma decisão `buy/sell` em testes de integração sem todas as travas; preflight produz
relatório reproduzível; desarme remoto é demonstrado em conta demo.

## P0 — migração dos serviços

1. Inventariar deploy, domínio/URL, containers, volumes, secrets e conta MT5 do serviço `2lotus`.
2. Provisionar o `live-core` com configuração fail-closed e credenciais independentes.
3. Fazer smoke test sem conta, depois com conta demo temporária, sem execução.
4. Confirmar que o `2lotus` continua operando somente a demo registrada.
5. Conectar a conta real ao `live-core`, ainda desarmado, e validar apenas leitura/heartbeat.
6. Liberar canário em janela monitorada após aprovação humana do checklist.

Aceite: cada conta aparece em exatamente um serviço; nenhuma chave ou banco é compartilhado por
engano; desligar `live-core` não interrompe a demo; desligar `2lotus` não afeta a conta real.

## P1 — observabilidade e recuperação

- Alertas para heartbeat atrasado, candles stale, erro Supabase, latência, stops rejeitados e spread.
- Backup automatizado do SQLite com restauração testada e retenção de ticks em Parquet.
- Runbook de restart do MT5/API/rede e reconciliação idempotente após queda.

Aceite: falhas simuladas não duplicam ordem/candidato, dashboards sinalizam degradação e a
restauração recupera o último estado consistente.

## P1 — qualidade do software

- Elevar cobertura para 75%, priorizando `live_guard.py`, `main.py`, `sqlite_store.py` e histórico.
- Dividir `main.py` em routers e extrair as estratégias V3 do módulo monolítico.
- Recriar o virtualenv para remover shebangs herdados de outro repositório.

## P2 — governança de promoção

- Versionar contrato de bundle e compatibilidade de schema.
- Exigir dataset, parâmetros, holdouts, custos e critérios de rollback no manifesto.
- Bloquear promoção quando o bundle não corresponder ao catálogo e ao commit aprovados.
