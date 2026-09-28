# Estratégias do Live Core

O catálogo operacional atual acompanha somente dois perfis ZigZag em `XAUUSD/M5`. Os horários são
UTC e os indicadores usam dados causais. Ambos permanecem permanentemente em shadow.

## ZigZag Scalper V4.0

- ID: `core-zigzag-v10`
- Perfil: `zigzag_scalper_v10_context_router_shadow`

- Agrega candles M5 completos em M30 e usa pivôs com duas velas de confirmação de cada lado.
- Opera rompimentos causais entre `03:00-19:00 UTC`.
- Em volatilidade normal usa a lógica/geometria V1; em expansão do range M5 usa filtros estruturais V2.
- O corte adaptativo padrão é range médio M5 igual ou superior a `6,0`.
- Roteia compras para V3.2 e vendas para V3.1, sem avaliar o ramo alternativo.
- Permanece em shadow.

## ZigZag Scalper V5.1

- ID: `core-zigzag-v8`
- Perfil: `zigzag_scalper_v8_accuracy_cycle_shadow`

- Aplica o calendário seletivo de semanas e dias em UTC ao ramo ZigZag contextual.
- Mantém sinais e risco herdados do roteador temporal V6.0.
- Permanece em shadow e requer replay unificado e validação forward.

## Stochastic Sell V3

- ID: `core-stochastic-v3`
- Perfil: `stochastic_sell_v3_adaptive_shadow`

- Usa Estocástico `14/3/3`, EMA50 e ATR14 em candles M5 concluídos.
- Exige cruzamento baixista a partir da região extrema e aceita somente vendas antes de `13:00 UTC`.
- Usa a geometria seletiva V2 e veta o contexto H1 vendedor fraco identificado na pesquisa.
- Stop acima das três velas recentes mais `0,25 ATR`; parcial em 1R e alvo segundo a variante ativa.
- Permanece em shadow quando consultado isoladamente.

## Tendência Multitemporal V3

- ID: `core-multitimeframe-v3`
- Perfil: `multitimeframe_trend_pullback_v3_adaptive_shadow`

- Opera compras entre `13:00-16:00 UTC` com alinhamento causal H1/M15/M5.
- Em range/transição H1 usa a V2 (`0,5 ATR`, alvo `1,25R`, seis candles).
- Em tendência compradora/alta volatilidade não contrária usa a V1 (`0,7 ATR`, alvo `0,75R`, doze candles).
- Tendência H1 vendedora e histórico insuficiente são bloqueados.
- Permanece em shadow quando consultado isoladamente.

## Combinação das três V3

- ID: `core-combined-v3`
- Perfil: `portfolio_v3_adaptive_union_shadow`

- Avalia ZigZag, Stochastic e Multitemporal V3 no mesmo ciclo e pode devolver candidatos paralelos.
- O EA consulta as três linhas individualmente sem execução e entrega somente a resposta do combinado
  à rotina que pode executar.
- É o único perfil elegível ao guardião real; por padrão a allowlist está vazia e nada é promovido.
- A promoção exige todas as travas descritas em `safety-controls.md`.

## Limites da reprodução

As estratégias são reconstruções baseadas em evidência observada, não o código do fornecedor.
Operações curtas dependem de ticks, spread, latência, slippage e contrato da corretora. Backtest,
holdout e forward shadow não garantem desempenho real.

## Onde acompanhar

- `/overview`: estado executivo do núcleo.
- `/bots` e `/compare`: catálogo, curvas e comparação.
- `/trades`: resultados reportados.
- `/security`: postura das travas.
- `/operations`: heartbeat, latência, dados recentes e erros.
