# Aplicando a página de rastreio no tema

São **quatro arquivos**. O tema é OS 2.0 (templates `.json`), então a página é
montada por seções.

## Onde criar cada um

No admin: **Loja online → Temas → ⋯ → Editar código**.

| Arquivo daqui | Onde criar no tema |
|---|---|
| `assets/rastreio.css` | pasta `assets` → *Adicionar um novo recurso* |
| `assets/rastreio.js` | pasta `assets` → *Adicionar um novo recurso* |
| `sections/rastreio.liquid` | pasta `sections` → *Adicionar uma nova seção* |
| `templates/page.rastreio.json` | pasta `templates` → *Adicionar um novo modelo* |

Ao criar pelo editor da Shopify, informe **apenas o nome** (`rastreio`) — a
interface acrescenta a extensão sozinha. Se ela criar o arquivo com conteúdo de
exemplo, apague tudo antes de colar.

**Ordem importa:** crie os `assets` primeiro. A seção referencia os dois, e o
editor acusa erro se eles ainda não existirem.

## Ligando à sua página

1. **Loja online → Páginas** → abra a página que você já criou
2. No painel direito, em **Modelo de tema** (ou *Theme template*), escolha
   **`rastreio`**
3. **Salvar**

O nome do modelo vem de `page.rastreio.json` — **não** precisa coincidir com o
endereço da página. Você pode aplicá-lo a qualquer página.

## Testando

Abra a página no site. Use um pedido real e o e-mail daquela compra.

Vale testar também os caminhos de erro, que são metade da experiência:

- E-mail errado com pedido certo → mensagem genérica
- Pedido inexistente → **exatamente a mesma mensagem** (proposital: diferenciar
  permitiria descobrir quais números de pedido existem)
- Número com `#` na frente → funciona igual, a API tolera

E abra no celular: a maior parte do tráfego vem de lá, e o resultado tem rolagem
automática pensada para telas pequenas.

## Editando sem mexer em código

No editor de temas (**Personalizar**), a seção expõe:

- Título, subtítulo, textos dos campos e do botão
- Nota de privacidade e rodapé
- Imagem ao lado do formulário
- Cor de fundo
- **Endereço da API** — se o domínio mudar, troca aqui

## Depois de publicar

Remova a origem de desenvolvimento do CORS, no servidor:

```bash
cd /opt/rastreio
sed -i 's|,http://localhost:8080||' .env
docker compose --env-file .env -f deploy/docker-compose.traefik.yml up -d
grep CORS_ORIGINS .env
```

Devem sobrar apenas os dois domínios da loja.

## Se algo não funcionar

**A página aparece mas o formulário não faz nada:** o `rastreio.js` não carregou.
Confira se o arquivo existe em `assets` com esse nome exato.

**"Não conseguimos conectar":** quase sempre é CORS. O domínio de onde a página
está sendo servida precisa constar em `CORS_ORIGINS` no servidor. Se estiver
testando pelo preview do tema, o domínio é o mesmo da loja — deve funcionar.

**"Não encontramos um pedido":** o e-mail precisa ser exatamente o usado na
compra. Lembre que pedidos com mais de 60 dias não aparecem, por limitação da
API da Shopify.

O console do navegador (F12) mostra a causa real em qualquer um dos casos.
