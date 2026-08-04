"""Configuracao da aplicacao.

A aplicacao nao sobe com configuracao invalida: e preferivel falhar no boot,
onde alguem esta olhando, a servir dados errados em producao.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas import Grupo

Ambiente = Literal["development", "staging", "production"]

# Comprimento minimo do segredo do webhook. A Frete Rapido NAO assina o payload:
# o segredo na URL e a unica barreira que impede qualquer um de postar
# ocorrencias falsas. 32 caracteres de `secrets.token_urlsafe` sao inadivinhaveis.
MIN_SEGREDO_WEBHOOK = 32


def _codigos(bruto: str) -> frozenset[int]:
    """Lista de codigos de ocorrencia separada por virgula.

    Recusa o que nao for numero em vez de descartar em silencio: um
    `NOTIFICACAO_CODIGOS_IGNORADOS=232;140` (ponto e virgula por engano) que
    virasse conjunto vazio voltaria a notificar um codigo que alguem desligou de
    proposito.
    """
    valores: set[int] = set()
    for parte in bruto.split(","):
        limpo = parte.strip()
        if not limpo:
            continue
        if not limpo.isdigit():
            raise ValueError(f"codigo de ocorrencia nao numerico: {limpo!r}")
        valores.add(int(limpo))
    return frozenset(valores)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Ambiente = "development"

    # --- Shopify ---
    shopify_shop_domain: str = ""
    # Apps do Dev Dashboard (obrigatorio para apps novos desde 01/01/2026):
    # a aplicacao troca estas credenciais por um token que expira em 24h.
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    # Apps personalizados LEGADOS: token `shpat_` fixo. Tem precedencia se
    # preenchido, para nao quebrar integracoes antigas que ainda funcionam.
    shopify_access_token: str = ""
    shopify_api_version: str = "2026-07"

    # O "#" nao e caso especial: e o valor padrao do prefixo e passa pelo mesmo
    # caminho de qualquer outro afixo. Ha um unico ponto de configuracao, para
    # que a remocao nunca seja aplicada duas vezes.
    shopify_order_prefix: str = "#"
    shopify_order_suffix: str = ""

    # --- Frete Rapido ---
    # A operacao usa 3 CNPJs, e cada um tem seu proprio token: um token so
    # enxerga os fretes do seu CNPJ. O pedido da Shopify carrega uma TAG que
    # identifica qual empresa despachou, e e ela que escolhe o token.
    # Formato: JSON {"tag-da-loja": "token de 32 caracteres", ...}
    frete_rapido_tokens: dict[str, str] = Field(default_factory=dict)
    # Compatibilidade / operacao de CNPJ unico.
    frete_rapido_token: str = ""
    frete_rapido_base_url: str = "https://freterapido.com"
    # Fuso ATRIBUIDO as datas naive da Frete Rapido. Isto e atribuicao de fuso de
    # origem, nao conversao. PENDENTE de confirmacao com o fornecedor.
    frete_rapido_timezone: str = "America/Sao_Paulo"

    # --- Webhook da Frete Rapido / notificacao proativa ---
    # A FR nao assina o webhook: o segredo vai no CAMINHO da URL e e comparado
    # com `compare_digest`. Vazio desliga a rota por completo.
    fr_webhook_segredo: str = ""

    # UM SEGREDO POR CNPJ. O cadastro no Dash FR e por CNPJ (confirmado com o
    # suporte em 03/08/2026), e o payload nao diz qual embarcador originou o
    # evento -- so traz o CNPJ da TRANSPORTADORA. Dar uma URL distinta a cada
    # cadastro e o que permite saber a origem.
    #
    # O ganho principal nao e estatistico, e de deteccao de falha: com uma URL
    # unica, um cadastro que pare de enviar e indistinguivel de um CNPJ com
    # pouco movimento. Com segredos separados, o painel mostra qual CNPJ
    # emudeceu -- e da para revogar um sem tocar nos outros.
    #
    # Formato: JSON {"tag-da-loja": "segredo", ...}, com as MESMAS chaves de
    # FRETE_RAPIDO_TOKENS.
    fr_webhook_segredos: dict[str, str] = Field(default_factory=dict)

    # Bearer token exigido no cabecalho `Authorization`. O painel da Frete
    # Rapido tem campo proprio para isto -- a documentacao publica nao menciona,
    # mas o formulario de cadastro oferece Basic, Bearer e headers avulsos.
    #
    # E mecanismo MELHOR que o segredo no caminho: cabecalho nao aparece em log
    # de acesso, nem em `Referer`, nem no historico de proxy. Usamos os dois, e
    # ambos precisam bater. Compartilhado pelos tres cadastros: quem separa a
    # origem e o segredo do caminho.
    fr_webhook_bearer: str = ""

    # Interruptor geral do ENVIO. Com `false` a API percorre o caminho inteiro
    # -- recebe, filtra, classifica, deduplica, resolve o pedido na Shopify --
    # e grava o resultado SEM enviar nada. E o modo de observacao: serve para
    # medir quais ocorrencias de fato acontecem e quantos pedidos tem telefone
    # utilizavel, antes de falar com cliente de verdade.
    notificacao_ativa: bool = False

    # Grupos que disparam mensagem, separados por virgula. O grupo -- e nao o
    # codigo -- e o nivel certo de configuracao: ele ja e o conceito de negocio
    # curado em `services/ocorrencias.py`.
    notificacao_grupos: str = ""
    # Valvulas de escape por codigo, para o caso pontual que o grupo nao resolve.
    notificacao_codigos_extra: str = ""
    notificacao_codigos_ignorados: str = ""

    # Trava anti-spam. Uma transportadora que posta cinco codigos em sequencia
    # nao pode virar cinco mensagens -- e um segredo vazado nao pode virar mil.
    notificacao_max_por_pedido: int = Field(default=3, ge=1, le=50)
    notificacao_janela_horas: int = Field(default=6, ge=1, le=168)

    # Janela de AGREGACAO DE VOLUME -- curta, e outra grandeza. Uma remessa de
    # varias caixas gera uma ocorrencia por volume com minutos de diferenca
    # (observado: 4 em 21 minutos), e isso e UM fato. Ja duas tentativas de
    # entrega reais no mesmo dia sao DOIS fatos, e o cliente precisa saber dos
    # dois.
    #
    # Usar a janela anti-spam (horas) para as duas coisas silenciava a segunda
    # tentativa legitima. Sao conceitos distintos e agora tem parametros
    # distintos.
    notificacao_janela_volume_min: int = Field(default=60, ge=1, le=1440)

    # Teto de TENTATIVAS por pedido na mesma janela. Limita CUSTO, nao mensagem.
    notificacao_max_tentativas_pedido: int = Field(default=20, ge=1, le=500)

    # Espera minima antes de reprocessar a MESMA linha pendente. Sem isto, cada
    # repeticao readquiria o lease e reconsultava a Frete Rapido sem limite --
    # o teto de tentativas so barrava linha nova. Menor que o primeiro degrau da
    # escada de reentrega deles (1 min), para nao atrasar o caminho legitimo.
    notificacao_cooldown_s: int = Field(default=45, ge=1, le=3600)

    # Duracao do lease de processamento. Precisa cobrir o pior caso do caminho
    # inteiro com folga: Frete Rapido (8s) + Shopify (ate ~14s quando renova o
    # token OAuth, que tem orcamento proprio) + n8n (6s), MAIS esperas que nao
    # entram em orcamento nenhum -- trava de renovacao compartilhada e espera por
    # conexao do banco. O `renovar` antes do envio cobre o resto.
    notificacao_lease_s: int = Field(default=120, ge=30, le=900)

    # Confirmar cada evento na PROPRIA Frete Rapido antes de avisar o cliente.
    #
    # O webhook nao e assinado: o segredo da URL prova que quem chamou conhece o
    # segredo, nao que o evento aconteceu. Perguntar a fonte -- "este pedido tem
    # mesmo esta ocorrencia?" -- transforma o payload de afirmacao em palpite a
    # ser verificado, e nao depende de IP fixo nem de nada que o fornecedor
    # precise nos conceder.
    #
    # Interruptor de emergencia: se algum dia as duas APIs deles divergirem, isto
    # desliga a verificacao sem exigir deploy.
    notificacao_verificar_na_fonte: bool = True

    # --- n8n (entrega da mensagem) ---
    n8n_webhook_url: str = ""
    n8n_webhook_token: str = ""

    # --- Banco ---
    database_url: str = ""
    email_hmac_key: str = ""

    # --- Seguranca ---
    cors_origins: str = ""
    trusted_proxies: str = "127.0.0.1"
    rate_limit: str = "10/minute"
    # Limite proprio do webhook: trafego de servidor, nao de navegador. O de
    # cliente (10/minuto) faria a Frete Rapido levar 429 numa rajada normal de
    # ocorrencias.
    rate_limit_webhook: str = "300/minute"

    # --- Demonstracao ---
    demo_mode: bool = False
    demo_email: str = "demo@exemplo.com"

    # --- Testes de contrato ---
    contract_test_order_number: str = ""
    contract_test_email: str = ""

    # Limites de campos de origem externa, para que cabecalho de cliente ou texto
    # de transportadora nao inchem o banco sem teto.
    max_user_agent: int = Field(default=512)
    max_texto_externo: int = Field(default=1000)

    @property
    def producao(self) -> bool:
        return self.env == "production"

    @property
    def lista_cors(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def lista_proxies(self) -> list[str]:
        return [p.strip() for p in self.trusted_proxies.split(",") if p.strip()]

    @property
    def tokens_frete_rapido(self) -> dict[str, str]:
        """Tokens por CNPJ, com as chaves normalizadas para casar com as tags.

        As tags da Shopify sao digitadas por pessoas: comparar sem diferenciar
        caixa e espacos evita que "Matriz " deixe de casar com "matriz".
        """
        if self.frete_rapido_tokens:
            return {
                chave.strip().casefold(): token
                for chave, token in self.frete_rapido_tokens.items()
                if token
            }
        # Operacao de CNPJ unico: um token sem tag associada.
        if self.frete_rapido_token:
            return {"": self.frete_rapido_token}
        return {}

    @property
    def grupos_notificaveis(self) -> frozenset[Grupo]:
        """Grupos configurados para disparar mensagem.

        Valores invalidos NAO sao ignorados aqui -- o validador de boot recusa a
        subida. Silenciar um erro de digitacao desligaria as notificacoes sem
        ninguem perceber, que e exatamente o modo de falha que este projeto
        evita em todo lugar.
        """
        return frozenset(
            Grupo(g.strip().casefold())
            for g in self.notificacao_grupos.split(",")
            if g.strip()
        )

    @property
    def codigos_extra(self) -> frozenset[int]:
        """Codigos que disparam mesmo fora dos grupos configurados."""
        return _codigos(self.notificacao_codigos_extra)

    @property
    def codigos_ignorados(self) -> frozenset[int]:
        """Codigos que nunca disparam, mesmo dentro de um grupo configurado."""
        return _codigos(self.notificacao_codigos_ignorados)

    @property
    def segredos_webhook(self) -> dict[str, str]:
        """Segredo -> CNPJ. Invertido de proposito: a rota busca pelo segredo.

        O `FR_WEBHOOK_SEGREDO` avulso continua valendo, mapeado para CNPJ
        desconhecido (`""`). E o caminho de quem opera um CNPJ so -- e o de quem
        ja tinha o webhook no ar antes desta mudanca, que nao pode quebrar num
        `docker compose up`.
        """
        mapa = {
            segredo: chave.strip().casefold()
            for chave, segredo in self.fr_webhook_segredos.items()
            if segredo
        }
        if self.fr_webhook_segredo:
            mapa.setdefault(self.fr_webhook_segredo, "")
        return mapa

    @property
    def webhook_fr_habilitado(self) -> bool:
        """A rota so existe com ao menos um segredo configurado."""
        return bool(self.segredos_webhook)

    @model_validator(mode="after")
    def _validar(self) -> Settings:
        # Trava de seguranca: servir dados falsos a clientes reais e pior do que
        # nao subir. Um deploy distraido com DEMO_MODE ligado seria invisivel.
        if self.demo_mode and self.producao:
            raise ValueError(
                "DEMO_MODE=true e proibido com ENV=production: a aplicacao serviria "
                "dados simulados a clientes reais."
            )

        # Em modo demonstracao as integracoes reais nao sao usadas, entao os
        # segredos delas nao precisam existir.
        if not self.demo_mode:
            faltando = [
                nome
                for nome, valor in (
                    ("SHOPIFY_SHOP_DOMAIN", self.shopify_shop_domain),
                    ("EMAIL_HMAC_KEY", self.email_hmac_key),
                )
                if not valor
            ]
            # Duas formas validas de autenticar: credenciais do Dev Dashboard ou
            # token fixo de app legado.
            tem_client = bool(self.shopify_client_id and self.shopify_client_secret)
            if not tem_client and not self.shopify_access_token:
                faltando.append(
                    "SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (ou SHOPIFY_ACCESS_TOKEN)"
                )
            if not self.tokens_frete_rapido:
                faltando.append("FRETE_RAPIDO_TOKENS (ou FRETE_RAPIDO_TOKEN)")
            if faltando:
                raise ValueError(
                    f"configuracao obrigatoria ausente: {', '.join(faltando)}"
                )

        self._validar_notificacao()

        if self.producao:
            self._exigir_producao()

        return self

    def _validar_notificacao(self) -> None:
        """Recusa configuracao de notificacao invalida, mesmo fora de producao.

        Tudo aqui falha FECHADO. Um grupo escrito errado
        (`aguardando_retira`) que virasse conjunto vazio desligaria os avisos em
        silencio -- e ninguem descobre um aviso que nao foi enviado.
        """
        problemas: list[str] = []

        try:
            grupos = self.grupos_notificaveis
        except ValueError:
            validos = ", ".join(sorted(g.value for g in Grupo))
            problemas.append(
                f"NOTIFICACAO_GRUPOS contem grupo inexistente "
                f"({self.notificacao_grupos!r}). Validos: {validos}"
            )
            grupos = frozenset()

        for nome, bruto in (
            ("NOTIFICACAO_CODIGOS_EXTRA", self.notificacao_codigos_extra),
            ("NOTIFICACAO_CODIGOS_IGNORADOS", self.notificacao_codigos_ignorados),
        ):
            try:
                _codigos(bruto)
            except ValueError as exc:
                problemas.append(f"{nome}: {exc}")

        if self.notificacao_ativa:
            # Com o envio LIGADO ha um cliente do outro lado. Toda barreira que
            # a documentacao promete tem de estar de fato no lugar -- nao basta
            # existir no codigo. Revisao de seguranca mostrou que a combinacao
            # "envio ativo + Bearer vazio + confirmacao desligada" subia
            # tranquila, e nela o segredo da URL virava a unica barreira.
            if not grupos and not self.codigos_extra:
                problemas.append(
                    "NOTIFICACAO_ATIVA=true sem NOTIFICACAO_GRUPOS nem "
                    "NOTIFICACAO_CODIGOS_EXTRA: nada dispararia mensagem"
                )
            if not self.n8n_webhook_url:
                problemas.append("NOTIFICACAO_ATIVA=true exige N8N_WEBHOOK_URL")
            if not self.segredos_webhook:
                problemas.append(
                    "NOTIFICACAO_ATIVA=true exige FR_WEBHOOK_SEGREDOS "
                    "(ou FR_WEBHOOK_SEGREDO)"
                )

            # O segredo da URL vaza para o log de acesso do proxy, que nao
            # controlamos. Quem autentica de verdade e o Bearer.
            if len(self.fr_webhook_bearer) < MIN_SEGREDO_WEBHOOK:
                problemas.append(
                    "NOTIFICACAO_ATIVA=true exige FR_WEBHOOK_BEARER com pelo "
                    f"menos {MIN_SEGREDO_WEBHOOK} caracteres: o segredo da URL "
                    "aparece no log de acesso do proxy e nao serve sozinho"
                )
            if not self.n8n_webhook_token:
                problemas.append(
                    "NOTIFICACAO_ATIVA=true exige N8N_WEBHOOK_TOKEN: sem ele, "
                    "quem descobrir a URL do n8n dispara mensagem para qualquer "
                    "telefone"
                )
            # "Envia sem confirmar" nao deve ser um estado alcancavel: sem a
            # confirmacao, o TEXTO que chega ao cliente volta a vir de quem
            # chamou a rota.
            if not self.notificacao_verificar_na_fonte:
                problemas.append(
                    "NOTIFICACAO_VERIFICAR_NA_FONTE=false com NOTIFICACAO_ATIVA="
                    "true: o interruptor de emergencia da verificacao exige "
                    "desligar tambem o envio"
                )
            # Chave de segredo sem token correspondente = evento que nunca
            # confirma, em silencio.
            orfas = sorted(
                c
                for c in self.segredos_webhook.values()
                if c and c not in self.tokens_frete_rapido
            )
            if orfas:
                problemas.append(
                    f"CNPJs em FR_WEBHOOK_SEGREDOS sem token em "
                    f"FRETE_RAPIDO_TOKENS: {', '.join(orfas)}"
                )

        # O segredo e a barreira principal da rota: a Frete Rapido nao assina o
        # payload. Curto demais e adivinhavel.
        curtos = [
            nome
            for nome, valor in (
                ("FR_WEBHOOK_SEGREDO", self.fr_webhook_segredo),
                *(
                    (f"FR_WEBHOOK_SEGREDOS[{chave}]", segredo)
                    for chave, segredo in self.fr_webhook_segredos.items()
                ),
            )
            if valor and len(valor) < MIN_SEGREDO_WEBHOOK
        ]
        for nome in curtos:
            problemas.append(
                f"{nome} curto demais (minimo {MIN_SEGREDO_WEBHOOK} caracteres; "
                'gere com `python -c "import secrets; '
                'print(secrets.token_urlsafe(32))"`)'
            )

        # Dois CNPJs com o MESMO segredo tornariam a origem indeterminada -- e o
        # ponto inteiro de separar os segredos e saber a origem.
        segredos = [s for s in self.fr_webhook_segredos.values() if s]
        if len(segredos) != len(set(segredos)):
            problemas.append(
                "FR_WEBHOOK_SEGREDOS tem segredo repetido entre CNPJs: a origem "
                "do evento ficaria indeterminada"
            )

        if self.notificacao_max_por_pedido < 1:
            problemas.append("NOTIFICACAO_MAX_POR_PEDIDO deve ser >= 1")
        if self.notificacao_janela_horas < 1:
            problemas.append("NOTIFICACAO_JANELA_HORAS deve ser >= 1")

        if problemas:
            raise ValueError(
                "configuracao de notificacao invalida: " + "; ".join(problemas)
            )

    def _exigir_producao(self) -> None:
        """Rejeita configuracao que apenas PARECE preenchida.

        Verificar so se o valor existe nao basta: uma implantacao podia subir com
        a chave HMAC do arquivo de exemplo, com tokens ficticios de formato
        plausivel, ou sem CORS -- e a API responderia normalmente, so que
        insegura ou devolvendo "sem rastreio" para todo mundo.
        """
        problemas: list[str] = []

        # Valores que aparecem no .env.example. Se chegaram aqui, ninguem trocou.
        marcadores = ("xxxx", "yyyy", "zzzz", "troque", "exemplo", "sua-loja",
                      "COLE_AQUI", "token32caracteres", "tag-empresa-")
        suspeitos = {
            "EMAIL_HMAC_KEY": self.email_hmac_key,
            "SHOPIFY_CLIENT_SECRET": self.shopify_client_secret,
            "SHOPIFY_SHOP_DOMAIN": self.shopify_shop_domain,
        }
        for nome, valor in suspeitos.items():
            if valor and any(m.lower() in valor.lower() for m in marcadores):
                problemas.append(f"{nome} ainda contem valor de exemplo")

        for chave, token in self.tokens_frete_rapido.items():
            if any(m.lower() in token.lower() for m in marcadores):
                problemas.append(f"token do CNPJ '{chave}' e um valor de exemplo")

        # Chave curta e reversivel por forca bruta: o HMAC deixaria de proteger.
        if len(self.email_hmac_key) < 32:
            problemas.append("EMAIL_HMAC_KEY curta demais (minimo 32 caracteres)")

        # Sem CORS em producao, nenhuma pagina de navegador consegue chamar a API.
        if not self.lista_cors:
            problemas.append("CORS_ORIGINS vazio")

        # Sem banco nao ha auditoria nem expurgo -- exigencia de LGPD do projeto.
        if not self.database_url:
            problemas.append("DATABASE_URL vazia (sem auditoria nem expurgo LGPD)")

        if problemas:
            raise ValueError(
                "configuracao invalida para producao: " + "; ".join(problemas)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
