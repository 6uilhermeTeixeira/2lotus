# Runtime simultâneo: demo e real

## Topologia

| Ambiente | MT5 | API | Conta | Execução inicial |
| --- | --- | --- | --- | --- |
| Demo | `mt5-wine`, display `:5`, noVNC `6085` | `2lotus:1112` | demo existente | configuração atual; revisar preset |
| Real | `mt5-live-wine`, display `:6`, noVNC `6086` | `live-core:1111` | ainda não conectada | obrigatoriamente desativada |

As instalações, perfis Wine, displays, portas VNC, EAs, chaves, stores e logs não podem ser
compartilhados. O terminal real deve usar `LiveCoreBridgeEA` e apontar apenas para a porta 1111.

## Provisionamento e handoff

1. Inicie os serviços `live-core-mt5-display`, `live-core-mt5-desktop`,
   `live-core-mt5-terminal`, `live-core-mt5-vnc` e `live-core-mt5-web`.
2. Acesse localmente `http://127.0.0.1:6086/vnc.html` e conclua a instalação se solicitado.
3. Execute `scripts/install-live-core-expert.sh` após a instalação do terminal.
4. Entregue login, servidor e senha por `/run/user/$UID/live-core-mt5-login.txt`, modo 600, uma
   linha por campo. Reinicie somente `live-core-mt5-terminal.service`; o arquivo será apagado.
5. Configure o EA com URLs `http://127.0.0.1:1111`, login permitido e carregue
   `live-core-safe.set`. O preset nasce com `EnableOrderExecution=false`,
   `EnableResultReporting=false`, `AllowedAccountLogin=0` e sem chave de ingestão.
6. Valide heartbeat, conta, símbolo e readiness antes de considerar qualquer canário.

Nunca copie `accounts.dat`, presets, configuração ou credenciais do prefixo demo.

## Portas locais

- Demo: API `1112`, VNC `5905`, noVNC `6085`.
- Real: API `1111`, VNC `5906`, noVNC `6086`.

Todas permanecem vinculadas a `127.0.0.1`. Para acesso remoto, use um túnel SSH; não exponha
VNC, noVNC ou as APIs diretamente à internet.
