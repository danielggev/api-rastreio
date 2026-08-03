# Deploy no VPS

Ao final você terá a API no ar com HTTPS, backup diário e expurgo LGPD agendado.

## Qual caminho seguir

Antes de tudo, veja se algo já ocupa as portas 80 e 443:

```bash
ss -tlnp | grep -E ':80|:443'
```

| Resultado | Caminho |
|---|---|
| Portas livres | **[Caminho A](#caminho-a--servidor-limpo-com-caddy)** — subimos o Caddy junto |
| Um proxy já rodando (Traefik, Nginx…) | **[Caminho B](#caminho-b--vps-que-já-tem-traefik)** — a API entra atrás dele |

Instalar um segundo proxy num servidor que já tem um **derruba o que estiver no
ar**. No VPS da Hostinger com template de n8n, por exemplo, o Traefik já detém
as duas portas — nesse caso use o Caminho B.

---

## Caminho B — VPS que já tem Traefik

Este é o caso do VPS da Hostinger com n8n instalado. **Não instale o Caddy**: o
Traefik já detém as portas e já emite certificado. A API entra como mais um
serviço, descoberto por rótulos.

### B1. Domínio

A Hostinger fornece um hostname com **wildcard DNS** — qualquer subdomínio já
resolve para o VPS, sem configurar nada:

```bash
# no seu computador, confirme:
nslookup rastreio.srv1835728.hstgr.cloud
```

Use `rastreio.srv<SEU-NUMERO>.hstgr.cloud`. Nada de sslip.io nem de comprar
domínio.

### B2. Código e configuração

```bash
mkdir -p /opt/rastreio && cd /opt/rastreio
git clone <URL-DO-REPO> .

cp .env.example .env
chmod 600 .env
nano .env
```

Preencha (ver [Passo 3](#passo-3--configurar-as-variáveis) para a lista completa):

```ini
ENV=production
API_DOMINIO=rastreio.srv1835728.hstgr.cloud
POSTGRES_PASSWORD=<gere um valor novo>
EMAIL_HMAC_KEY=<gere um valor novo>
CORS_ORIGINS=https://www.grudadoemvoce.com.br,https://grudadoemvoce.com.br
DEMO_MODE=false
```

### B3. Subir

```bash
docker compose --env-file .env -f deploy/docker-compose.traefik.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.traefik.yml exec api alembic upgrade head
```

Confira que o n8n continua no ar — nada deve ter sido afetado:

```bash
docker ps
```

### B4. Verificar

O certificado leva de 30 segundos a 2 minutos para ser emitido na primeira vez.

```bash
curl https://rastreio.srv1835728.hstgr.cloud/health
# {"status":"ok"}
```

Se falhar, veja o que o Traefik está dizendo:

```bash
docker logs traefik-traefik-1 --tail 50
```

Siga para o [Passo 7](#passo-7--verificar) para as demais verificações e para o
[Passo 8](#passo-8--agendar-backup-e-expurgo) para backup e expurgo — trocando
`deploy/docker-compose.prod.yml` por `deploy/docker-compose.traefik.yml` nos
comandos.

---

## Caminho A — servidor limpo (com Caddy)

### Por que a API precisa de HTTPS e de um domínio

A loja roda em HTTPS, e navegadores **bloqueiam** chamadas de uma página HTTPS
para endereços HTTP — o formulário simplesmente não funcionaria. Certificado
HTTPS, por sua vez, não é emitido para IP puro, só para domínio.

### O domínio, sem mexer no DNS da loja

Sem acesso ao Registro.br, usamos o **sslip.io**: ele resolve qualquer IP sem
cadastro nenhum. Troque os pontos do IP do VPS por hífens:

| IP do VPS | Domínio |
|---|---|
| `203.0.113.10` | `203-0-113-10.sslip.io` |

Descubra o IP dentro do servidor:

```bash
curl -s ifconfig.me
```

E confirme que resolve (deve devolver o próprio IP):

```bash
dig +short 203-0-113-10.sslip.io
```

Não há propagação a esperar — o sslip.io responde na hora.

> **Isso é temporário e reversível.** O domínio vive na variável `API_DOMINIO`
> do `.env`. Quando você conseguir acesso ao DNS, troca por
> `api.grudadoemvoce.com.br`, reinicia o Caddy e pronto — nenhum outro arquivo
> muda. O único inconveniente hoje é o IP do servidor ficar visível no
> JavaScript da página. Não é falha de segurança, mas é deselegante.

### Portas

Libere **80 e 443** no firewall. A 80 é obrigatória: o Let's Encrypt a usa para
validar o domínio, mesmo que o site só atenda em 443.

---

## Passo 1 — Instalar Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker          # aplica o grupo sem precisar deslogar

docker --version && docker compose version
```

## Passo 2 — Colocar o código no servidor

```bash
sudo mkdir -p /opt/rastreio && sudo chown "$USER" /opt/rastreio
cd /opt/rastreio
git clone <URL-DO-REPO> .
```

## Passo 3 — Configurar as variáveis

```bash
cp .env.example .env
chmod 600 .env         # o arquivo tem segredos; só o dono deve ler
nano .env
```

Preencha:

```ini
ENV=production

# Domínio público da API. Com sslip.io, é o IP do VPS com hífens.
API_DOMINIO=203-0-113-10.sslip.io

SHOPIFY_SHOP_DOMAIN=grudadoemvoce.myshopify.com
SHOPIFY_CLIENT_ID=<do Dev Dashboard>
SHOPIFY_CLIENT_SECRET=<do Dev Dashboard>
SHOPIFY_API_VERSION=2026-07
SHOPIFY_ORDER_PREFIX=
SHOPIFY_ORDER_SUFFIX=

FRETE_RAPIDO_TOKENS={"grudado":"...","melhores":"...","tudo":"..."}
FRETE_RAPIDO_TIMEZONE=America/Sao_Paulo

EMAIL_HMAC_KEY=<gere um valor novo, ver abaixo>
POSTGRES_PASSWORD=<gere um valor novo>

# Domínios da loja que podem chamar a API
CORS_ORIGINS=https://www.grudadoemvoce.com.br,https://grudadoemvoce.com.br

RATE_LIMIT=10/minute
DEMO_MODE=false
```

Gerando os segredos:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # EMAIL_HMAC_KEY
openssl rand -hex 24                                             # POSTGRES_PASSWORD
```

> **A senha do Postgres precisa ser segura em URL.** Ela é interpolada na
> `DATABASE_URL`, e caracteres como `/`, `+` ou `@` quebram o parser — a conexão
> falha com um erro que não aponta para a causa. Por isso hexadecimal, e não
> `base64`.

> **A `EMAIL_HMAC_KEY` não pode mudar depois.** Trocá-la invalida a correlação de
> todos os logs anteriores — os hashes antigos deixam de bater com os novos.

Não preencha `TRUSTED_PROXIES` aqui: o compose de produção já define a faixa
interna do Docker.

## Passo 4 — (nada a fazer)

O Caddyfile lê o domínio de `API_DOMINIO`, que você já preencheu. Nenhum arquivo
precisa ser editado.

## Passo 5 — Subir

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.prod.yml ps
```

## Passo 6 — Criar as tabelas

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec api alembic upgrade head
```

## Passo 7 — Verificar

```bash
curl https://203-0-113-10.sslip.io/health
# {"status":"ok"}

# /docs precisa estar FECHADO em produção
curl -o /dev/null -w "%{http_code}\n" https://203-0-113-10.sslip.io/docs
# 404

# Consulta real
curl -s -X POST https://203-0-113-10.sslip.io/api/v1/rastreio \
  -H "Content-Type: application/json" \
  -d '{"email":"email-do-pedido@exemplo.com","numero_pedido":"59551"}'
```

### O IP real está chegando?

Este é o item mais importante da verificação. Se o proxy estiver mal
configurado, **todos os visitantes contam como um único IP** e o rate limit —
a defesa principal contra enumeração de pedidos — se desliga sem emitir erro.

```bash
# Faça 12 consultas seguidas: as duas últimas devem devolver 429
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code} " -X POST \
    https://203-0-113-10.sslip.io/api/v1/rastreio \
    -H "Content-Type: application/json" \
    -d '{"email":"teste@exemplo.com","numero_pedido":"1"}'
done; echo
```

E confirme no banco que o IP gravado é público, não `172.x` nem `127.0.0.1`:

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec db \
  psql -U rastreio -d rastreio -c \
  "SELECT ip_origem, resultado, criado_em FROM consulta_log ORDER BY id DESC LIMIT 5;"
```

Se aparecer IP interno, o `X-Forwarded-For` não está chegando — revise o
`reverse_proxy` no Caddyfile e o `TRUSTED_PROXIES` no compose.

## Passo 8 — Agendar backup e expurgo

```bash
chmod +x deploy/backup.sh deploy/testar-restauracao.sh
crontab -e
```

```cron
# Backup diário às 3h
0 3 * * * /opt/rastreio/deploy/backup.sh >> /var/log/rastreio-backup.log 2>&1

# Expurgo LGPD (90 dias) às 4h
0 4 * * * cd /opt/rastreio && docker compose --env-file .env -f deploy/docker-compose.prod.yml exec -T api python scripts/expurgar.py >> /var/log/rastreio-expurgo.log 2>&1
```

**Teste a restauração uma vez** — backup nunca verificado costuma não funcionar,
e a hora de descobrir isso não é quando você precisa dele:

```bash
./deploy/backup.sh
./deploy/testar-restauracao.sh /opt/rastreio/backups/rastreio-*.sql.gz
```

O teste restaura numa base temporária e apaga em seguida. Não toca em produção.

## Passo 9 — Apontar a página

No `assets/js/rastreio.js` do tema:

```js
var API = window.RASTREIO_API || 'https://203-0-113-10.sslip.io/api/v1/rastreio';
```

---

## Checklist antes de liberar aos clientes

- [ ] `https://.../health` responde `{"status":"ok"}`
- [ ] `/docs` devolve **404**
- [ ] Consulta com pedido real funciona
- [ ] E-mail errado e pedido inexistente devolvem **resposta idêntica**
- [ ] 12 consultas seguidas resultam em `429`
- [ ] `ip_origem` no banco é **IP público**, não interno
- [ ] Nenhum e-mail em claro: `SELECT email_hmac, ip_origem FROM consulta_log LIMIT 5`
- [ ] Token da Frete Rápido não aparece nos logs: `docker compose logs api | grep -i token`
- [ ] Backup roda e a **restauração foi testada**
- [ ] Cron do expurgo agendado
- [ ] `CORS_ORIGINS` só com os domínios da loja
- [ ] `.env` com permissão `600`
- [ ] Política de privacidade da loja menciona a consulta de rastreio

---

## Migrando para um domínio próprio depois

Quando conseguir acesso ao DNS (ou registrar um domínio novo):

1. Crie o registro `A` apontando para o IP do VPS
2. Confirme: `dig +short api.grudadoemvoce.com.br`
3. Troque uma linha no `.env`:

```ini
API_DOMINIO=api.grudadoemvoce.com.br
```

4. Reinicie o Caddy — ele emite o novo certificado sozinho:

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d caddy
```

5. Atualize o `assets/js/rastreio.js` do tema com o novo endereço

O endereço antigo do sslip.io continua funcionando enquanto o certificado for
válido, então dá para trocar sem janela de indisponibilidade.

---

## Se o certificado falhar

O sslip.io é compartilhado por muita gente, e o Let's Encrypt impõe limites por
domínio registrado. Se o Caddy não conseguir emitir:

```bash
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs caddy | grep -i "certificate\|acme\|rate"
```

Aparecendo erro de limite (`too many certificates`), as saídas são:

- **Esperar** — os limites do Let's Encrypt são semanais
- **Trocar para ZeroSSL**, acrescentando ao topo do Caddyfile:

```
{
	acme_ca https://acme.zerossl.com/v2/DV90
}
```

- **Registrar um domínio próprio** (R$ 40/ano no Registro.br) — resolve de vez

---

## Operação

```bash
# Logs
docker compose --env-file .env -f deploy/docker-compose.prod.yml logs -f api

# Reiniciar
docker compose --env-file .env -f deploy/docker-compose.prod.yml restart api

# Atualizar o código
git pull && docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.prod.yml exec api alembic upgrade head
```

### O que observar nos logs

| Sinal | Significado |
|---|---|
| `vazio_fr` frequente | Pedido despachado que a Frete Rápido não conhece — número divergente do ERP |
| `tag_cnpj_ausente` | Pedido sem tag de empresa: a busca consultou os 3 tokens |
| `cnpj_divergente_da_tag` | Pedido marcado com a empresa errada |
| `ACCESS_DENIED` | Escopo `read_orders` faltando ou app desinstalado |
| `nao_encontrado` alto | Agrega digitação errada, e-mail diferente **e** pedidos com mais de 60 dias |

Consulta útil para a taxa de anomalias na última hora:

```sql
SELECT resultado, count(*)
FROM consulta_log
WHERE criado_em > now() - interval '1 hour'
GROUP BY resultado ORDER BY 2 DESC;
```

---

## Webhook da Frete Rápido — aviso proativo

Duas fases. **Não pule a Fase 1**: é ela que transforma a escolha dos gatilhos
numa decisão informada, e ela não fala com cliente nenhum.

### Passo 0 — o telefone está disponível?

**Faça isto antes de qualquer outra coisa.** É a pendência com mais chance de
mudar o desenho, e leva um minuto:

```bash
.venv\Scripts\python.exe scripts/checar_telefone.py
```

Somente leitura — pode rodar contra produção. Ele mede a cobertura usando
`normalizar_telefone_br`, a **mesma** função do caminho de produção, então o
número que ele reporta é o número que você terá de verdade. Um levantamento por
GraphiQL diria "preenchido", que é outra pergunta: fixo e celular antigo de 8
dígitos não recebem WhatsApp.

Dois desfechos exigem ação:

- **`ACCESS_DENIED`, ou todos os telefones vindo nulos** → falta *protected
  customer data*. Shopify admin → Apps → seu app → API access → "Protected
  customer data access". Enquanto não sair, o telefone tem que vir da Frete
  Rápido (`GET quote/{id_frete}`).
- **Cobertura abaixo de ~60%** → só com a Shopify a maioria dos clientes ficaria
  sem aviso. Implemente a segunda fonte antes de ligar o envio na Fase 2.

O restante da Fase 1 pode seguir em paralelo: o modo observação funciona mesmo
sem telefone nenhum — os eventos entram como `sem_contato`, o que já é a
medição.

> **Medido em 03/08/2026** na loja real, 250 pedidos despachados dos últimos 30
> dias: **98,8% com telefone utilizável**, todos vindos de
> `shippingAddress.phone`. As 3 exceções foram 2 números estrangeiros e 1 campo
> vazio. Nome do cliente disponível em 99,6%.
>
> **A segunda fonte na Frete Rápido não é necessária.** E o app **não** precisa
> de *protected customer data* nem de `read_customers` — o `read_orders` que ele
> já tem alcança o `shippingAddress`.
>
> Isso vale enquanto a consulta **não** pedir `customer { … }`. Esse campo exige
> `read_customers`, e a Shopify nega a *consulta inteira* com `ACCESS_DENIED` —
> derrubando junto a página de rastreio, que é o fluxo principal. Há teste de
> regressão para isso em `tests/test_shopify.py`.

### Fase 1 — modo observação

**1. Gerar o segredo.** Ele é a única barreira: a Frete Rápido não assina o
payload.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**2. Configurar no `.env`** (envio desligado de propósito):

```bash
FR_WEBHOOK_SEGREDO=<o valor gerado acima>
NOTIFICACAO_ATIVA=false
NOTIFICACAO_GRUPOS=aguardando_retirada,tentativa_falha
```

**3. Subir e migrar:**

```bash
docker compose --env-file .env -f deploy/docker-compose.traefik.yml up -d --build
docker compose --env-file .env -f deploy/docker-compose.traefik.yml exec api alembic upgrade head
```

Confirme nos logs a linha que declara a configuração efetiva — é assim que se
responde "quais status estão ativos agora" sem abrir o `.env` do servidor:

```
webhook da Frete Rapido ativo | envio=DESLIGADO (modo observacao) | grupos=[...]
```

**4. Testar antes de cadastrar na Frete Rápido:**

```bash
curl -i -X POST "https://$API_DOMINIO/api/v1/webhook/frete-rapido/$FR_WEBHOOK_SEGREDO" \
  -H 'Content-Type: application/json' \
  -d @tests/fixtures/webhook-ocorrencia-232.json
```

Espere `200` com `{"status":"observado",...}`. Segredo errado devolve `404`.

**5. Confirmar que o segredo não vaza no log** — se aparecer em claro, a camada
de segurança principal está furada:

```bash
docker compose --env-file .env -f deploy/docker-compose.traefik.yml logs api | grep 'frete-rapido'
# deve mostrar /webhook/frete-rapido/***
```

**6. Cadastrar a URL no Dash FR.** Não há API para isso — é o formulário
"Cadastro de Webhook", com três abas. Faça **só depois** dos passos 4 e 5: a
Frete Rápido **não reenvia em 404**, então eventos que chegarem antes do deploy
são perdidos em silêncio.

> A documentação pública diz que não há autenticação. **Tem.** O formulário
> oferece Basic (usuário/senha), Bearer token e headers avulsos. Verificado no
> painel em 03/08/2026.

*Aba **URL***

| Campo | Valor |
|---|---|
| Nome | `Ocorrencias - rastreio` (livre) |
| Webhook | `https://SEU_DOMINIO/api/v1/webhook/frete-rapido/<FR_WEBHOOK_SEGREDO>` |
| Tipo | **Ocorrência** |
| Usuário / Senha | vazio |
| Bearer token | o valor de `FR_WEBHOOK_BEARER` |

*Aba **Headers*** — deixar vazia. O Bearer da aba URL já cobre; header avulso
seria uma terceira barreira sem ganho.

*Aba **Configurações de Disparo*** — duas decisões que não são óbvias:

- **Lista de ocorrências: deixar TUDO desmarcado.** O próprio painel avisa que
  sem filtro "o disparo será enviado considerando todos os cenários", e é
  exatamente isso que a Fase 1 precisa. Marcar as ocorrências aqui **cegaria a
  medição**: o relatório 9 do `monitor.sql` existe para mostrar o que de fato
  acontece, e ele só mostraria o que você já tivesse pré-selecionado — circular
  e inútil. Depois da Fase 1 dá para estreitar aqui, se o volume incomodar.
- **"Incluir fretes do tipo reversa?": deixar DESLIGADO.** Frete reverso é a
  mercadoria voltando *para a loja*. Uma reversa em estado "disponível para
  retirada" é a **loja** que precisa buscar — e o aviso iria para o cliente,
  dizendo para ele retirar um pacote que está voltando. Os códigos de reversa já
  caem no grupo `devolucao`, que não dispara; desligar aqui é a segunda camada
  contra uma mensagem constrangedora.
- **Canal: deixar vazio** — vazio significa todos os canais.

Como a operação usa **3 CNPJs**, confirme com o suporte se o cadastro é por CNPJ
ou por conta, e se os três podem apontar para a mesma URL. Aproveite e peça as
**faixas de IP de origem** — com elas dá para fechar a rota no Traefik, a camada
de segurança mais forte disponível.

**7. Deixar rodando de uma a duas semanas** e então:

```bash
./deploy/monitor.sh
```

Os relatórios 9 a 12 respondem, com dado real: quais ocorrências de fato chegam
(→ define `NOTIFICACAO_GRUPOS`), quantas mensagens sairiam por dia (→ dimensiona
o custo) e qual a taxa de `sem_contato` (→ decide se vale buscar o telefone na
própria Frete Rápido como segunda fonte).

### Fase 2 — ligar o envio

1. Montar o fluxo no n8n e apontar `N8N_WEBHOOK_URL` + `N8N_WEBHOOK_TOKEN`
2. Ajustar `NOTIFICACAO_GRUPOS` conforme os dados da Fase 1
3. Alinhar a retenção do n8n (`EXECUTIONS_DATA_MAX_AGE`) com os 90 dias daqui —
   o telefone do cliente fica retido lá, fora do `scripts/expurgar.py`
4. Usar o **reprocessamento manual do Dash FR** para reenviar uma ocorrência real
   e acompanhar o caminho inteiro
5. Só então `NOTIFICACAO_ATIVA=true`, com `NOTIFICACAO_MAX_POR_PEDIDO` baixo nos
   primeiros dias

**Interruptor de emergência:** `NOTIFICACAO_ATIVA=false` + `restart api`. Os
eventos continuam sendo gravados; nada é enviado.

### O que observar

| Sinal | Significado |
|---|---|
| `sem_contato` alto | O telefone da Shopify não basta — avaliar a segunda fonte (`GET quote/{id_frete}`) |
| `pendente` há mais de 24 h | A Frete Rápido esgotou as 12 tentativas e o aviso **não saiu**. Ver a coluna `erro` |
| `limite anti-spam` no log | Rajada de códigos no mesmo pedido, ou segredo vazado |
| `webhook para pedido inexistente` | Webhook forjado, ou pedido com mais de 60 dias (some da API da Shopify) |
| 429 na rota do webhook | `RATE_LIMIT_WEBHOOK` baixo demais. A FR reenvia, mas o aviso atrasa |
