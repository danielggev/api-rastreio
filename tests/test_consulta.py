"""Orquestracao: a ORDEM do fluxo e o que estes testes protegem."""

from __future__ import annotations

from datetime import date

import pytest

from app.schemas import Grupo, OcorrenciaFR, Resultado
from app.services.cache import CacheMemoria
from app.services.consulta import MSG_NAO_ENCONTRADO, ServicoConsulta
from app.services.multi_cnpj import ResultadoBusca
from app.services.normalizacao import NumeroPedidoFR
from app.services.shopify import PedidoShopify, ShopifyErro

EMAIL = "cliente@exemplo.com"


class ShopifyFalso:
    def __init__(self, pedido: PedidoShopify | None, erro: Exception | None = None) -> None:
        self.pedido = pedido
        self.erro = erro
        self.chamadas = 0

    async def buscar_pedido(self, numero: NumeroPedidoFR) -> PedidoShopify | None:
        self.chamadas += 1
        if self.erro:
            raise self.erro
        return self.pedido


class FreteRapidoFalso:
    """Substitui o `BuscadorMultiCNPJ`, que ja resolve a selecao de token."""

    def __init__(
        self,
        ocorrencias: list[OcorrenciaFR] | None = None,
        erro: Exception | None = None,
        houve_falha: bool = False,
    ) -> None:
        self.ocorrencias = ocorrencias if ocorrencias is not None else []
        self.erro = erro
        self.houve_falha = houve_falha
        self.chamadas = 0

    async def buscar(
        self, numero: NumeroPedidoFR, tags: list[str] | None = None
    ) -> ResultadoBusca:
        self.chamadas += 1
        if self.erro:
            # O buscador ja converte falha de token em `houve_falha`; um erro
            # aqui representa indisponibilidade total.
            return ResultadoBusca(houve_falha=True)
        return ResultadoBusca(
            ocorrencias=list(self.ocorrencias),
            cnpj="empresa-a" if self.ocorrencias else None,
            houve_falha=self.houve_falha,
        )


def _pedido(
    email: str | None = EMAIL,
    tem_fulfillment: bool = False,
    codigo_rastreio: str | None = None,
) -> PedidoShopify:
    return PedidoShopify(
        id="gid://shopify/Order/1",
        name="#59552",
        email_normalizado=email,
        criado_em=None,
        tem_fulfillment=tem_fulfillment,
        codigo_rastreio=codigo_rastreio,
    )


def _servico(shopify: object, fr: object, cache: object | None = None) -> ServicoConsulta:
    return ServicoConsulta(shopify, fr, cache)  # type: ignore[arg-type]


def _ocorrencias() -> list[OcorrenciaFR]:
    from app.services.ordenacao import indexar

    return indexar(
        [
            OcorrenciaFR(codigo=0, nome="Contratado", data_prevista_entrega=date(2026, 7, 29)),
            OcorrenciaFR(
                codigo=1,
                nome="Aguardando coleta / postagem",
                razao_social_transportadora="JADLOG LOGISTICA S.A",
            ),
        ]
    )


# --------------------------------------------------------------------------
# Seguranca: ordem do fluxo
# --------------------------------------------------------------------------


async def test_cache_nunca_e_servido_sem_validar_o_email() -> None:
    """TESTE OBRIGATORIO do plano.

    Com o cache quente para o pedido, uma requisicao com email errado nao pode
    devolver nada alem de `nao_encontrado`.
    """
    cache = CacheMemoria()
    await cache.guardar("59552", _ocorrencias())

    fr = FreteRapidoFalso(_ocorrencias())
    servico = _servico(ShopifyFalso(_pedido()), fr, cache)

    consulta = await servico.consultar("invasor@exemplo.com", "59552")

    assert consulta.resultado is Resultado.NAO_ENCONTRADO
    assert consulta.resposta.mensagem == MSG_NAO_ENCONTRADO  # type: ignore[union-attr]
    assert not hasattr(consulta.resposta, "historico")


