# Estrategias Live Core

O Live Core acompanha quatro perfis em `XAUUSD` com entrada operacional em `M5`. Os horarios abaixo
sao UTC. Todos os indicadores usam apenas candles concluídos; os componentes isolados permanecem
em shadow e somente o portfólio coordenador pode ser promovido pelo guardiao do canario.

## Happy Gold ZigZag Scalper

Perfil: `happy_gold_zigzag_scalper`

- Agrega candles M5 em M30 e requer pelo menos sete candles M30 completos.
- Usa um proxy causal de ZigZag: um pivô precisa de duas velas confirmadas de cada lado.
- Compra quando o ask rompe o último pivô superior e vende quando o bid rompe o pivô inferior.
- Filtra a sessão para `03:00-19:00 UTC` e impede outra posição no mesmo símbolo.
- Stop fixo: 240 pontos. Alvo: 1000 pontos. Janela máxima: dois candles M5.
- A reconstrução também registra break-even em 40 pontos e trailing após 120 pontos, com distância
  de 60 pontos, para pesquisa e replay compatíveis.
- O lote original reconstruído de `0.10` aparece apenas como sugestão do componente. O canário real
  aplica o limite independente de `0.01`.

## Stochastic Sell 18 UTC

Perfil: `stochastic_sell_session`

- Calcula Estocástico `14/3/3`, EMA50 e ATR14 sobre M5, exigindo ao menos 60 candles.
- O setup-base baixista exige preço abaixo da EMA50 e cruzamento de `%K` abaixo de `%D` depois de
  a leitura anterior alcançar 65 ou mais.
- A variante selecionada aceita apenas `sell` e entradas estritamente anteriores a `18:00 UTC`.
- Stop acima da máxima das três velas mais recentes mais `0,25 ATR`.
- Parcial teórica em 1R e alvo final em 2R.
- O perfil isolado possui trava shadow permanente; no portfólio ainda depende de regime vendedor.

## Tendencia Multitemporal

Perfil: `multitimeframe_trend_pullback`

- Opera somente compras na janela `13:00-16:00 UTC`.
- H1: EMA20 acima da EMA50, EMA20 ascendente e distância entre médias de pelo menos `0,7 ATR`.
- M15: EMA9 acima da EMA21 e EMA9 ascendente.
- M5: EMA9 acima da EMA21, mínima tocando a EMA9 e candle comprador fechando acima dela.
- Stop de `1,2 ATR` do M5, alvo de `0,75R` e duração máxima de 12 candles M5.
- Aplica multiplicador de risco `0,5` e permanece em shadow isoladamente.

## Happy Gold + Portfolio 7/6 - 100/100

Perfil: `happy_gold_portfolio_100_100`

- Coordena o ZigZag com o portfólio formado por Tendência Multitemporal e Stochastic Sell.
- O ZigZag confirmado tem prioridade. Dentro do portfólio 7/6, Tendência Multitemporal precede
  Stochastic Sell.
- Se ZigZag e portfólio produzirem lados opostos simultaneamente, o coordenador bloqueia a entrada.
- O sinal Stochastic só é aceito quando o classificador identifica regime `trend/sell`.
- A janela do coordenador é `06:00-19:00 UTC`, com uma posição compartilhada por símbolo.
- É o único `bot_id` permitido pelo guardião real: `core-portfolio`.
- A promoção final ainda exige todas as travas descritas em `docs/safety-controls.md`.

## Limites da reproducao

Estas regras são uma reconstrução baseada no histórico observado, não o código do fornecedor. O
comportamento real de operações com duração de segundos depende de ticks, spread, slippage,
latência, stops mínimos e especificações do contrato da corretora. Resultados históricos e shadow
não garantem desempenho futuro.

## Onde acompanhar

- `/bots`: visão consolidada e curva comparativa.
- `/compare`: métricas lado a lado dos quatro perfis.
- `/bots/{bot_id}`: replay, candles, operações e validação de cada estratégia.
- `/operations`: heartbeat, latência, candle mais recente e erros operacionais.
