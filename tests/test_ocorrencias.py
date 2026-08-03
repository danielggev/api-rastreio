"""Testes do mapa de grupos.

O foco esta nas classificacoes que uma leitura apressada do catalogo erra --
justamente as que a revisao do plano pegou.
"""

from __future__ import annotations

import pytest

from app.schemas import Grupo
from app.services.ocorrencias import (
    AMBIGUOS,
    POR_CODIGO,
    classificar,
    exige_acao_do_cliente,
)


def test_nenhum_codigo_em_dois_grupos() -> None:
    """A propria construcao do mapa levanta erro; aqui garantimos que ele carregou."""
    assert len(POR_CODIGO) > 200


def test_codigo_desconhecido_cai_no_neutro() -> None:
    """A Frete Rapido segue adicionando codigos; um novo nao pode quebrar a pagina."""
    assert classificar(9999) is Grupo.EM_ANDAMENTO
    assert classificar(4) is Grupo.EM_ANDAMENTO  # lacuna real do catalogo


def test_ambiguos_ficam_no_neutro_ate_confirmacao() -> None:
    for codigo in AMBIGUOS:
        assert codigo not in POR_CODIGO
        assert classificar(codigo) is Grupo.EM_ANDAMENTO


@pytest.mark.parametrize(
    ("codigo", "grupo", "porque"),
    [
        (0, Grupo.PREPARANDO, "Contratado"),
        (1, Grupo.PREPARANDO, "Aguardando coleta / postagem"),
        (2, Grupo.EM_TRANSITO, "Em transito"),
        (3, Grupo.ENTREGUE, "Entregue"),
        (17, Grupo.SAIU_PARA_ENTREGA, "Em rota para entrega"),
    ],
)
def test_classificacoes_basicas(codigo: int, grupo: Grupo, porque: str) -> None:
    assert classificar(codigo) is grupo, porque


@pytest.mark.parametrize(
    ("codigo", "grupo", "porque"),
    [
        # Vai BUSCAR a mercadoria, nao entregar. Marcar como "chega hoje" seria
        # prometer ao cliente algo que nao vai acontecer.
        (145, Grupo.PREPARANDO, "A caminho do endereco de coleta"),
        # Pronto nao e o mesmo que em rota.
        (319, Grupo.EM_TRANSITO, "Pronto para entrega"),
        # Logistica reversa: a mercadoria esta voltando para a loja.
        (312, Grupo.DEVOLUCAO, "Em rota para entrega - Reversa"),
        # O pior erro possivel: entregue DE VOLTA ao remetente, jamais verde.
        (314, Grupo.DEVOLUCAO, "Pedido entregue - Reversa"),
        # Entrega parcial nao e desfecho feliz.
        (11, Grupo.ENTREGA_PARCIAL, "Entrega parcial"),
        # Esta no ponto de coleta; o cliente ainda NAO recebeu e precisa buscar.
        (140, Grupo.AGUARDANDO_RETIRADA, "Entregue no ponto de coleta"),
    ],
)
def test_classificacoes_que_uma_leitura_apressada_erra(
    codigo: int, grupo: Grupo, porque: str
) -> None:
    assert classificar(codigo) is grupo, porque


def test_reversa_nunca_e_entregue() -> None:
    """Nenhum codigo de logistica reversa pode ser exibido como entrega concluida."""
    for codigo in (309, 310, 311, 312, 313, 314):
        assert classificar(codigo) is Grupo.DEVOLUCAO


def test_obito_do_destinatario_nao_recebe_tratamento_automatico() -> None:
    """262 = "Entrega nao realizada - destinatario falecido".

    Literalmente e uma tentativa de entrega frustrada, e por isso estava em
    TENTATIVA_FALHA. Mas aquele grupo produz DOIS comportamentos automaticos que
    nao cabem aqui:

    1. Na pagina, o texto "normalmente uma nova tentativa acontece nos proximos
       dias uteis" -- uma promessa para quem acabou de perder alguem.
    2. Com o aviso proativo ligado, um WhatsApp para o telefone do falecido.

    `problema` nao dispara notificacao e nao tem texto de orientacao: o cliente
    ve o rotulo da propria Frete Rapido e a equipe trata o caso a mao. A
    classificacao correta e a que produz o comportamento correto.
    """
    assert classificar(262) is Grupo.PROBLEMA
    assert not exige_acao_do_cliente(classificar(262))


def test_grupos_que_exigem_acao() -> None:
    assert exige_acao_do_cliente(Grupo.AGUARDANDO_RETIRADA)
    assert exige_acao_do_cliente(Grupo.TENTATIVA_FALHA)
    assert not exige_acao_do_cliente(Grupo.EM_TRANSITO)
    assert not exige_acao_do_cliente(Grupo.ENTREGUE)


def test_todo_codigo_mapeado_tem_grupo_valido() -> None:
    for codigo, grupo in POR_CODIGO.items():
        assert isinstance(grupo, Grupo), codigo
        assert grupo is not Grupo.EM_ANDAMENTO, (
            f"codigo {codigo} mapeado explicitamente para o fallback; "
            "remova-o do mapa em vez de classifica-lo como neutro"
        )