async def test_email_errado_e_pedido_inexistente_respondem_identico() -> None:
    """TESTE OBRIGATORIO: divergir permitiria enumerar pedidos validos."""
    com_email_errado = await _servico(
        ShopifyFalso(_pedido()), FreteRapidoFalso(_ocorrencias())
    ).consultar("outro@exemplo.com", "59552")

    inexistente = await _servico(
        ShopifyFalso(None), FreteRapidoFalso(_ocorrencias())
    ).consultar(EMAIL, "99999")

    assert (
        com_email_errado.resposta.model_dump_json()
        == inexistente.resposta.model_dump_json()
    )
    assert com_email_errado.status_http == inexistente.status_http == 404


async def test_frete_rapido_nao_e_chamada_antes_da_validacao() -> None:
    fr = FreteRapidoFalso(_ocorrencias())
    await _servico(ShopifyFalso(_pedido()), fr).consultar("outro@exemplo.com", "59552")
    assert fr.chamadas == 0


async def test_email_nulo_no_pedido_resulta_em_nao_encontrado() -> None:
    consulta = await _servico(
        ShopifyFalso(_pedido(email=None)), FreteRapidoFalso(_ocorrencias())
    ).consultar(EMAIL, "59552")
    assert consulta.resultado is Resultado.NAO_ENCONTRADO


async def test_numero_em_formato_invalido_nao_chega_na_shopify() -> None:
    shopify = ShopifyFalso(_pedido())
    consulta = await _servico(shopify, FreteRapidoFalso()).consultar(EMAIL, "LOJA12-345-A")
    assert consulta.resultado is Resultado.NAO_ENCONTRADO
    assert shopify.chamadas == 0


# --------------------------------------------------------------------------
# Frete Rapido chamada mesmo sem fulfillment
# --------------------------------------------------------------------------


async def test_consulta_frete_rapido_mesmo_sem_fulfillment() -> None:
    """TESTE OBRIGATORIO do plano.

    Os pedidos reais de teste provam o caso: tem ocorrencias ("Contratado",
    "Aguardando coleta") e ainda nao tem rastreio na Shopify.
    """
    fr = FreteRapidoFalso(_ocorrencias())
    consulta = await _servico(ShopifyFalso(_pedido(tem_fulfillment=False)), fr).consultar(
        EMAIL, "59552"
    )

    assert fr.chamadas == 1
    assert consulta.resultado is Resultado.SUCESSO
    assert consulta.resposta.status_atual.codigo == 1  # type: ignore[union-attr]


async def test_sucesso_consolida_previsao_e_transportadora() -> None:
    consulta = await _servico(
        ShopifyFalso(_pedido()), FreteRapidoFalso(_ocorrencias())
    ).consultar(EMAIL, "59552")

    resposta = consulta.resposta
    assert resposta.previsao_entrega == date(2026, 7, 29)  # type: ignore[union-attr]
    # A razao social vira nome comercial na resposta: o cliente reconhece
    # "Jadlog", nao "JADLOG LOGISTICA S.A".
    assert resposta.transportadora == "Jadlog"  # type: ignore[union-attr]
    assert resposta.status_atual.grupo is Grupo.PREPARANDO  # type: ignore[union-attr]


# --------------------------------------------------------------------------
# Distincao entre sem_rastreio e vazio_fr
# --------------------------------------------------------------------------


async def test_sem_fulfillment_e_sem_ocorrencias_e_sem_rastreio() -> None:
    consulta = await _servico(
        ShopifyFalso(_pedido(tem_fulfillment=False)), FreteRapidoFalso([])
    ).consultar(EMAIL, "59552")

    assert consulta.resultado is Resultado.SEM_RASTREIO
    assert consulta.status_http == 200


async def test_com_fulfillment_e_sem_ocorrencias_e_vazio_fr() -> None:
    """A distincao e o que transforma o bug silencioso do "#" em sinal observavel."""
    consulta = await _servico(
        ShopifyFalso(_pedido(tem_fulfillment=True, codigo_rastreio="FR260723D6KTG")),
        FreteRapidoFalso([]),
    ).consultar(EMAIL, "59552")

    assert consulta.resultado is Resultado.VAZIO_FR
    assert consulta.status_http == 200


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


