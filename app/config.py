"""Configuracao da aplicacao.

A aplicacao nao sobe com configuracao invalida: e preferivel falhar no boot,
onde alguem esta olhando, a servir dados errados em producao.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Ambiente = Literal["development", "staging", "production"]


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

    # --- Banco ---
    database_url: str = ""
    email_hmac_key: str = ""

    # --- Seguranca ---
    cors_origins: str = ""
    trusted_proxies: str = "127.0.0.1"
    rate_limit: str = "10/minute"

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

        if self.producao:
            self._exigir_producao()

        return self

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
