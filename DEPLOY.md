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
