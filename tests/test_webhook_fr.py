"""Webhook da Frete Rapido: segredo, lista de permissao, gatilhos e dedup.

O teste que mais importa aqui e o de REENTREGA. A Frete Rapido reenvia o mesmo
evento ate 12 vezes em ~24h enquanto nao receber HTTP 200, e o pior modo de
falha do projeto e o cliente receber 12 mensagens identicas.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import criar_app
from app.schemas import Grupo, StatusEvento, WebhookOcorrenciaFR
from app.services.eventos import EventosMemoria
from app.services.normalizacao import NumeroPedidoFR
from app.services.notificacao import ServicoNotificacao, montar_payload
from app.services.shopify import PedidoShopify, ShopifyErro

FIXTURES = Path(__file__).parent / "fixtures"
SEGREDO = "s" * 40
ROTA = f"/api/v1/webhook/frete-rapido/{SEGREDO}"
TELEFONE = "+5511988887777"


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
    base: dict[str, Any] = {
        "fr_webhook_segredo": SEGREDO,
        "notificacao_grupos": "aguardando_retirada,tentativa_falha",
        "notificacao_ativa": True,
        "n8n_webhook_url": "https://n8n.exemplo/webhook/fr",
    }
    base.update(kw)
    return Settings(**base)


def servico(
    *,
    shopify: ShopifyFalsa | None = None,
    n8n: N8nFalso | None = None,
    s: Settings | None = None,
) -> tuple[ServicoNotificacao, ShopifyFalsa, N8nFalso]:
    sh = shopify or ShopifyFalsa(pedido())
    fila = n8n or N8nFalso()
    servico = ServicoNotificacao(
        shopify=sh,  # type: ignore[arg-type]
        eventos=EventosMemoria(),
        n8n=fila,  # type: ignore[arg-type]
        settings=s or settings(),
    )
    return servico, sh, fila


async def processar(bruto: dict[str, Any], **kw: Any) -> Any:
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
    svc, _, fila = servico()

    await svc.processar(WebhookOcorrenciaFR.model_validate(payload()))
    await svc.processar(
        WebhookOcorrenciaFR.model_validate(
            payload(codigo=32, data_ocorrencia="2026-08-04 09:00:00")
        )
    )

    assert len(fila.enviados) == 2


async def test_trava_anti_spam_contem_rajada_no_mesmo_pedido() -> None:
    """Uma transportadora que posta cinco codigos seguidos nao vira cinco avisos."""
    svc, _, fila = servico(s=settings(notificacao_max_por_pedido=2))

    for dia in range(1, 6):
        await svc.processar(
            WebhookOcorrenciaFR.model_validate(
                payload(codigo=32, data_ocorrencia=f"2026-08-0{dia} 09:00:00")
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
    )
    evento = WebhookOcorrenciaFR.model_validate(payload())

    primeira = await svc.processar(evento)
    assert primeira.status is StatusEvento.PENDENTE
    assert primeira.status_http == 503

    # O n8n volta; a reentrega da FR encontra a linha em `pendente` e conclui.
    svc._n8n = N8nFalso()  # type: ignore[assignment]
    segunda = await svc.processar(evento)

    assert segunda.status is StatusEvento.ENVIADO
    assert segunda.status_http == 200


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


def test_payload_do_n8n_leva_o_minimo_de_dado_pessoal() -> None:
    evento = WebhookOcorrenciaFR.model_validate(payload())
    saida = montar_payload(evento, Grupo.AGUARDANDO_RETIRADA, TELEFONE, "Daniel")

    assert saida["telefone"] == TELEFONE
    assert saida["primeiro_nome"] == "Daniel"
    assert saida["grupo"] == "aguardando_retirada"
    assert saida["pedido"] == "59552"
    assert saida["prazo_devolucao"] == "2026-08-12"
    # O rotulo e o `nome` da propria Frete Rapido; nao traduzimos.
    assert saida["rotulo"] == "Disponivel para retirada nos Correios"
    assert saida["transportadora"] == "Correios"

    # Nada alem de telefone e primeiro nome sai daqui: o n8n retem os dados de
    # execucao no banco dele, fora do alcance do expurgo LGPD.
    texto = json.dumps(saida, default=str)
    assert "123.456.789-00" not in texto
    assert "cliente@exemplo.com" not in texto
    assert "35260712345678000199550010001234561234567890" not in texto


def test_data_de_ocorrencia_sai_com_fuso() -> None:
    """Data naive sem fuso declarado exibiria o horario errado perto da meia-noite."""
    evento = WebhookOcorrenciaFR.model_validate(payload())
    saida = montar_payload(evento, Grupo.AGUARDANDO_RETIRADA, TELEFONE, None)

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

BEARER = "b" * 48


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
