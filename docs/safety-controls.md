# Protecoes de execucao real

Este documento descreve as protecoes obrigatorias do canario real. Todas seguem o principio
`fail closed`: valor ausente, dependencia indisponivel ou validacao negativa resulta em `hold`
ou impede a inicializacao/execucao do EA.

## Matriz de protecoes

| Protecao | Camada | Configuracao ou validacao | Estado inicial | Comportamento de bloqueio |
| --- | --- | --- | --- | --- |
| Conta MT5 permitida por login | API e EA | `LIVE_CANARY_ACCOUNT_LOGIN`, `AllowedAccountLogin` e `account.login` | Login `0`, portanto invalido | API mantem `hold`; EA falha no `OnInit` se a execucao estiver ligada e o login nao coincidir |
| Lote maximo de `0.01` | API e EA | `LIVE_CANARY_MAX_LOT` e `MaxLiveLot` | `0.01` | API limita o tamanho promovido; EA aplica novamente `MathMin` e recusa configuracao acima de `0.01` |
| Uma entrada por dia | EA | `MaxLiveEntriesPerDay=1`, historico diario, simbolo e `StrategyMagicNumber` | Uma entrada | EA conta deals de entrada do dia e bloqueia uma nova ordem ao atingir o limite |
| Uma posicao simultanea | API e EA | Posicoes do payload e `PositionSelect(_Symbol)` | Nenhuma exposicao adicional permitida | API mantem `hold` quando recebe exposicao; EA sempre consulta a posicao local antes de enviar a ordem |
| Limite de spread | API e EA | `LIVE_CANARY_MAX_SPREAD_POINTS` e `MaxLiveSpreadPoints` | `15` pontos | API mantem `hold`; EA rele o tick imediatamente antes da ordem e cancela a execucao |
| SL e TP obrigatorios | API | `stop_loss_price` e `take_profit_price` precisam ser positivos | Obrigatorios | O candidato nao e promovido e permanece `hold` |
| Persistencia Supabase obrigatoria | API | `DAYTRADE_RESULTS_STORE=SUPABASE`, credenciais validas e estado persistente carregado | Nao configurada | Sem store persistente, o guardiao mantem `hold`; falha de dependencia retorna erro e nao libera ordem |
| Duas chaves independentes na API | API | `DAYTRADE_ENABLE_LIVE_DECISIONS=true` e `LIVE_CANARY_ARMED=true` | Ambas `false` | Qualquer uma desligada mantem `hold` |
| Chave local adicional | EA | `EnableOrderExecution=true` | `false` | O EA consulta e registra sinais, mas nao chama a rotina de execucao |
| Kill switch pelo servidor | API | Alterar `LIVE_CANARY_ARMED=false` e reiniciar/reimplantar | Desarmado | Respostas voltam imediatamente a `hold`, mesmo que o EA continue habilitado localmente |
| Execucao nasce desativada | API e EA | Valores versionados em `.env.example` e inputs padrao do EA | Desativada | Sao necessarias ativacoes explicitas e independentes em ambas as camadas |

## Observacoes de implementacao

- Somente `core-portfolio` esta na allowlist de execucao. Os tres componentes sao consultados
  exclusivamente em shadow.
- A verificacao de posicao na API depende de o cliente enviar `positions`. O EA possui a verificacao
  local autoritativa com `PositionSelect`, portanto nao depende desse payload para impedir duplicidade.
- O limite diario do EA usa o horario do servidor da corretora e conta apenas entradas com o mesmo
  simbolo e `StrategyMagicNumber`.
- O limite de spread e expresso em pontos do simbolo, nao em dolares nem em pips normalizados.
- Desabilitar Algo Trading no terminal e o corte local imediato. O kill switch da API e o corte remoto.

## Fontes no codigo

- Guardiao da API: `python-service/app/live_guard.py`
- Configuracoes e valores padrao: `python-service/app/config.py` e `.env.example`
- Integracao do guardiao ao endpoint: `python-service/app/main.py`
- Travas locais e envio de ordens: `mt5/experts/LiveCoreBridgeEA.mq5`
- Testes fail-closed: `python-service/tests/test_live_guard.py`

## Procedimento de desarme

1. Defina `LIVE_CANARY_ARMED=false` e reinicie ou reimplante a API.
2. Confirme que uma consulta candidata retorna `action=hold`.
3. Defina `EnableOrderExecution=false` no EA ou remova o EA do grafico.
4. Para corte imediato, desabilite Algo Trading no terminal MT5.
5. Posicoes ja abertas nao sao fechadas automaticamente pelo kill switch; devem ser gerenciadas
   conforme o plano operacional da posicao.
