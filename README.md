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

## LGPD

- Email gravado **apenas como HMAC-SHA256**; os dados são *pseudonimizados*, não
  anônimos
- Retenção de 90 dias em `consulta_log`, com expurgo diário
- O cache guarda somente campos de uma lista de permissão — CPF/CNPJ e nome do
  entregador, presentes no payload da Frete Rápido, são descartados
- Nenhuma URL é registrada: o token da Frete Rápido trafega na query string

```bash
.venv\Scripts\python.exe scripts/expurgar.py    # cron diário
```

A retenção só se completa após o ciclo de backup — cópias antigas ainda contêm
os registros apagados, então a retenção dos backups precisa ser curta.
