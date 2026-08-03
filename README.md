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

**O webhook da Frete Rápido não é assinado.** Não há HMAC, cabeçalho assinado nem
lista de IPs publicada na documentação deles. O segredo no caminho da URL é a
única barreira — daí ele ser comparado com `compare_digest`, ter tamanho mínimo
exigido no boot e ser redigido no log (o uvicorn registra o caminho de *toda*
requisição). A segunda camada é tratar o payload como não-confiável: o
`numero_pedido` é sempre resolvido contra a Shopify antes de qualquer envio, o
que limita o estrago de um segredo vazado a "mensagem sobre um pedido real para o
dono real dele".

**Telefone é recusado, nunca "consertado".** Fixo e celular antigo de 8 dígitos
viram `sem_contato` em vez de ganharem um nono dígito na marra. Inventar o dígito
produz um número plausível que pode ser de outra pessoa — e aqui o custo do erro
não é uma consulta vazia, é mandar dados de um pedido para um desconhecido.

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
