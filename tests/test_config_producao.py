"""Validacao reforcada para producao.

Verificar apenas se um valor existe nao basta: uma implantacao podia subir com
a chave do arquivo de exemplo, com tokens ficticios ou sem CORS, e a API
responderia normalmente -- so que insegura, ou devolvendo "sem rastreio" para
todo mundo.
"""

from __future__ import annotations

import pytest

from app.config import Settings

VALIDO = {
    "env": "production",
    "shopify_shop_domain": "loja-real.myshopify.com",
    "shopify_client_id": "a" * 32,
    "shopify_client_secret": "shpss_" + "b" * 32,
    "email_hmac_key": "c" * 48,
    "frete_rapido_tokens": {"empresa": "d" * 32},
    "cors_origins": "https://loja-real.com.br",
    "database_url": "postgresql+psycopg://u:p@db:5432/rastreio",
}


def test_configuracao_completa_e_aceita() -> None:
    assert Settings(**VALIDO).producao  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("campo", "valor", "trecho"),
    [
        # Valores que vieram direto do .env.example.
        ("email_hmac_key", "troque-esta-chave-por-um-valor-aleatorio-longo", "exemplo"),
        ("shopify_shop_domain", "sua-loja.myshopify.com", "exemplo"),
        ("shopify_client_secret", "shpss_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "exemplo"),
        # Chave curta seria reversivel por forca bruta.
        ("email_hmac_key", "curta", "curta demais"),
        # Sem CORS nenhuma pagina de navegador consegue chamar a API.
        ("cors_origins", "", "CORS_ORIGINS"),
        # Sem banco nao ha auditoria nem expurgo LGPD.
        ("database_url", "", "DATABASE_URL"),
    ],
)
def test_producao_recusa_valores_perigosos(
    campo: str, valor: str, trecho: str
) -> None:
    config = {**VALIDO, campo: valor}
    with pytest.raises(ValueError, match=trecho):
        Settings(**config)  # type: ignore[arg-type]


def test_producao_recusa_token_ficticio_da_frete_rapido() -> None:
    """Token de exemplo tem formato plausivel e passaria por 'preenchido'.

    A API subiria e devolveria 'sem rastreio' para todos os pedidos -- falha
    silenciosa, do tipo que so aparece por reclamacao de cliente.
    """
    config = {**VALIDO, "frete_rapido_tokens": {"empresa": "x" * 32}}
    with pytest.raises(ValueError, match="valor de exemplo"):
        Settings(**config)  # type: ignore[arg-type]


def test_desenvolvimento_nao_sofre_essas_restricoes() -> None:
    """Em desenvolvimento os valores de exemplo sao justamente o que se usa."""
    s = Settings(
        env="development",
        shopify_shop_domain="sua-loja.myshopify.com",
        shopify_client_id="teste",
        shopify_client_secret="shpss_xxxx",
        email_hmac_key="curta",
        frete_rapido_tokens={"empresa": "x" * 32},
        cors_origins="",
        database_url="",
    )
    assert not s.producao