async def test_cache_evita_segunda_chamada_a_frete_rapido() -> None:
    cache = CacheMemoria()
    fr = FreteRapidoFalso(_ocorrencias())
    servico = _servico(ShopifyFalso(_pedido()), fr, cache)

    primeira = await servico.consultar(EMAIL, "59552")
    segunda = await servico.consultar(EMAIL, "59552")

    assert fr.chamadas == 1
    assert not primeira.veio_do_cache
    assert segunda.veio_do_cache


async def test_retorno_vazio_nao_e_cacheado() -> None:
    """Cachear vazio criaria um `vazio_fr` falso enquanto o TTL durasse."""
    cache = CacheMemoria()
    fr = FreteRapidoFalso([])
    servico = _servico(ShopifyFalso(_pedido()), fr, cache)

    await servico.consultar(EMAIL, "59552")
    await servico.consultar(EMAIL, "59552")

    assert fr.chamadas == 2
    assert await cache.obter("59552") is None


async def test_dados_do_pedido_vem_sempre_frescos_da_shopify() -> None:
    """O cache guarda apenas ocorrencias da Frete Rapido.

    A Shopify e consultada em toda requisicao (para validar o email), entao os
    dados de fulfillment nunca ficam velhos.
    """
    cache = CacheMemoria()
    fr = FreteRapidoFalso(_ocorrencias())

    await _servico(ShopifyFalso(_pedido()), fr, cache).consultar(EMAIL, "59552")

    consulta = await _servico(
        ShopifyFalso(_pedido(tem_fulfillment=True, codigo_rastreio="FR260723D6KTG")),
        fr,
        cache,
    ).consultar(EMAIL, "59552")

    assert consulta.veio_do_cache
    assert fr.chamadas == 1
    assert consulta.resultado is Resultado.SUCESSO


# --------------------------------------------------------------------------
# Erros externos
# --------------------------------------------------------------------------


async def test_falha_da_shopify_vira_erro_externo() -> None:
    consulta = await _servico(
        ShopifyFalso(None, erro=ShopifyErro("indisponivel")), FreteRapidoFalso()
    ).consultar(EMAIL, "59552")

    assert consulta.resultado is Resultado.ERRO_EXTERNO
    assert consulta.status_http == 503


async def test_falha_da_frete_rapido_vira_erro_externo() -> None:
    from app.services.frete_rapido import FreteRapidoErro

    consulta = await _servico(
        ShopifyFalso(_pedido()), FreteRapidoFalso(erro=FreteRapidoErro("indisponivel"))
    ).consultar(EMAIL, "59552")

    assert consulta.resultado is Resultado.ERRO_EXTERNO


async def test_falha_parcial_de_cnpj_sem_dados_nunca_vira_sem_rastreio() -> None:
    """TESTE CRITICO do multi-CNPJ.

    Se um dos tokens falhou e nenhum devolveu dados, dizer "pedido em separacao"
    seria mentir: o CNPJ que falhou pode ser justamente o que despachou.
    """
    consulta = await _servico(
        ShopifyFalso(_pedido(tem_fulfillment=False)),
        FreteRapidoFalso([], houve_falha=True),
    ).consultar(EMAIL, "59552")

    assert consulta.resultado is Resultado.ERRO_EXTERNO
    assert consulta.status_http == 503


async def test_cnpj_que_atendeu_fica_registrado_para_auditoria() -> None:
    consulta = await _servico(
        ShopifyFalso(_pedido()), FreteRapidoFalso(_ocorrencias())
    ).consultar(EMAIL, "59552")

    assert consulta.cnpj == "empresa-a"


@pytest.mark.parametrize(
    ("resultado", "status"),
    [
        (Resultado.SUCESSO, 200),
        (Resultado.SEM_RASTREIO, 200),
        (Resultado.VAZIO_FR, 200),
        (Resultado.NAO_ENCONTRADO, 404),
        (Resultado.ERRO_EXTERNO, 503),
    ],
)
def test_mapa_de_status_http(resultado: Resultado, status: int) -> None:
    from app.services.consulta import Consulta

    consulta = Consulta.__new__(Consulta)
    object.__setattr__(consulta, "resultado", resultado)
    assert consulta.status_http == status
