# API de Consulta de Rastreio

Página self-service de rastreio para a loja Shopify: o cliente informa **email da
compra + número do pedido**, a API valida esses dados na Shopify e devolve o
status de entrega vindo da Frete Rápido.

## Como funciona

1. Valida email + número do pedido na **Shopify Admin API** (GraphQL)
2. Identifica qual dos **3 CNPJs** despachou, pela *tag* do pedido
3. Consulta as ocorrências na **Frete Rápido** com o token daquele CNPJ
4. Devolve status atual, histórico, transportadora e previsão de entrega

A API é **somente leitura** nas duas integrações: nunca cria, altera ou cancela
nada.

### Aviso proativo (webhook da Frete Rápido)

O caminho acima é *pull*: só descobre o status quando o cliente vem perguntar.
Existe também um caminho *push*, que **não substitui** o anterior — a página
segue funcionando para quem consulta espontaneamente.

A Frete Rápido faz `POST` numa rota nossa a cada atualização de ocorrência.
Quando a ocorrência exige ação do cliente — encomenda **aguardando retirada** ou
**tentativa de entrega frustrada**, os dois casos em que a encomenda volta para a
loja se ninguém agir — a API resolve o pedido na Shopify e manda o n8n enviar um
WhatsApp.

```
Frete Rápido ──POST──▶ /api/v1/webhook/frete-rapido/{segredo}
                          │  confere segredo · filtra (LGPD) · classifica
                          │  decide gatilho · deduplica · trava anti-spam
                          │  resolve pedido na Shopify → telefone + nome
                          └──POST──▶ n8n ──▶ WhatsApp
```

**A API decide, o n8n entrega.** A decisão fica aqui porque três coisas não podem
regredir em silêncio: a lista de permissão LGPD (o payload traz comprovante com
assinatura de terceiro e chave de acesso de NF-e), o mapa de ~350 códigos de
`services/ocorrencias.py`, e a deduplicação — a Frete Rápido reenvia o mesmo
evento até 12 vezes em ~24 h enquanto não receber HTTP 200, e sem a restrição
`UNIQUE` do banco o cliente receberia 12 mensagens idênticas.

Quais status disparam mensagem é **configurável** (`NOTIFICACAO_GRUPOS`, com
válvulas de escape por código). Ver `.env.example`.

> **Estado atual: Fase 1 — modo observação.** Com `NOTIFICACAO_ATIVA=false` a API
> percorre o caminho inteiro e **grava sem enviar nada**. Isso mede, com dado
> real, quais ocorrências acontecem de fato, quantas mensagens sairiam por dia e
> quantos pedidos têm telefone utilizável. Os relatórios 9 a 12 de
> `deploy/monitor.sql` respondem essas perguntas — decida os gatilhos com eles
> antes de ligar o envio.

## Rodando localmente

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy .env.example .env    # preencher as credenciais
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Documentação interativa em `http://localhost:8000/docs` (desabilitada em
produção).

Com banco:

```bash
docker compose up -d db
.venv\Scripts\python.exe -m alembic upgrade head
```

## Validação contra as APIs reais

```bash
.venv\Scripts\python.exe scripts/validar_integracoes.py [numero_pedido] [email]
```

Verifica Shopify, Frete Rápido, fuso horário, fluxo completo e vazamento de
existência de pedido. Não imprime segredo algum.

## Testes

```bash
.venv\Scripts\python.exe -m pytest -q          # unitários e de integração
.venv\Scripts\python.exe -m mypy app           # tipos, modo estrito
.venv\Scripts\python.exe -m ruff check app tests scripts
```

Testes marcados com `contrato` batem nas APIs reais e exigem segredos; ficam
fora do CI.

## Decisões que não são óbvias

**Normalização do número do pedido.** O que a Shopify chama de `name` nesta loja
é `59552`, sem prefixo. O `#` que o cliente eventualmente digita é tolerado na
entrada, mas **nunca** reconstruído na busca — são coisas diferentes: afixo da
loja versus tolerância de digitação.

**O código do fulfillment não é exibido.** O ERP grava ali o `id_frete` da Frete
Rápido (`FR260723D6KTG`), que não é rastreável no site da transportadora.
Mostrá-lo faria o cliente tentar usá-lo na Jadlog, falhar e procurar o suporte.

**Rótulo vem da API, grupo vem do código.** Não traduzimos os 250+ códigos de
ocorrência: o campo `nome` da Frete Rápido já é legível. Nosso mapa define
apenas o grupo visual, e código desconhecido cai num grupo neutro em vez de
quebrar.

**Falha fechada no multi-CNPJ.** Se um token falhar e nenhum devolver dados, a
resposta é erro — nunca "sem rastreio". O token que falhou pode ser justamente o
que tinha as ocorrências.

**`workers=1` é deliberado.** O rate limit conta em memória, por processo. Com N
workers o limite efetivo seria N vezes maior, desligando na prática a defesa
principal contra enumeração de pedidos.

**CORS não é segurança.** Restringe apenas navegadores; qualquer script o ignora.
A proteção real é o rate limiting mais a validação de email.

