# Integrando a consulta de rastreio em outro projeto

Você precisa de **dois arquivos** e **cinco elementos** no HTML.

## 1. Copie os arquivos

- [`rastreio.js`](rastreio.js) — toda a lógica, sem dependência nenhuma
- [`rastreio.css`](rastreio.css) — só as classes `.rastreio-*`

Não copie o `index.html`: ele é a página de teste, não a integração.

## 2. Garanta os cinco elementos

Os nomes dos `id` são seus — só precisam ser passados na configuração:

```html
<form id="meu-form">
  <input type="email" id="meu-email" required>
  <input type="text"  id="meu-pedido" required inputmode="numeric">
  <button type="submit" id="meu-botao">Consultar</button>
</form>

<!-- aria-live faz o leitor de tela anunciar o resultado.
     Sem isso, quem usa leitor envia o formulário e não recebe retorno algum. -->
<div id="meu-resultado" aria-live="polite" aria-atomic="true"></div>
```

## 3. Ligue os dois

```html
<link rel="stylesheet" href="rastreio.css">

<!-- Script CLÁSSICO, não módulo: navegadores bloqueiam `import` em file://,
     e o sintoma é o formulário ficar inerte sem erro visível. -->
<script src="rastreio.js"></script>
<script>
  window.RASTREIO_API = "http://localhost:8000/api/v1/rastreio";

  window.Rastreio.ligarFormulario({
    formulario: document.getElementById("meu-form"),
    campoEmail: document.getElementById("meu-email"),
    campoPedido: document.getElementById("meu-pedido"),
    resultado:  document.getElementById("meu-resultado"),
    botao:      document.getElementById("meu-botao"),   // opcional
  });
</script>
```

Se algum elemento não for encontrado, o console mostra qual — em vez de o
formulário ficar silenciosamente sem efeito.

## 4. Testando com pedidos REAIS

Suba a API **sem** o modo demonstração:

```powershell
# na pasta deste projeto
$env:DEMO_MODE = "false"
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Agora use os pedidos de verdade da loja:

Use um pedido real da loja e **o e-mail usado naquela compra**. Os pedidos
`59551` (entregue) e `59552` (aguardando coleta) servem como referência.

O e-mail precisa ser exatamente o do pedido — é dado de cliente, então não fica
registrado aqui.

### Sobre CORS

Com `ENV=development` (o padrão), a API aceita **qualquer origem** — então seu
protótipo funciona aberto de `file://` ou servido em qualquer porta, sem
configuração. A API avisa isso no log ao subir.

Em produção o comportamento é o oposto: só as origens de `CORS_ORIGINS` passam.

> CORS não é barreira de segurança de qualquer forma — ele restringe apenas
> navegadores, e qualquer script o ignora. A proteção real contra enumeração de
> pedidos é o rate limiting mais a validação de e-mail.

## O que a API responde

Sete resultados, todos com `resultado` no corpo:

| `resultado` | HTTP | Significado |
|---|---|---|
| `sucesso` | 200 | Tem histórico para exibir |
| `sem_rastreio` | 200 | Pedido confirmado, ainda não despachado |
| `vazio_fr` | 200 | Despachado, mas sem detalhe disponível agora |
| `nao_encontrado` | 404 | E-mail ou número não conferem |
| `erro_externo` | 503 | Falha nas integrações |
| `limite_excedido` | 429 | Muitas consultas do mesmo IP |
| — | 422 | E-mail em formato inválido |

`nao_encontrado` é **deliberadamente idêntico** para pedido inexistente e para
e-mail errado. Diferenciar permitiria descobrir quais números de pedido existem.

Em `sucesso`, estes campos podem vir **nulos**: `transportadora`,
`previsao_entrega` e `descricao`. Um pedido ainda não despachado tem ocorrências
mas pode não ter nenhum deles.

## Armadilha: porta 8000 já ocupada

Se um servidor de um teste anterior ainda estiver rodando, o novo **falha ao
subir em silêncio** e o antigo continua respondendo — possivelmente em outro
modo. O sintoma é confuso: pedidos reais voltam `nao_encontrado` porque quem
respondeu foi um servidor em modo demonstração.

```powershell
# ver quem está na porta
Get-NetTCPConnection -LocalPort 8000 -State Listen

# encerrar
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

O `rodar-demo.ps1` já detecta isso e pergunta antes de continuar.

## Limite de requisições

O padrão é **10 consultas por minuto por IP**. Testando muitos cenários seguidos
você bate no limite e recebe `429`. Para afrouxar durante o desenvolvimento:

```powershell
$env:RATE_LIMIT = "100/minute"
```
