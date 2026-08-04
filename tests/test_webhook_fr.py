"""Webhook da Frete Rapido: segredo, lista de permissao, gatilhos e dedup.

O teste que mais importa aqui e o de REENTREGA. A Frete Rapido reenvia o mesmo
evento ate 12 vezes em ~24h enquanto nao receber HTTP 200, e o pior modo de
falha do projeto e o cliente receber 12 mensagens identicas.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import criar_app
from app.schemas import Grupo, OcorrenciaFR, StatusEvento, WebhookOcorrenciaFR
from app.services.datas import atribuir_fuso
from app.services.eventos import EventosMemoria
from app.services.multi_cnpj import BuscadorMultiCNPJ, ResultadoBusca
from app.services.normalizacao import NumeroPedidoFR
from app.services.notificacao import ServicoNotificacao, montar_payload
from app.services.ordenacao import indexar
from app.services.shopify import PedidoShopify, ShopifyErro

FIXTURES = Path(__file__).parent / "fixtures"
SEGREDO = "s" * 40
ROTA = f"/api/v1/webhook/frete-rapido/{SEGREDO}"
TELEFONE = "+5511988887777"
BEARER = "b" * 48


def payload(**alteracoes: Any) -> dict[str, Any]:
    bruto: dict[str, Any] = json.loads(
        (FIXTURES / "webhook-ocorrencia-232.json").read_text(encoding="utf-8")
    )
    bruto.update(alteracoes)
    return bruto


class ShopifyFalsa:
    def __init__(
        self, pedido: PedidoShopify | None = None, erro: Exception | None = None
    ) -> None:
        self._pedido = pedido
        self._erro = erro
        self.chamadas = 0

    async def buscar_pedido(self, numero: NumeroPedidoFR) -> PedidoShopify | None:
        self.chamadas += 1
        if self._erro is not None:
            raise self._erro
        return self._pedido


class N8nFalso:
    def __init__(self, erro: Exception | None = None) -> None:
        self._erro = erro
        self.enviados: list[dict[str, Any]] = []

    async def enviar(self, payload: dict[str, Any]) -> None:
        if self._erro is not None:
            raise self._erro
        self.enviados.append(payload)


INICIO = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
# Historico plausivel ANTES do evento: contratado, coletado, em transito.
HISTORICO = [0, 15, 2]


class FreteRapidoFalso:
    """Substitui o `BuscadorMultiCNPJ` na confirmacao do evento na fonte.

    Modela ESTADO ATUAL, nao so historico: `atual` e a ocorrencia mais recente,
    e e ela que a confirmacao compara. Foi a distincao que uma revisao de
    seguranca mostrou faltar -- "existe no historico" nao e "e o estado agora".
    """

    def __init__(
        self,
        atual: int | None = None,
        historico: list[int] | None = None,
        erro: Exception | None = None,
        houve_falha: bool = False,
        sem_data: set[int] | None = None,
    ) -> None:
        self._atual = atual
        self._historico = HISTORICO if historico is None else historico
        self._erro = erro
        self._houve_falha = houve_falha
        # Codigos que a Frete Rapido devolve SEM `data_ocorrencia` -- o schema
        # aceita, e a ordenacao os empurra para o fim da lista.
        self._sem_data = sem_data or set()
        # Cada consulta feita, com o CNPJ pedido. A confirmacao NAO pode
        # consultar mais de um token por evento.
        self.consultas: list[str | None] = []

    def avancar(self, codigo: int) -> None:
        """A encomenda avanca: o estado atual vira historico e `codigo` assume."""
        if self._atual is not None:
            self._historico = [*self._historico, self._atual]
        self._atual = codigo

    async def buscar_no_cnpj(
        self, numero: NumeroPedidoFR, cnpj: str | None
    ) -> ResultadoBusca:
        self.consultas.append(cnpj)
        if self._erro is not None:
            raise self._erro

        codigos = list(self._historico)
        if self._atual is not None:
            codigos.append(self._atual)
        # Datas crescentes: a ultima da lista e a mais recente.
        ocorrencias = indexar(
            [
                OcorrenciaFR(
                    codigo=c,
                    data_ocorrencia=(
                        None if c in self._sem_data else INICIO + timedelta(hours=i)
                    ),
                    razao_social_transportadora="EMPRESA BRASILEIRA DE CORREIOS E TELEGRAFOS",
                )
                for i, c in enumerate(codigos)
            ]
        )
        return ResultadoBusca(
            ocorrencias=ocorrencias, houve_falha=self._houve_falha
        )


def pedido(**kw: Any) -> PedidoShopify:
    base: dict[str, Any] = {
        "id": "gid://shopify/Order/1",
        "name": "#59552",
        "email_normalizado": "cliente@exemplo.com",
        "criado_em": None,
        "tem_fulfillment": True,
        "codigo_rastreio": "FR260723D6KTG",
        "telefone": TELEFONE,
        "nome_cliente": "Daniel",
    }
    base.update(kw)
    return PedidoShopify(**base)


def settings(**kw: Any) -> Settings:
    # Com `notificacao_ativa=True` a validacao de boot exige TODAS as barreiras
    # -- e essa exigencia e ela propria um teste: se alguma sair daqui, os
    # cenarios de envio param de subir.
    base: dict[str, Any] = {
        "fr_webhook_segredo": SEGREDO,
        "fr_webhook_bearer": BEARER,
        "notificacao_grupos": "aguardando_retirada,tentativa_falha",
        "notificacao_ativa": True,
        "n8n_webhook_url": "https://n8n.exemplo/webhook/fr",
        "n8n_webhook_token": "t" * 40,
    }
    base.update(kw)
    return Settings(**base)


def servico(
    *,
    shopify: ShopifyFalsa | None = None,
    n8n: N8nFalso | None = None,
    s: Settings | None = None,
    fr: FreteRapidoFalso | None = None,
    codigo_atual: int = 232,
) -> tuple[ServicoNotificacao, ShopifyFalsa, N8nFalso]:
    sh = shopify or ShopifyFalsa(pedido())
    fila = n8n or N8nFalso()
    # Por padrao a Frete Rapido confirma o evento em questao: os testes que NAO
    # tratam de verificacao nao devem tropecar nela.
    servico = ServicoNotificacao(
        shopify=sh,  # type: ignore[arg-type]
        eventos=EventosMemoria(),
        n8n=fila,  # type: ignore[arg-type]
        settings=s or settings(),
        frete_rapido=fr or FreteRapidoFalso(atual=codigo_atual),  # type: ignore[arg-type]
    )
    return servico, sh, fila


def avancar_tempo(svc: ServicoNotificacao, **delta: float) -> None:
    """Simula a passagem do tempo, empurrando os registros para tras.

    Necessario porque varias regras sao por JANELA -- cooldown, agregacao de
    volume, anti-spam -- e testa-las de verdade exige tempo passando, nao
    `sleep`.
    """
    d = timedelta(**delta)
    for linha in svc._eventos._linhas.values():  # type: ignore[attr-defined]
        linha.recebido_em -= d
        for campo in ("aviso_reservado_em", "proxima_tentativa_em", "processando_ate"):
            valor = getattr(linha, campo)
            if valor is not None:
                setattr(linha, campo, valor - d)


async def processar(bruto: dict[str, Any], **kw: Any) -> Any:
    kw.setdefault("codigo_atual", int(bruto["codigo"]))
    svc, sh, fila = servico(**kw)
    desfecho = await svc.processar(WebhookOcorrenciaFR.model_validate(bruto))
    return desfecho, sh, fila


# --------------------------------------------------------------------------
# Lista de permissao (LGPD)
# --------------------------------------------------------------------------


def test_lista_de_permissao_descarta_dado_pessoal_de_terceiro() -> None:
    """TESTE OBRIGATORIO, espelho do que existe para o fluxo de polling.

    O payload do webhook traz comprovante com assinatura de quem recebeu, chave
    de acesso da NF-e e metadados arbitrarios do ERP -- neste caso, um CPF.
    Nada disso pode sobreviver ao parsing.
    """
    evento = WebhookOcorrenciaFR.model_validate(payload())

    serializado = json.dumps(evento.model_dump(), default=str)
    assert "comprovantes" not in serializado
    assert "notas_fiscais" not in serializado
    assert "metadados" not in serializado
    assert "canhoto" not in serializado
    # Os valores concretos, nao so os nomes dos campos.
    assert "123.456.789-00" not in serializado
    assert "35260712345678000199550010001234561234567890" not in serializado


def test_campos_uteis_sobrevivem() -> None:
    evento = WebhookOcorrenciaFR.model_validate(payload())

    assert evento.codigo == 232
    assert evento.numero_pedido == "59552"
    assert evento.id_frete == "FR260723D6KTG"
    assert evento.nome == "Disponivel para retirada nos Correios"
    assert evento.prazo_devolucao == "2026-08-12"
    assert evento.nome_transportadora == "Correios"


def test_codigo_aceita_inteiro_e_string_numerica() -> None:
    """A documentacao mostra inteiro; o polling ja mostrou as duas formas."""
    assert WebhookOcorrenciaFR.model_validate(payload(codigo="232")).codigo == 232


def test_nome_transportadora_cai_na_razao_social_quando_falta_fantasia() -> None:
    bruto = payload()
    bruto["transportadora"]["nome_fantasia"] = ""
    evento = WebhookOcorrenciaFR.model_validate(bruto)
    assert evento.nome_transportadora == "EMPRESA BRASILEIRA DE CORREIOS E TELEGRAFOS"


# --------------------------------------------------------------------------
# Gatilhos configuraveis
# --------------------------------------------------------------------------


async def test_grupo_configurado_dispara() -> None:
    desfecho, _, fila = await processar(payload())

    assert desfecho.status is StatusEvento.ENVIADO
    assert desfecho.grupo is Grupo.AGUARDANDO_RETIRADA
    assert len(fila.enviados) == 1


async def test_grupo_fora_da_configuracao_nao_dispara() -> None:
    """Codigo 3 = Entregue. A FR manda webhook para TODA ocorrencia."""
    desfecho, sh, fila = await processar(payload(codigo=3))

    assert desfecho.status is StatusEvento.DESCARTADO
    assert fila.enviados == []
    # Filtrar ANTES da Shopify: a maioria esmagadora dos eventos cai aqui.
    assert sh.chamadas == 0


async def test_configuracao_restrita_exclui_o_outro_grupo() -> None:
    s = settings(notificacao_grupos="aguardando_retirada")

    retirada, _, fila_a = await processar(payload(), s=s)
    # 32 = Destinatario ausente, do grupo tentativa_falha.
    ausente, _, fila_b = await processar(payload(codigo=32), s=s)

    assert retirada.status is StatusEvento.ENVIADO
    assert ausente.status is StatusEvento.DESCARTADO
    assert len(fila_a.enviados) == 1
    assert fila_b.enviados == []


async def test_codigo_ignorado_tem_precedencia_sobre_o_grupo() -> None:
    desfecho, _, fila = await processar(
        payload(), s=settings(notificacao_codigos_ignorados="232")
    )

    assert desfecho.status is StatusEvento.DESCARTADO
    assert fila.enviados == []


async def test_codigo_extra_dispara_fora_de_qualquer_grupo() -> None:
    """Valvula de escape: 3 = Entregue, que nao esta em grupo configurado."""
    desfecho, _, fila = await processar(
        payload(codigo=3), s=settings(notificacao_codigos_extra="3")
    )

    assert desfecho.status is StatusEvento.ENVIADO
    assert len(fila.enviados) == 1


def test_grupo_inexistente_derruba_o_boot() -> None:
    """Falhar FECHADO: um erro de digitacao que virasse conjunto vazio
    desligaria os avisos em silencio, e ninguem descobre um aviso que nao saiu."""
    with pytest.raises(ValueError, match="grupo inexistente"):
        settings(notificacao_grupos="aguardando_retira")


def test_codigo_nao_numerico_derruba_o_boot() -> None:
    with pytest.raises(ValueError, match="nao numerico"):
        settings(notificacao_codigos_ignorados="232;140")


def test_envio_ligado_sem_gatilho_derruba_o_boot() -> None:
    with pytest.raises(ValueError, match="nada dispararia"):
        settings(notificacao_grupos="", notificacao_codigos_extra="")


def test_segredo_curto_derruba_o_boot() -> None:
    with pytest.raises(ValueError, match="curto demais"):
        settings(fr_webhook_segredo="curto")


# --------------------------------------------------------------------------
# Deduplicacao -- o caso que a Frete Rapido exercita de verdade
# --------------------------------------------------------------------------


async def test_reentrega_do_mesmo_evento_envia_uma_unica_vez() -> None:
    """TESTE OBRIGATORIO.

    A FR reenvia ate 12 vezes em ~24h enquanto nao receber 200. Sem a reserva,
    cada reentrega viraria uma mensagem nova.
    """
    svc, _, fila = servico()
    evento = WebhookOcorrenciaFR.model_validate(payload())

    desfechos = [await svc.processar(evento) for _ in range(12)]

    assert len(fila.enviados) == 1
    assert desfechos[0].status is StatusEvento.ENVIADO
    # As reentregas respondem 200 para encerrar a escada da FR.
    assert all(d.status_http == 200 for d in desfechos)


async def test_dedup_vale_com_data_de_ocorrencia_NULA() -> None:
    """Regressao do NULLS NOT DISTINCT.

    No Postgres, NULL e distinto de NULL num indice unico por padrao: sem a
    clausula, dois eventos sem `data_ocorrencia` furariam a restricao e o
    cliente receberia a mensagem duas vezes.
    """
    svc, _, fila = servico()
    evento = WebhookOcorrenciaFR.model_validate(payload(data_ocorrencia=None))

    await svc.processar(evento)
    await svc.processar(evento)

    assert len(fila.enviados) == 1


async def test_ocorrencia_nova_no_mesmo_pedido_dispara_de_novo() -> None:
    """A dedup nao pode calar um evento legitimamente novo."""
    fr = FreteRapidoFalso(atual=232)
    svc, _, fila = servico(fr=fr)

    await svc.processar(WebhookOcorrenciaFR.model_validate(payload()))

    # Dias depois a encomenda volta para tentativa de entrega.
    fr.avancar(32)
    await svc.processar(
        WebhookOcorrenciaFR.model_validate(
            payload(codigo=32, data_ocorrencia="2026-08-04 09:00:00")
        )
    )

    assert len(fila.enviados) == 2


async def test_uma_ocorrencia_por_volume_gera_UM_aviso() -> None:
    """Observado em producao no pedido 60422, e por isso esta suite existe.

    Quatro ocorrencias do mesmo codigo em 21 minutos, uma por volume da remessa.
    Datas distintas de verdade, entao a dedup normal (que inclui a data) nao
    pega. Se acontecesse com "disponivel para retirada", o cliente receberia
    varios avisos identicos para buscar a MESMA encomenda.
    """
    svc, _, fila = servico()

    # Mesmos instantes reais do pedido 60422, so que com codigo acionavel.
    for hora, minuto, seg in [(12, 53, 21), (12, 58, 38), (13, 3, 53), (13, 14, 51)]:
        await svc.processar(
            WebhookOcorrenciaFR.model_validate(
                payload(data_ocorrencia=f"2026-08-04 {hora:02d}:{minuto:02d}:{seg:02d}")
            )
        )

    assert len(fila.enviados) == 1


async def test_mesmo_codigo_depois_da_janela_avisa_de_novo() -> None:
    """O caso oposto, e por isso a regra e por JANELA e nao absoluta.

    "Destinatario ausente" na segunda e de novo na quarta sao duas tentativas de
    entrega diferentes -- o cliente precisa saber das duas.
    """
    svc, _, fila = servico(codigo_atual=32)
    evento_a = WebhookOcorrenciaFR.model_validate(
        payload(codigo=32, data_ocorrencia="2026-08-01 09:00:00")
    )
    await svc.processar(evento_a)

    avancar_tempo(svc, hours=48)

    await svc.processar(
        WebhookOcorrenciaFR.model_validate(
            payload(codigo=32, data_ocorrencia="2026-08-03 09:00:00")
        )
    )

    assert len(fila.enviados) == 2


async def test_codigo_DIFERENTE_no_mesmo_pedido_avisa() -> None:
    """A regra e por codigo, nao por pedido: dois fatos distintos, dois avisos."""
    fr = FreteRapidoFalso(atual=232)
    svc, _, fila = servico(fr=fr)

    await svc.processar(WebhookOcorrenciaFR.model_validate(payload()))
    fr.avancar(32)
    await svc.processar(
        WebhookOcorrenciaFR.model_validate(
            payload(codigo=32, data_ocorrencia="2026-08-04 09:00:00")
        )
    )

    assert len(fila.enviados) == 2


async def test_trava_anti_spam_contem_rajada_no_mesmo_pedido() -> None:
    """Uma transportadora que posta varios codigos seguidos nao vira varios avisos.

    Aqui os codigos sao DIFERENTES -- a repeticao do mesmo codigo ja e barrada
    antes, por outra regra. Este e o teto geral, que existe para o caso em que a
    encomenda muda de estado varias vezes em poucas horas.
    """
    fr = FreteRapidoFalso(atual=232)
    svc, _, fila = servico(fr=fr, s=settings(notificacao_max_por_pedido=2))

    for i, codigo in enumerate([232, 32, 5, 38]):
        if i:
            fr.avancar(codigo)
        await svc.processar(
            WebhookOcorrenciaFR.model_validate(
                payload(codigo=codigo, data_ocorrencia=f"2026-08-0{i + 1} 09:00:00")
            )
        )

    assert len(fila.enviados) == 2


# --------------------------------------------------------------------------
# Contato e desfechos terminais
# --------------------------------------------------------------------------


async def test_pedido_sem_telefone_e_terminal_com_200() -> None:
    """`sem_contato` NAO e falha: insistir gastaria as 12 tentativas da FR
    num evento que nunca podera ser entregue."""
    desfecho, _, fila = await processar(
        payload(), shopify=ShopifyFalsa(pedido(telefone=None))
    )

    assert desfecho.status is StatusEvento.SEM_CONTATO
    assert desfecho.status_http == 200
    assert fila.enviados == []


async def test_pedido_inexistente_na_shopify_e_descartado() -> None:
    """Webhook forjado ou numero de outra operacao."""
    desfecho, _, fila = await processar(payload(), shopify=ShopifyFalsa(None))

    assert desfecho.status is StatusEvento.DESCARTADO
    assert desfecho.status_http == 200
    assert fila.enviados == []


async def test_shopify_fora_do_ar_pede_reenvio_com_503() -> None:
    """Aqui o 503 vale a pena: o aviso nao pode ser PERDIDO por indisponibilidade."""
    desfecho, _, fila = await processar(
        payload(), shopify=ShopifyFalsa(erro=ShopifyErro("indisponivel"))
    )

    assert desfecho.status is StatusEvento.PENDENTE
    assert desfecho.status_http == 503
    assert fila.enviados == []


async def test_n8n_fora_do_ar_pede_reenvio_e_depois_conclui() -> None:
    """A reentrega da FR precisa REPROCESSAR um evento que ficou pendente."""
    from app.services.n8n import N8nErro

    quebrado = N8nFalso(erro=N8nErro("connect error"))
    svc = ServicoNotificacao(
        shopify=ShopifyFalsa(pedido()),  # type: ignore[arg-type]
        eventos=EventosMemoria(),
        n8n=quebrado,  # type: ignore[arg-type]
        settings=settings(),
        frete_rapido=FreteRapidoFalso(atual=232),  # type: ignore[arg-type]
    )
    evento = WebhookOcorrenciaFR.model_validate(payload())

    primeira = await svc.processar(evento)
    assert primeira.status is StatusEvento.PENDENTE
    assert primeira.status_http == 503

    # O n8n volta. A reentrega da FR so e aceita depois do cooldown -- que
    # existe para impedir reconsulta ilimitada da mesma linha pendente.
    svc._n8n = N8nFalso()  # type: ignore[assignment]
    avancar_tempo(svc, minutes=5)
    segunda = await svc.processar(evento)

    assert segunda.status is StatusEvento.ENVIADO
    assert segunda.status_http == 200


# --------------------------------------------------------------------------
# Confirmacao na fonte -- o webhook nao e assinado
# --------------------------------------------------------------------------


async def test_evento_confirmado_pela_frete_rapido_e_enviado() -> None:
    desfecho, _, fila = await processar(payload(), fr=FreteRapidoFalso(atual=232))

    assert desfecho.status is StatusEvento.ENVIADO
    assert len(fila.enviados) == 1


async def test_evento_NAO_confirmado_nao_vira_mensagem() -> None:
    """O caso que a verificacao existe para cobrir.

    O segredo da URL prova que quem chamou o conhece -- nao que o evento
    aconteceu. Um payload forjado com codigo 232 mandaria o cliente a agencia
    buscar um pacote que nao esta la.
    """
    desfecho, _, fila = await processar(
        payload(), fr=FreteRapidoFalso(atual=2)  # 232 nunca aconteceu
    )

    assert desfecho.status is StatusEvento.PENDENTE
    assert fila.enviados == []
    assert desfecho.detalhe is not None
    assert "nao existe" in desfecho.detalhe


async def test_replay_de_ocorrencia_HISTORICA_nao_vira_mensagem() -> None:
    """REGRESSAO da falha CRITICA apontada em revisao de seguranca.

    A versao anterior perguntava "este codigo existe no historico?". O endpoint
    devolve o HISTORICO INTEIRO, entao um "disponivel para retirada" de dias
    atras confirmava para sempre -- bastava reproduzir o evento antigo num
    pedido JA ENTREGUE para mandar o cliente a agencia a toa.

    A pergunta certa e sobre o estado ATUAL.
    """
    # A encomenda passou por 232, mas ja foi entregue (codigo 3).
    fr = FreteRapidoFalso(atual=3, historico=[0, 15, 2, 232])

    desfecho, _, fila = await processar(payload(codigo=232), fr=fr)

    assert fila.enviados == []
    assert desfecho.status is StatusEvento.DESCARTADO
    assert desfecho.detalhe is not None
    assert "seguiu adiante" in desfecho.detalhe


async def test_ocorrencia_que_seguiu_adiante_encerra_com_200() -> None:
    """Nao adianta reenviar: a encomenda nao volta ao estado anterior.

    Diferente do "codigo nao existe", que pode ser defasagem de propagacao e
    merece nova tentativa.
    """
    fr = FreteRapidoFalso(atual=3, historico=[232])

    desfecho, _, _ = await processar(payload(codigo=232), fr=fr)

    assert desfecho.status_http == 200


async def test_data_forjada_no_payload_nao_cria_aviso_novo() -> None:
    """Variar `data_ocorrencia` gera chave de dedup nova, mas nao mensagem falsa.

    Com o estado atual conferido, o pior que um replay consegue e repetir uma
    mensagem VERDADEIRA -- e a trava anti-spam limita isso.
    """
    fr = FreteRapidoFalso(atual=3, historico=[232])
    svc, _, fila = servico(fr=fr)

    for dia in range(1, 6):
        await svc.processar(
            WebhookOcorrenciaFR.model_validate(
                payload(codigo=232, data_ocorrencia=f"2026-08-0{dia} 10:00:00")
            )
        )

    assert fila.enviados == []


async def test_entregas_simultaneas_do_mesmo_evento_enviam_uma_vez() -> None:
    """REGRESSAO da corrida apontada em revisao de seguranca.

    `ON CONFLICT` arbitra quem cria a LINHA, nao quem executa o EFEITO. Sem o
    lease, uma requisicao inseria e a outra lia `pendente` -- e ambas seguiam,
    porque `pendente` significava tanto "alguem esta processando" quanto "pode
    tentar de novo".
    """
    import asyncio

    svc, _, fila = servico()
    evento = WebhookOcorrenciaFR.model_validate(payload())

    await asyncio.gather(*(svc.processar(evento) for _ in range(8)))

    assert len(fila.enviados) == 1


async def test_rajada_simultanea_no_mesmo_pedido_respeita_a_cota() -> None:
    """A cota contada FORA da transacao deixava N corridas verem zero.

    Cinco eventos distintos do mesmo pedido, todos ao mesmo tempo, com teto 2.
    """
    import asyncio

    fr = FreteRapidoFalso(atual=32)
    svc, _, fila = servico(fr=fr, s=settings(notificacao_max_por_pedido=2))

    eventos = [
        WebhookOcorrenciaFR.model_validate(
            payload(codigo=32, data_ocorrencia=f"2026-08-0{d} 09:00:00")
        )
        for d in range(1, 6)
    ]
    await asyncio.gather(*(svc.processar(e) for e in eventos))

    assert len(fila.enviados) <= 2


async def test_sem_data_nao_vira_desfecho_terminal() -> None:
    """REGRESSAO da rodada 2 da revisao.

    `ordenar_desc` empurra ocorrencia SEM DATA para o fim da lista -- e uma
    convencao de apresentacao, nao prova de que e antiga. Tratar isso como
    "seguiu adiante" descartaria com 200 um aviso legitimo cuja unica falha e
    nao ter timestamp, e ele se perderia em silencio.
    """
    # 232 chegou agora, mas sem `data_ocorrencia`: a ordenacao o joga para o fim
    # e ele parece historico.
    fr = FreteRapidoFalso(atual=2, historico=[0, 232], sem_data={232})

    desfecho, _, fila = await processar(payload(codigo=232), fr=fr)

    # Sem data comparavel, a duvida vira reenvio, nao descarte.
    assert desfecho.status is StatusEvento.PENDENTE
    assert fila.enviados == []


async def test_repeticao_da_mesma_linha_pendente_respeita_cooldown() -> None:
    """REGRESSAO da amplificacao 1-para-1 apontada na rodada 2.

    O teto de tentativas so barra linha NOVA. Repetir a MESMA linha pendente
    passava direto, e como `concluir` libera o lease, cada repeticao readquiria
    e consultava a Frete Rapido de novo -- sem limite, na mesma cota da pagina.
    """
    fr = FreteRapidoFalso(atual=2)  # nunca confirma: fica pendente
    svc, _, _ = servico(fr=fr)
    evento = WebhookOcorrenciaFR.model_validate(payload())

    for _ in range(10):
        await svc.processar(evento)

    # Uma consulta na primeira vez; as demais param no cooldown.
    assert len(fr.consultas) == 1


async def test_evento_em_processamento_pede_reenvio_com_503() -> None:
    """REGRESSAO da rodada 2: 200 aqui PERDE o evento.

    HTTP 200 e terminal para a Frete Rapido -- ela para de tentar. Se o processo
    que detem o lease morrer depois disso, o lease expira e ninguem reassume:
    nao ha fila local. Com 503 ela continua tentando.
    """
    from app.services.eventos import ChaveEvento

    svc, _, _ = servico()
    evento = WebhookOcorrenciaFR.model_validate(payload())
    chave = ChaveEvento("59552", 232, atribuir_fuso(evento.data_ocorrencia))

    # Simula outro processo com o lease vivo.
    await svc._eventos.adquirir(  # type: ignore[attr-defined]
        chave,
        Grupo.AGUARDANDO_RETIRADA,
        dono="outro-processo",
        lease_s=120,
        cooldown_s=45,
        desde=datetime.now(UTC) - timedelta(hours=6),
        max_tentativas=20,
    )

    desfecho = await svc.processar(evento)

    assert desfecho.status is StatusEvento.PENDENTE
    assert desfecho.status_http == 503


async def test_worker_com_lease_vencido_nao_sobrescreve_o_novo_dono() -> None:
    """REGRESSAO do fencing (rodada 2).

    `concluir` filtrava so pela chave: um worker cujo lease venceu apagava o
    lease de quem assumiu depois, e o novo dono seguia sobre linha sobrescrita.
    """
    from app.services.eventos import ChaveEvento, EventosMemoria

    eventos = EventosMemoria()
    chave = ChaveEvento("59552", 232, None)
    comum = {
        "lease_s": 120,
        "cooldown_s": 45,
        "desde": datetime.now(UTC) - timedelta(hours=6),
        "max_tentativas": 20,
    }

    await eventos.adquirir(chave, Grupo.AGUARDANDO_RETIRADA, dono="A", **comum)
    # O lease de A vence e B assume.
    linha = eventos._linhas[chave]  # type: ignore[attr-defined]
    linha.processando_ate = datetime.now(UTC) - timedelta(seconds=1)
    linha.proxima_tentativa_em = None
    await eventos.adquirir(chave, Grupo.AGUARDANDO_RETIRADA, dono="B", **comum)

    # A, atrasado, tenta concluir.
    await eventos.concluir(chave, StatusEvento.ENVIADO, dono="A")

    assert eventos._linhas[chave].status is StatusEvento.PENDENTE  # type: ignore[attr-defined]
    assert eventos._linhas[chave].dono == "B"  # type: ignore[attr-defined]
    assert await eventos.renovar(chave, dono="B", lease_s=120) is True
    assert await eventos.renovar(chave, dono="A", lease_s=120) is False


async def test_evento_forjado_nao_ocupa_vaga_de_aviso() -> None:
    """REGRESSAO do envenenamento de cota (rodada 2).

    Com a cota tomada ANTES da confirmacao, tres eventos forjados enchiam as
    vagas e o legitimo que chegasse junto era descartado sem nunca ser
    consultado.
    """
    fr = FreteRapidoFalso(atual=232)
    svc, _, fila = servico(fr=fr, s=settings(notificacao_max_por_pedido=1))

    # Tres forjados: codigo acionavel que a fonte nao confirma como atual.
    for d in range(1, 4):
        await svc.processar(
            WebhookOcorrenciaFR.model_validate(
                payload(codigo=32, data_ocorrencia=f"2026-08-0{d} 09:00:00")
            )
        )
    assert fila.enviados == []

    # O legitimo ainda encontra vaga.
    desfecho = await svc.processar(WebhookOcorrenciaFR.model_validate(payload()))

    assert desfecho.status is StatusEvento.ENVIADO
    assert len(fila.enviados) == 1


async def test_nao_confirmado_e_PENDENTE_e_nao_descarte() -> None:
    """O webhook pode chegar antes de a leitura refletir o evento.

    Como `pendente` responde 503, a escada de reentrega da propria Frete Rapido
    (1, 2, 3, 5, 10 min...) resolve o atraso de propagacao sozinha. Descartar
    perderia um aviso legitimo por alguns segundos de diferenca.
    """
    desfecho, _, _ = await processar(payload(), fr=FreteRapidoFalso(atual=2))

    assert desfecho.status_http == 503


async def test_evento_forjado_nunca_toca_no_contato_do_cliente() -> None:
    """Privacidade, nao so seguranca.

    A confirmacao roda ANTES da Shopify: nao buscamos o telefone de ninguem com
    base num evento que ainda nao sabemos se e real.
    """
    desfecho, sh, fila = await processar(payload(), fr=FreteRapidoFalso(atual=2))

    assert desfecho.status is StatusEvento.PENDENTE
    assert sh.chamadas == 0
    assert fila.enviados == []


async def test_frete_rapido_fora_do_ar_pede_reenvio() -> None:
    from app.services.frete_rapido import FreteRapidoErro

    desfecho, _, fila = await processar(
        payload(), fr=FreteRapidoFalso(erro=FreteRapidoErro("indisponivel"))
    )

    assert desfecho.status is StatusEvento.PENDENTE
    assert desfecho.status_http == 503
    assert fila.enviados == []


async def test_confirmacao_usa_o_token_do_cnpj_que_recebeu_o_evento() -> None:
    """Ja sabemos o CNPJ pelo segredo da URL: nao dependemos da tag da Shopify."""
    fr = FreteRapidoFalso(atual=232)
    svc, _, _ = servico(fr=fr)

    await svc.processar(WebhookOcorrenciaFR.model_validate(payload()), cnpj="melhores")

    # UMA consulta, e no CNPJ certo. Ver o teste de amplificacao abaixo.
    assert fr.consultas == ["melhores"]


class ClienteFRContador:
    """Conta chamadas por token, para medir a amplificacao real."""

    def __init__(self) -> None:
        self.tokens_consultados: list[str] = []

    async def buscar_ocorrencias(
        self, numero: NumeroPedidoFR, token: str
    ) -> list[OcorrenciaFR]:
        self.tokens_consultados.append(token)
        return []  # pedido inexistente: e o caso que disparava o fallback


async def test_confirmacao_nao_consulta_os_outros_CNPJs() -> None:
    """REGRESSAO da amplificacao de cota apontada em revisao de seguranca.

    `buscar()` cai no fallback quando o token indicado volta vazio, consultando
    os outros dois. Um evento forjado para pedido inexistente custava 3 chamadas
    -- e a cota da Frete Rapido (720/min) e a MESMA da pagina de rastreio.
    Repetir eventos forjados no teto da rota (300/min) daria 900 chamadas/min e
    derrubaria o fluxo principal.

    O fallback tambem furava o isolamento: segredo vazado do CNPJ A confirmando
    pedidos de B e C.
    """
    cliente = ClienteFRContador()
    buscador = BuscadorMultiCNPJ(
        cliente,  # type: ignore[arg-type]
        {"grudado": "tok-g", "melhores": "tok-m", "tudo": "tok-t"},
    )

    await buscador.buscar_no_cnpj(NumeroPedidoFR("59552"), "melhores")

    assert cliente.tokens_consultados == ["tok-m"]


async def test_consulta_do_cliente_MANTEM_o_fallback() -> None:
    """O fluxo da pagina e outro caso: ali a tag pode estar errada, e o custo de
    confiar cegamente e responder "nao despachado" para um pedido que existe."""
    cliente = ClienteFRContador()
    buscador = BuscadorMultiCNPJ(
        cliente,  # type: ignore[arg-type]
        {"grudado": "tok-g", "melhores": "tok-m", "tudo": "tok-t"},
    )

    await buscador.buscar(NumeroPedidoFR("59552"), ["melhores"])

    assert len(cliente.tokens_consultados) == 3


async def test_cnpj_sem_token_configurado_falha_alto() -> None:
    """`FR_WEBHOOK_SEGREDOS` e `FRETE_RAPIDO_TOKENS` divergindo.

    Confirmar por outro token seria exatamente o furo de isolamento que este
    caminho existe para fechar -- melhor recusar e aparecer no relatorio.
    """
    from app.services.frete_rapido import FreteRapidoErro

    buscador = BuscadorMultiCNPJ(
        ClienteFRContador(),  # type: ignore[arg-type]
        {"grudado": "tok-g", "melhores": "tok-m"},
    )

    with pytest.raises(FreteRapidoErro, match="nao tem token configurado"):
        await buscador.buscar_no_cnpj(NumeroPedidoFR("59552"), "tudo")


async def test_sem_cnpj_com_varios_tokens_recusa_confirmar() -> None:
    """Segredo avulso com 3 CNPJs: nao ha como escolher sem adivinhar."""
    from app.services.frete_rapido import FreteRapidoErro

    buscador = BuscadorMultiCNPJ(
        ClienteFRContador(),  # type: ignore[arg-type]
        {"grudado": "tok-g", "melhores": "tok-m"},
    )

    with pytest.raises(FreteRapidoErro, match="sem CNPJ identificado"):
        await buscador.buscar_no_cnpj(NumeroPedidoFR("59552"), None)


def test_desligar_a_verificacao_com_envio_ligado_derruba_o_boot() -> None:
    """"Envia sem confirmar" nao pode ser um estado alcancavel.

    Sem a confirmacao, o TEXTO que chega ao cliente volta a vir de quem chamou a
    rota -- que e exatamente a falha critica que a revisao apontou. O
    interruptor de emergencia existe, mas exige desligar o envio junto.
    """
    with pytest.raises(ValueError, match="exige desligar tambem o envio"):
        settings(notificacao_verificar_na_fonte=False)


async def test_verificacao_desligada_pula_a_consulta() -> None:
    """Interruptor de emergencia, caso as duas APIs deles divirjam algum dia.

    Com ele, a Fase 1 segue medindo sem consultar a Frete Rapido -- util se a
    cota estiver sob pressao.
    """
    fr = FreteRapidoFalso(atual=2)  # nao confirmaria
    desfecho, _, fila = await processar(
        payload(),
        fr=fr,
        s=settings(
            notificacao_verificar_na_fonte=False,
            notificacao_ativa=False,
            n8n_webhook_url="",
            n8n_webhook_token="",
        ),
    )

    assert desfecho.status is StatusEvento.OBSERVADO
    assert fila.enviados == []
    assert fr.consultas == []


def test_envio_ligado_exige_bearer() -> None:
    """O segredo da URL vaza para o log de acesso do proxy; o Bearer nao."""
    with pytest.raises(ValueError, match="exige FR_WEBHOOK_BEARER"):
        settings(fr_webhook_bearer="")


def test_envio_ligado_exige_token_do_n8n() -> None:
    with pytest.raises(ValueError, match="exige N8N_WEBHOOK_TOKEN"):
        settings(n8n_webhook_token="")


def test_cnpj_de_webhook_sem_token_da_frete_rapido_derruba_o_boot() -> None:
    """Divergencia entre os dois mapas = evento que nunca confirma, em silencio."""
    with pytest.raises(ValueError, match="sem token em FRETE_RAPIDO_TOKENS"):
        settings(
            fr_webhook_segredo="",
            fr_webhook_segredos={"inexistente": "z" * 40},
        )


async def test_confirmacao_roda_antes_do_interruptor_de_envio() -> None:
    """Na Fase 1 a verificacao TEM de rodar, senao a medicao mente.

    Se ela so entrasse junto com o envio, o volume medido em observacao seria
    maior que o real -- e a Fase 2 comecaria com expectativa errada.
    """
    fr = FreteRapidoFalso(atual=232)
    desfecho, _, fila = await processar(
        payload(),
        fr=fr,
        s=settings(notificacao_ativa=False, n8n_webhook_url=""),
    )

    assert desfecho.status is StatusEvento.OBSERVADO
    assert fila.enviados == []
    assert fr.consultas == [None]


# --------------------------------------------------------------------------
# Modo observacao (Fase 1)
# --------------------------------------------------------------------------


async def test_notificacao_desligada_observa_sem_enviar() -> None:
    """TESTE OBRIGATORIO da Fase 1: a API nao fala com cliente nenhum.

    Mesmo assim a Shopify E consultada -- e isso que da a medicao real de
    quantos pedidos tem telefone utilizavel.
    """
    desfecho, sh, fila = await processar(
        payload(),
        s=settings(notificacao_ativa=False, n8n_webhook_url=""),
    )

    assert desfecho.status is StatusEvento.OBSERVADO
    assert desfecho.status_http == 200
    assert fila.enviados == []
    assert sh.chamadas == 1


async def test_modo_observacao_distingue_descartado_de_observado() -> None:
    """`observado` e o que dimensiona o volume antes de ligar o envio."""
    s = settings(notificacao_ativa=False, n8n_webhook_url="")

    interessa, _, _ = await processar(payload(), s=s)
    ignora, _, _ = await processar(payload(codigo=3), s=s)

    assert interessa.status is StatusEvento.OBSERVADO
    assert ignora.status is StatusEvento.DESCARTADO


# --------------------------------------------------------------------------
# Contrato com o n8n
# --------------------------------------------------------------------------


def _confirmada() -> OcorrenciaFR:
    """Como a ocorrencia chega da API da Frete Rapido."""
    return OcorrenciaFR(
        codigo=232,
        nome="Disponivel para retirada nos Correios",
        data_ocorrencia=datetime(2026, 8, 3, 15, 37, 12),
        razao_social_transportadora="EMPRESA BRASILEIRA DE CORREIOS E TELEGRAFOS",
    )


def test_payload_do_n8n_leva_o_minimo_de_dado_pessoal() -> None:
    saida = montar_payload(
        NumeroPedidoFR("59552"),
        _confirmada(),
        Grupo.AGUARDANDO_RETIRADA,
        TELEFONE,
        "Daniel",
    )

    assert saida["telefone"] == TELEFONE
    assert saida["primeiro_nome"] == "Daniel"
    assert saida["grupo"] == "aguardando_retirada"
    assert saida["pedido"] == "59552"
    # O rotulo e o `nome` da propria Frete Rapido; nao traduzimos.
    assert saida["rotulo"] == "Disponivel para retirada nos Correios"
    assert saida["transportadora"] == "Correios"

    # Nada alem de telefone e primeiro nome sai daqui: o n8n retem os dados de
    # execucao no banco dele, fora do alcance do expurgo LGPD.
    texto = json.dumps(saida, default=str)
    assert "123.456.789-00" not in texto
    assert "cliente@exemplo.com" not in texto
    assert "35260712345678000199550010001234561234567890" not in texto


async def test_nada_do_corpo_do_webhook_chega_ao_cliente() -> None:
    """REGRESSAO da falha CRITICA apontada em revisao de seguranca.

    A versao anterior copiava `nome`, `mensagem`, `transportadora` e os prazos
    direto do payload. Confirmavamos o GATILHO e deixavamos passar o CONTEUDO:
    quem tivesse o segredo escrevia o texto que chegava no WhatsApp do cliente.
    """
    veneno = payload(
        nome="RETIRE HOJE OU PERDE",
        mensagem="Clique em http://golpe.exemplo para liberar sua entrega",
        prazo_devolucao="hoje",
    )
    veneno["transportadora"] = {"nome_fantasia": "Transportadora Falsa"}

    _, _, fila = await processar(veneno)

    assert len(fila.enviados) == 1
    texto = json.dumps(fila.enviados[0], default=str)
    assert "golpe.exemplo" not in texto
    assert "RETIRE HOJE OU PERDE" not in texto
    assert "Transportadora Falsa" not in texto
    # O que sai e o que a Frete Rapido devolveu, e a nossa traducao dela.
    assert fila.enviados[0]["transportadora"] == "Correios"


def test_data_de_ocorrencia_sai_com_fuso() -> None:
    """Data naive sem fuso declarado exibiria o horario errado perto da meia-noite."""
    saida = montar_payload(
        NumeroPedidoFR("59552"),
        _confirmada(),
        Grupo.AGUARDANDO_RETIRADA,
        TELEFONE,
        None,
    )

    assert saida["data_ocorrencia"] is not None
    assert datetime.fromisoformat(str(saida["data_ocorrencia"])).tzinfo is not None


# --------------------------------------------------------------------------
# Rota HTTP: segredo, e o segredo no log
# --------------------------------------------------------------------------


def _app(**kw: Any) -> TestClient:
    import os

    for chave, valor in {
        "FR_WEBHOOK_SEGREDO": SEGREDO,
        "NOTIFICACAO_GRUPOS": "aguardando_retirada,tentativa_falha",
        "NOTIFICACAO_ATIVA": "false",
        **kw,
    }.items():
        os.environ[chave] = str(valor)

    from app.config import get_settings

    get_settings.cache_clear()
    app = criar_app()
    app.state.servico_notificacao = ServicoNotificacao(
        shopify=ShopifyFalsa(pedido()),  # type: ignore[arg-type]
        eventos=EventosMemoria(),
        n8n=N8nFalso(),  # type: ignore[arg-type]
        settings=get_settings(),
        frete_rapido=FreteRapidoFalso(atual=232),  # type: ignore[arg-type]
    )
    return TestClient(app)


@pytest.fixture
def cliente() -> Iterator[TestClient]:
    import os

    with _app() as c:
        yield c
    for chave in ("FR_WEBHOOK_SEGREDO", "NOTIFICACAO_GRUPOS", "NOTIFICACAO_ATIVA"):
        os.environ.pop(chave, None)
    from app.config import get_settings

    get_settings.cache_clear()


def test_segredo_correto_responde_200(cliente: TestClient) -> None:
    r = cliente.post(ROTA, json=payload())

    assert r.status_code == 200
    assert r.json()["status"] == StatusEvento.OBSERVADO
    assert r.headers["cache-control"] == "no-store"


def test_segredo_errado_responde_404(cliente: TestClient) -> None:
    """404 e nao 401: um 401 confirmaria que a rota existe."""
    r = cliente.post(f"/api/v1/webhook/frete-rapido/{'x' * 40}", json=payload())

    assert r.status_code == 404


# --------------------------------------------------------------------------
# Bearer token -- a segunda barreira, configurada no Dash FR
# --------------------------------------------------------------------------


@pytest.fixture
def cliente_com_bearer() -> Iterator[TestClient]:
    import os

    with _app(FR_WEBHOOK_BEARER=BEARER) as c:
        yield c
    for chave in (
        "FR_WEBHOOK_SEGREDO",
        "FR_WEBHOOK_BEARER",
        "NOTIFICACAO_GRUPOS",
        "NOTIFICACAO_ATIVA",
    ):
        os.environ.pop(chave, None)
    from app.config import get_settings

    get_settings.cache_clear()


def test_bearer_correto_passa(cliente_com_bearer: TestClient) -> None:
    r = cliente_com_bearer.post(
        ROTA, json=payload(), headers={"Authorization": f"Bearer {BEARER}"}
    )

    assert r.status_code == 200


def test_bearer_ausente_responde_404_mesmo_com_segredo_certo(
    cliente_com_bearer: TestClient,
) -> None:
    """As duas barreiras sao independentes: uma nao substitui a outra."""
    r = cliente_com_bearer.post(ROTA, json=payload())

    assert r.status_code == 404


def test_bearer_errado_responde_404(cliente_com_bearer: TestClient) -> None:
    r = cliente_com_bearer.post(
        ROTA, json=payload(), headers={"Authorization": f"Bearer {'z' * 48}"}
    )

    assert r.status_code == 404


@pytest.mark.parametrize(
    "cabecalho",
    [
        "bearer {token}",  # o padrao HTTP nao diferencia caixa no esquema
        "BEARER {token}",
        "Bearer  {token}",  # espaco extra
    ],
)
def test_bearer_tolera_variacoes_de_forma(
    cliente_com_bearer: TestClient, cabecalho: str
) -> None:
    """Recusar por causa da caixa daria uma falha confusa, sem ganho nenhum."""
    r = cliente_com_bearer.post(
        ROTA,
        json=payload(),
        headers={"Authorization": cabecalho.format(token=BEARER)},
    )

    assert r.status_code == 200


@pytest.mark.parametrize(
    "cabecalho",
    ["", "Basic dXNlcjpwYXNz", BEARER, f"Token {BEARER}", "Bearer"],
)
def test_bearer_recusa_esquema_errado(
    cliente_com_bearer: TestClient, cabecalho: str
) -> None:
    r = cliente_com_bearer.post(
        ROTA, json=payload(), headers={"Authorization": cabecalho}
    )

    assert r.status_code == 404


def test_sem_bearer_configurado_o_cabecalho_e_ignorado(cliente: TestClient) -> None:
    """Compatibilidade: quem so usa o segredo da URL nao quebra."""
    r = cliente.post(ROTA, json=payload())

    assert r.status_code == 200


# --------------------------------------------------------------------------
# Um segredo por CNPJ -- o cadastro no Dash FR e por CNPJ
# --------------------------------------------------------------------------

SEGREDOS_CNPJ = {
    "grudado": "g" * 40,
    "melhores": "m" * 40,
    "tudo": "t" * 40,
}


def settings_multi(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "fr_webhook_segredo": "",
        "fr_webhook_segredos": SEGREDOS_CNPJ,
        "notificacao_grupos": "aguardando_retirada,tentativa_falha",
        "notificacao_ativa": False,
    }
    base.update(kw)
    return Settings(**base)


@pytest.mark.parametrize("cnpj", list(SEGREDOS_CNPJ))
def test_cada_segredo_identifica_seu_cnpj(cnpj: str) -> None:
    """O payload da FR nao diz de qual embarcador veio -- so o segredo diz."""
    from app.api.v1.webhook_fr import resolver_cnpj

    mapa = settings_multi().segredos_webhook
    assert resolver_cnpj(SEGREDOS_CNPJ[cnpj], mapa) == cnpj


def test_segredo_desconhecido_nao_resolve_cnpj_nenhum() -> None:
    from app.api.v1.webhook_fr import resolver_cnpj

    assert resolver_cnpj("x" * 40, settings_multi().segredos_webhook) is None


def test_segredo_avulso_continua_valendo_sem_cnpj() -> None:
    """Compatibilidade: quem ja tinha o webhook no ar nao pode quebrar num deploy.

    A operacao de CNPJ unico tambem cai aqui -- nao ha o que distinguir.
    """
    from app.api.v1.webhook_fr import resolver_cnpj

    mapa = settings(fr_webhook_segredo=SEGREDO).segredos_webhook
    assert resolver_cnpj(SEGREDO, mapa) == ""


def test_segredo_repetido_entre_cnpjs_derruba_o_boot() -> None:
    """Segredo duplicado tornaria a origem indeterminada -- que e o ponto inteiro.

    E o modo de falha mais provavel: copiar a linha do .env e esquecer de trocar
    o valor. Sem esta trava, dois CNPJs apareceriam como um so no painel.
    """
    with pytest.raises(ValueError, match="segredo repetido"):
        settings_multi(
            fr_webhook_segredos={"grudado": "g" * 40, "melhores": "g" * 40}
        )


def test_segredo_curto_em_um_cnpj_derruba_o_boot() -> None:
    with pytest.raises(ValueError, match=r"FR_WEBHOOK_SEGREDOS\[melhores\]"):
        settings_multi(
            fr_webhook_segredos={"grudado": "g" * 40, "melhores": "curto"}
        )


def test_cnpj_chega_ao_registro_do_evento() -> None:
    """Sem isto o relatorio nao consegue denunciar um cadastro que emudeceu."""
    import asyncio

    from app.services.eventos import ChaveEvento

    registrados: list[str | None] = []

    class EventosEspiao(EventosMemoria):
        async def registrar(
            self,
            chave: ChaveEvento,
            grupo: Grupo,
            status: StatusEvento,
            erro: str | None = None,
            cnpj: str | None = None,
        ) -> None:
            registrados.append(cnpj)
            await super().registrar(chave, grupo, status, erro, cnpj)

    svc = ServicoNotificacao(
        shopify=ShopifyFalsa(pedido()),  # type: ignore[arg-type]
        eventos=EventosEspiao(),
        n8n=N8nFalso(),  # type: ignore[arg-type]
        settings=settings_multi(),
    )
    # Codigo 3 = Entregue, fora dos gatilhos: cai no `registrar`.
    evento = WebhookOcorrenciaFR.model_validate(payload(codigo=3))
    asyncio.run(svc.processar(evento, cnpj="melhores"))

    assert registrados == ["melhores"]


def test_rota_devolve_o_cnpj_que_atendeu() -> None:
    """O `curl` de verificacao precisa confirmar QUAL dos tres cadastros respondeu.

    Colar a URL errada num dos tres e o erro mais facil de cometer no painel, e
    sem isto ele so apareceria dias depois, no relatorio.
    """
    import os

    from app.config import get_settings

    os.environ["FR_WEBHOOK_SEGREDOS"] = json.dumps(SEGREDOS_CNPJ)
    os.environ["FR_WEBHOOK_SEGREDO"] = ""
    os.environ["NOTIFICACAO_GRUPOS"] = "aguardando_retirada,tentativa_falha"
    os.environ["NOTIFICACAO_ATIVA"] = "false"
    get_settings.cache_clear()

    try:
        app = criar_app()
        app.state.servico_notificacao = ServicoNotificacao(
            shopify=ShopifyFalsa(pedido()),  # type: ignore[arg-type]
            eventos=EventosMemoria(),
            n8n=N8nFalso(),  # type: ignore[arg-type]
            settings=get_settings(),
            frete_rapido=FreteRapidoFalso(atual=232),  # type: ignore[arg-type]
        )
        with TestClient(app) as c:
            r = c.post(
                f"/api/v1/webhook/frete-rapido/{SEGREDOS_CNPJ['tudo']}",
                json=payload(),
            )
    finally:
        for chave in (
            "FR_WEBHOOK_SEGREDOS",
            "FR_WEBHOOK_SEGREDO",
            "NOTIFICACAO_GRUPOS",
            "NOTIFICACAO_ATIVA",
        ):
            os.environ.pop(chave, None)
        get_settings.cache_clear()

    assert r.status_code == 200
    assert r.json()["cnpj"] == "tudo"


def test_payload_invalido_e_rejeitado_sem_reenvio(cliente: TestClient) -> None:
    """422 nao esta na lista de reenvio da FR (408/429/5xx) -- e o certo:
    payload malformado nao melhora com repeticao."""
    r = cliente.post(ROTA, json={"numero_pedido": "59552"})  # sem `codigo`

    assert r.status_code == 422


def test_webhook_nao_e_barrado_pelo_limite_calibrado_para_humanos(
    cliente: TestClient,
) -> None:
    """A FR chega de poucos IPs e em rajada. Com o limite de 10/minuto do
    formulario, um lote de ocorrencias levaria 429 e o aviso urgente atrasaria
    horas."""
    from app.middleware.rate_limit import LimitadorJanelaDeslizante

    cliente.app.state.limitador = LimitadorJanelaDeslizante(limite=0, janela_s=60)  # type: ignore[attr-defined]

    codigos = [
        cliente.post(
            ROTA, json=payload(data_ocorrencia=f"2026-08-0{d} 09:00:00")
        ).status_code
        for d in range(1, 6)
    ]

    assert codigos == [200] * 5
    # O formulario do cliente continua limitado.
    assert cliente.post(
        "/api/v1/rastreio", json={"email": "a@b.com", "numero_pedido": "1"}
    ).status_code == 429


def test_segredo_nunca_aparece_no_log_de_acesso_do_uvicorn() -> None:
    """TESTE OBRIGATORIO -- e a regressao de um vazamento REAL.

    Uma primeira versao deste teste passava e mesmo assim o segredo vazava em
    producao: ela publicava o registro num handler do logger RAIZ, enquanto o
    `uvicorn.access` mantem handler proprio com `propagate=False`. Registro
    criado la nunca sobe ate o raiz, entao o filtro instalado no raiz nunca o
    via -- e o caminho da URL, com o segredo, ia inteiro para o log.

    Por isso este teste monta o logger exatamente como o uvicorn: handler
    proprio, `propagate=False` e o `AccessFormatter` de verdade.
    """
    from uvicorn.logging import AccessFormatter

    from app.services.logs import instalar_redacao

    capturado = StringIO()
    handler = logging.StreamHandler(capturado)
    handler.setFormatter(AccessFormatter(fmt="%(levelprefix)s %(message)s"))

    acesso = logging.getLogger("uvicorn.access")
    acesso.handlers = [handler]
    acesso.propagate = False
    acesso.setLevel(logging.INFO)

    instalar_redacao()
    try:
        # A forma exata do registro do uvicorn: o caminho e UM dos argumentos.
        acesso.info(
            '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1234", "POST", ROTA, "1.1", 200
        )
    finally:
        acesso.handlers = []

    saida = capturado.getvalue()
    assert SEGREDO not in saida
    assert "/webhook/frete-rapido/***" in saida


def test_redacao_preserva_a_estrutura_do_registro_de_acesso() -> None:
    """O `AccessFormatter` desempacota `record.args` em cinco variaveis.

    Zerar os argumentos ao redigir -- o caminho mais obvio -- faria o formatador
    estourar com "not enough values to unpack" exatamente no registro que carrega
    o segredo, trocando um vazamento por um erro de log.
    """
    from app.services.logs import RedatorDeSegredos

    registro = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "POST", ROTA, "1.1", 200),
        exc_info=None,
    )

    assert RedatorDeSegredos().filter(registro) is True

    assert isinstance(registro.args, tuple)
    assert len(registro.args) == 5
    assert registro.args[4] == 200  # o status continua int, nao virou texto
    assert SEGREDO not in str(registro.args[2])


def test_rota_nao_aparece_no_openapi(cliente: TestClient) -> None:
    """A URL carrega o segredo: nao entra em documentacao publica."""
    esquema = cliente.get("/openapi.json").json()
    assert not any("webhook" in caminho for caminho in esquema["paths"])