**O webhook da Frete Rápido não é assinado** — e a resposta a isso não é um
segredo melhor, é verificar o dado.

Não há HMAC nem lista de IPs publicada. Há dois segredos (o do caminho da URL e um
Bearer token), mas ambos provam apenas que quem chamou *os conhece* — não que o
evento aconteceu. Por isso, antes de mandar qualquer mensagem, a API **pergunta à
própria Frete Rápido** se aquele pedido tem mesmo aquela ocorrência. Evento
forjado não sobrevive à confirmação.

Isso resolve o problema sem depender de IP fixo nem de nada que o fornecedor
precise conceder, e de quebra pega erro de operação — URL trocada entre os três
cadastros, número de pedido errado no payload.

A confirmação roda **antes** da consulta à Shopify, o que é decisão de
privacidade além de segurança: não buscamos o telefone de ninguém com base num
evento que ainda não sabemos se é real.

Duas sutilezas que uma revisão de segurança externa expôs, e que valem registro
porque a versão ingênua de cada uma parecia suficiente:

**A pergunta é sobre o estado ATUAL, não sobre o histórico.** O endpoint devolve
todas as ocorrências do pedido, então "este código existe?" confirmava para
sempre um código de dias atrás — bastava reproduzir um "disponível para retirada"
antigo num pedido já entregue para mandar o cliente à agência à toa. Comparamos
com `ordenar_desc()[0]`, o mesmo critério de status atual que a página usa.

**O conteúdo da mensagem vem da ocorrência confirmada, nunca do corpo do
webhook.** A versão anterior confirmava o *gatilho* e copiava `nome`, `mensagem`
e `transportadora` do payload — quem tivesse o segredo escrevia o texto que
chegava no WhatsApp do cliente. Hoje o webhook só avisa que algo mudou; tudo que
o cliente lê sai da API da Frete Rápido, da nossa classificação, ou da Shopify.

**Telefone é recusado, nunca "consertado".** Fixo e celular antigo de 8 dígitos
viram `sem_contato` em vez de ganharem um nono dígito na marra. Inventar o dígito
produz um número plausível que pode ser de outra pessoa — e aqui o custo do erro
não é uma consulta vazia, é mandar dados de um pedido para um desconhecido.

**O contato vem do endereço de entrega, não do cadastro do cliente.** Não é
preferência: `customer { phone firstName }` exige o escopo `read_customers`, e a
Shopify não devolve o campo como nulo — ela nega a **consulta inteira** com
`ACCESS_DENIED`, o que derrubaria a página de rastreio junto. O `shippingAddress`
traz telefone e nome, cobre 98,8% dos pedidos despachados (medido em 03/08/2026)
e não precisa de escopo novo. `tests/test_shopify.py` tem a regressão.

**Uma ocorrência por volume não vira uma mensagem por volume.** Observado em
produção: o pedido 60422 gerou quatro ocorrências do mesmo código em 21 minutos,
com datas genuinamente distintas — uma por caixa da remessa. A deduplicação
normal não pega, porque a chave inclui a data. Por isso vale uma regra a mais:
dentro da janela anti-spam, o mesmo pedido com o mesmo código avisa **uma vez**.
A janela é o que mantém correto o caso oposto — "destinatário ausente" na segunda
e na quarta são duas tentativas de entrega diferentes, e o cliente deve saber das
duas.

**`sem_contato` responde 200, não 503.** É desfecho terminal, não falha: insistir
gastaria as 12 tentativas da Frete Rápido num evento que nunca poderá ser
entregue. O 503 fica reservado para o que *vale* reenviar — Shopify ou n8n fora
do ar.

## LGPD

- Email gravado **apenas como HMAC-SHA256**; os dados são *pseudonimizados*, não
  anônimos
- Retenção de 90 dias em `consulta_log`, com expurgo diário
- O cache guarda somente campos de uma lista de permissão — CPF/CNPJ e nome do
  entregador, presentes no payload da Frete Rápido, são descartados
- A mesma lista de permissão vale para o **webhook**, que traz ainda
  `comprovantes[].url_imagem` (canhoto com assinatura de quem recebeu),
  `notas_fiscais[].chave_acesso` e `metadados` arbitrários do ERP — nada disso
  sobrevive ao parsing
- `evento_frete` **não tem dado pessoal algum**, nem pseudonimizado: telefone e
  nome são lidos da Shopify no momento do envio e descartados
- Nenhuma URL é registrada: o token da Frete Rápido trafega na query string

**Fronteira com o n8n.** O único ponto onde dado pessoal sai daqui é o payload
enviado ao n8n — telefone e primeiro nome, nada mais. O n8n grava os dados de
execução no banco dele, **fora do alcance de `scripts/expurgar.py`**: alinhe o
`EXECUTIONS_DATA_MAX_AGE` / pruning dele com a retenção de 90 dias adotada aqui.

```bash
.venv\Scripts\python.exe scripts/expurgar.py    # cron diário
```

A retenção só se completa após o ciclo de backup — cópias antigas ainda contêm
os registros apagados, então a retenção dos backups precisa ser curta.
