from __future__ import annotations

import os

import pytest

# Configuracao minima para que `get_settings()` valide sem exigir segredos reais.
# Definida antes de qualquer import da aplicacao que leia settings.
os.environ.setdefault("ENV", "development")
os.environ.setdefault("SHOPIFY_SHOP_DOMAIN", "teste.myshopify.com")
os.environ.setdefault("SHOPIFY_ACCESS_TOKEN", "shpat_tokendetestesomente")
os.environ.setdefault("FRETE_RAPIDO_TOKEN", "t" * 32)
os.environ.setdefault("EMAIL_HMAC_KEY", "chave-de-teste-nao-usar-em-producao")
os.environ.setdefault("SHOPIFY_ORDER_PREFIX", "#")
os.environ.setdefault("SHOPIFY_ORDER_SUFFIX", "")


@pytest.fixture(autouse=True)
def _limpar_cache_settings() -> None:
    from app.config import get_settings

    get_settings.cache_clear()
