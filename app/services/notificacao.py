"""Decide se uma ocorrencia vira aviso ao cliente, e manda o n8n entregar.

A ORDEM dos passos e normativa, como em `consulta.py`, e por motivos parecidos:

1. **Filtrar antes de consultar a Shopify.** A Frete Rapido manda webhook para
   TODA ocorrencia -- "Contratado", "Em transito", "Entregue". A maioria esmagadora
   nao vira mensagem, e gastar uma chamada a Shopify em cada uma seria desperdicio
   proporcional ao volume da operacao.
2. **Deduplicar antes de agir.** A FR reenvia o mesmo evento ate 12 vezes em ~24h
   enquanto nao receber HTTP 200. Sem a reserva no banco, cada reentrega viraria
   uma mensagem nova -- o pior modo de falha deste projeto.
3. **Confirmar o evento na propria Frete Rapido antes de tocar em dado do
   cliente.** O webhook NAO e assinado: o segredo da URL prova que quem chamou o
   conhece, nao que o evento aconteceu. So a Frete Rapido tem autoridade sobre
   isso, e perguntar a ela nao depende de IP fixo nem de nada que o fornecedor
   precise nos conceder.
4. **Resolver o pedido na Shopify.** O `numero_pedido` do payload tambem e dado
   de origem nao confiavel, e a Shopify e quem tem o contato do cliente.

O desfecho `pendente` e o unico que responde 503, e isso e proposital: a escada
de reentrega da propria Frete Rapido (1, 2, 3, 5, 10, 30 min, depois 1, 2, 3, 4,
5, 8 h) faz o papel de fila de reprocessamento, sem precisarmos manter uma.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.config import Settings, get_settings
from app.schemas import Grupo, OcorrenciaFR, StatusEvento, WebhookOcorrenciaFR
from app.services.datas import atribuir_fuso
from app.services.eventos import ChaveEvento, EventosMemoria, RepositorioEventos
from app.services.logs import redigir_excecao
from app.services.multi_cnpj import BuscadorMultiCNPJ
from app.services.n8n import ClienteN8n, N8nErro
from app.services.normalizacao import NumeroPedidoErro, NumeroPedidoFR, truncar
from app.services.ocorrencias import classificar
from app.services.ordenacao import ordenar_desc
from app.services.shopify import ClienteShopify, ShopifyErro
from app.services.transportadora import nome_amigavel

logger = logging.getLogger(__name__)

MAX_ERRO = 256


@dataclass(frozen=True)
class Confirmacao:
    """Resposta da Frete Rapido sobre um evento recebido.

    Carrega a OCORRENCIA, e nao um booleano, porque e dela que o aviso e
    montado. O payload do webhook serve para saber que algo mudou; o que se diz
    ao cliente sai daqui.
    """

    ocorrencia: OcorrenciaFR | None
    motivo: str | None = None
    # Repetir nao mudaria o resultado -- a encomenda ja seguiu adiante.
    definitivo: bool = False


@dataclass(frozen=True)
class Desfecho:
    """O que aconteceu com o evento, e o que responder a Frete Rapido."""

    status: StatusEvento
    grupo: Grupo
    detalhe: str | None = None

    @property
    def status_http(self) -> int:
        """503 apenas em `pendente`: e o pedido de reenvio a Frete Rapido.

        Todos os demais desfechos sao TERMINAIS e respondem 200. Devolver erro
        num evento que nunca podera ser entregue -- pedido sem telefone, por
        exemplo -- so gastaria as 12 tentativas dela sem mudar nada.
        """
        return 503 if self.status is StatusEvento.PENDENTE else 200


class ServicoNotificacao:
    def __init__(
        self,
        shopify: ClienteShopify,
        eventos: RepositorioEventos | None = None,
        n8n: ClienteN8n | None = None,
        settings: Settings | None = None,
        frete_rapido: BuscadorMultiCNPJ | None = None,
    ) -> None:
        self._shopify = shopify
        self._eventos: RepositorioEventos = eventos or EventosMemoria()
        self._n8n = n8n or ClienteN8n()
        self._s = settings or get_settings()
        # Usado para CONFIRMAR o evento na origem. Sem ele a verificacao e
        # pulada -- o que so acontece em teste, ou com o interruptor desligado.
        self._frete_rapido = frete_rapido

    async def _confirmar(
        self, numero: NumeroPedidoFR, codigo: int, cnpj: str | None
    ) -> Confirmacao:
        """A ocorrencia ATUAL deste pedido na Frete Rapido, se for a do evento.

        Uma versao anterior perguntava apenas "este codigo existe no historico?".
        Nao bastava, e revisao de seguranca externa mostrou o furo: o endpoint
        devolve o HISTORICO, entao um codigo de dias atras confirmava para
        sempre. Bastava reproduzir um evento antigo de "disponivel para retirada"
        num pedido ja entregue para mandar o cliente a agencia a toa.

        A pergunta certa e sobre o ESTADO ATUAL: `ordenar_desc()[0]` e o status
        corrente, o mesmo criterio que a pagina de rastreio usa. So avisamos se a
        encomenda esta AGORA no estado que o evento afirma.

        A distincao entre os dois "nao" tambem importa:

        - codigo AUSENTE do historico -> ou o evento e falso, ou a leitura ainda
          nao propagou. Nao da para saber, entao pedimos reenvio (`definitivo`
          falso) e deixamos a escada da Frete Rapido resolver.
        - codigo PRESENTE, mas nao e o atual -> a encomenda seguiu adiante.
          Repetir nunca vai mudar isso: encerramos (`definitivo`).
        """
        if self._frete_rapido is None:
            return Confirmacao(ocorrencia=None, motivo="verificacao indisponivel")

        # `buscar_no_cnpj` e nao `buscar`: aqui NAO pode haver fallback para os
        # outros tokens. Ver a justificativa no proprio metodo -- em resumo, o
        # fallback multiplicava por 3 o custo de um evento forjado (na mesma cota
        # que a pagina de rastreio usa) e furava o isolamento entre os CNPJs.
        try:
            resultado = await self._frete_rapido.buscar_no_cnpj(numero, cnpj)
        except Exception as exc:
            return Confirmacao(None, truncar(redigir_excecao(exc), MAX_ERRO))

        if not resultado.ocorrencias:
            if resultado.houve_falha:
                return Confirmacao(None, "falha ao consultar a Frete Rapido")
            return Confirmacao(None, "pedido sem ocorrencias na Frete Rapido")

        atual = ordenar_desc(resultado.ocorrencias)[0]
        if atual.codigo == codigo:
            return Confirmacao(atual)

        candidatas = [o for o in resultado.ocorrencias if o.codigo == codigo]
        if not candidatas:
            return Confirmacao(None, f"ocorrencia {codigo} nao existe na Frete Rapido")

        # "Existe mas nao e o atual" so vira DESFECHO TERMINAL com evidencia
        # temporal de verdade. `ordenar_desc` e uma heuristica de APRESENTACAO:
        # ocorrencia sem data vai para o fim da lista por convencao, nao porque
        # se saiba que e antiga.
        #
        # Sem esta guarda, uma ocorrencia nova com `data_ocorrencia` nula (o
        # schema aceita) seria empurrada para o fim, lida como historica,
        # encerrada com 200 -- e um aviso legitimo se perderia em silencio.
        mais_recente = max(
            (o.data_ocorrencia for o in candidatas if o.data_ocorrencia), default=None
        )
        if atual.data_ocorrencia is None or mais_recente is None:
            return Confirmacao(
                None,
                f"ocorrencia {codigo} nao e a atual, mas falta data para "
                "estabelecer precedencia",
            )

        return Confirmacao(
            None,
            f"ocorrencia {codigo} existe no historico mas o estado atual e "
            f"{atual.codigo}: a encomenda seguiu adiante",
            definitivo=True,
        )

    # ------------------------------------------------------------------
    # Decisao de gatilho
    # ------------------------------------------------------------------

    def deve_notificar(self, codigo: int, grupo: Grupo) -> bool:
        """Regra de gatilho, nesta ordem: ignorados, extra, grupo.

        Os ignorados vem primeiro para que desligar um codigo especifico seja
        sempre possivel, sem precisar desmontar o grupo inteiro.
        """
        if codigo in self._s.codigos_ignorados:
            return False
        if codigo in self._s.codigos_extra:
            return True
        return grupo in self._s.grupos_notificaveis

    # ------------------------------------------------------------------
    # Fluxo
    # ------------------------------------------------------------------

    async def processar(
        self, evento: WebhookOcorrenciaFR, cnpj: str | None = None
    ) -> Desfecho:
        """`cnpj` vem do segredo da URL, nao do payload.

        A Frete Rapido nao diz qual embarcador originou o evento -- o unico CNPJ
        no corpo e o da transportadora. Como ha um cadastro de webhook por CNPJ,
        o segredo do caminho carrega essa identidade.
        """
        grupo = classificar(evento.codigo)

        # O numero vem de fora e pode chegar em qualquer formato. Sem forma
        # canonica nao ha como casar com a Shopify nem deduplicar de forma
        # estavel -- duas grafias do mesmo pedido furariam a chave UNIQUE.
        try:
            numero = NumeroPedidoFR(evento.numero_pedido)
        except NumeroPedidoErro:
            logger.warning(
                "webhook com numero de pedido em formato inesperado (codigo %s)",
                evento.codigo,
            )
            return Desfecho(StatusEvento.DESCARTADO, grupo, "numero nao normalizavel")

        chave = ChaveEvento(
            numero_pedido=str(numero),
            codigo=evento.codigo,
            data_ocorrencia=atribuir_fuso(evento.data_ocorrencia),
        )

        # 1. Gatilho. O caso comum -- e o mais barato.
        if not self.deve_notificar(evento.codigo, grupo):
            await self._eventos.registrar(
                chave, grupo, StatusEvento.DESCARTADO, cnpj=cnpj
            )
            return Desfecho(StatusEvento.DESCARTADO, grupo)

        # 2. Assumir o evento: lease + cota, numa transacao so.
        #
        # O lease existe porque `pendente` significava duas coisas ao mesmo
        # tempo -- "alguem esta processando" e "pode tentar de novo" -- e duas
        # entregas simultaneas passavam as duas, gerando mensagem duplicada. A
        # restricao UNIQUE arbitra quem cria a LINHA; o lease, quem executa o
        # EFEITO.
        #
        # A cota entra aqui, e nao depois, porque conta-la fora da transacao
        # deixava N corridas simultaneas verem zero e todas enviarem.
        agora = datetime.now(UTC)
        desde = agora - timedelta(hours=self._s.notificacao_janela_horas)
        # Identidade deste processamento. E o que permite `concluir` recusar a
        # escrita de um worker cujo lease ja venceu.
        dono = uuid4().hex
        reserva = await self._eventos.adquirir(
            chave,
            grupo,
            cnpj=cnpj,
            dono=dono,
            lease_s=self._s.notificacao_lease_s,
            cooldown_s=self._s.notificacao_cooldown_s,
            desde=desde,
            max_tentativas=self._s.notificacao_max_tentativas_pedido,
        )

        if reserva.status is not None:
            logger.info(
                "evento repetido do pedido %s (codigo %s): ja estava %s",
                numero,
                evento.codigo,
                reserva.status.value,
            )
            return Desfecho(reserva.status, grupo, "reentrega")

        if reserva.em_andamento or reserva.em_espera:
            # 503, e nao 200. HTTP 200 e TERMINAL para a Frete Rapido: ela para
            # de tentar. Se o processo que detem o lease morrer depois disso, o
            # lease expira e ninguem reassume -- nao ha fila local -- e o aviso
            # some. Com 503 ela continua tentando: enquanto o dono estiver vivo
            # as proximas veem o lease, e se ele morrer uma entrega posterior
            # recupera o evento.
            motivo = "em processamento" if reserva.em_andamento else "em espera"
            logger.info(
                "evento do pedido %s (codigo %s) %s",
                numero,
                evento.codigo,
                motivo,
            )
            return Desfecho(StatusEvento.PENDENTE, grupo, motivo)

        if reserva.custo_excedido:
            # PENDENTE, e nao descarte. Este teto existe para conter CUSTO, e o
            # evento que esbarra nele provavelmente e legitimo -- um pedido com
            # muita movimentacao, ou a vitima de alguem enchendo a cota dele de
            # proposito. Encerrar com 200 permitiria silenciar os avisos de um
            # pedido escolhido por 6 horas.
            #
            # Com 503 a Frete Rapido reapresenta mais tarde, quando a janela ja
            # deslizou. E a checagem acontece ANTES da confirmacao, entao repetir
            # nao custa chamada a eles.
            motivo = (
                f"teto de tentativas do pedido: {reserva.tentativas_recentes} em "
                f"{self._s.notificacao_janela_horas}h"
            )
            logger.warning("evento adiado no pedido %s -- %s", numero, motivo)
            return Desfecho(StatusEvento.PENDENTE, grupo, motivo)

        # 3. Confirmar na FONTE, antes de tocar em dado do cliente.
        #
        # A ordem importa em dois sentidos. Primeiro, seguranca: o segredo da URL
        # prova que quem chamou o conhece, nao que o evento aconteceu -- so a
        # Frete Rapido tem autoridade sobre isso. Segundo, privacidade: nao
        # buscamos o telefone de ninguem com base num evento que ainda nao
        # sabemos se e real.
        #
        # A ocorrencia devolvida aqui e a UNICA fonte do que sera dito ao
        # cliente. O corpo do webhook apenas avisa que algo mudou.
        confirmada: OcorrenciaFR | None = None
        if self._s.notificacao_verificar_na_fonte:
            conf = await self._confirmar(numero, evento.codigo, cnpj)
            if conf.ocorrencia is None:
                logger.warning(
                    "evento do pedido %s (codigo %s) nao confirmado na fonte: %s",
                    numero,
                    evento.codigo,
                    conf.motivo,
                )
                # `definitivo` = a encomenda seguiu adiante; repetir nao muda
                # nada, entao encerra com 200 em vez de pedir reenvio.
                final = (
                    StatusEvento.DESCARTADO
                    if conf.definitivo
                    else StatusEvento.PENDENTE
                )
                await self._eventos.concluir(chave, final, conf.motivo, dono=dono)
                return Desfecho(final, grupo, conf.motivo)
            confirmada = conf.ocorrencia

        # 4. Cota de MENSAGEM, so agora. Quando isto ficava junto da aquisicao do
        # lease, tres eventos forjados simultaneos ocupavam as tres vagas antes
        # de qualquer verificacao, e o evento legitimo que chegasse junto era
        # descartado sem nunca ser consultado. So evento confirmado consome cota.
        vaga = await self._eventos.reservar_aviso(
            chave,
            desde=desde,
            desde_volume=agora
            - timedelta(minutes=self._s.notificacao_janela_volume_min),
            desde_hora=agora - timedelta(hours=1),
            max_avisos=self._s.notificacao_max_por_pedido,
            cnpj=cnpj,
            max_global=self._s.notificacao_max_global_hora,
            max_cnpj=self._s.notificacao_max_cnpj_hora,
        )
        if vaga.limite_sistemico is not None:
            # Disjuntor. ADIA, nao descarta: as demais travas sao por pedido, e
            # esta pega o padrao que elas nao veem -- avisos espalhados por
            # muitos pedidos. Uma rajada legitima acima do teto sai mais tarde,
            # em vez de sumir.
            #
            # WARNING de proposito: se isto disparar em operacao normal, o teto
            # esta baixo demais e precisa ser recalibrado com o relatorio 10.
            logger.warning(
                "DISJUNTOR de avisos acionado (%s); pedido %s adiado",
                vaga.limite_sistemico,
                numero,
            )
            return Desfecho(StatusEvento.PENDENTE, grupo, vaga.limite_sistemico)

        if not vaga.concedida:
            if vaga.codigo_repetido:
                # Nao e excesso, e repeticao do mesmo fato -- tipicamente uma
                # ocorrencia por VOLUME da remessa. A janela aqui e curta de
                # proposito: duas tentativas de entrega reais no mesmo dia sao
                # dois fatos, e o cliente precisa saber dos dois.
                motivo = (
                    f"ja avisado sobre o codigo {evento.codigo} deste pedido nos "
                    f"ultimos {self._s.notificacao_janela_volume_min} min"
                )
            else:
                motivo = (
                    f"limite anti-spam: {vaga.avisos_recentes} aviso(s) em "
                    f"{self._s.notificacao_janela_horas}h"
                )
            logger.info("aviso contido para o pedido %s -- %s", numero, motivo)
            await self._eventos.concluir(
                chave, StatusEvento.DESCARTADO, motivo, dono=dono
            )
            return Desfecho(StatusEvento.DESCARTADO, grupo, motivo)

        # 5. Shopify: e aqui que o payload nao confiavel encontra a realidade.
        try:
            pedido = await self._shopify.buscar_pedido(numero)
        except ShopifyErro as exc:
            # Shopify fora do ar nao pode PERDER o aviso: fica pendente e a FR
            # reenvia. E o caso em que o 503 vale a pena.
            falha = truncar(redigir_excecao(exc), MAX_ERRO)
            logger.error("falha na Shopify ao processar webhook: %s", falha)
            await self._eventos.concluir(
                chave, StatusEvento.PENDENTE, falha, dono=dono
            )
            return Desfecho(StatusEvento.PENDENTE, grupo, falha)

        if pedido is None:
            # Pedido que nao existe na loja: webhook forjado, numero de outra
            # operacao, ou pedido antigo demais para a API da Shopify.
            logger.warning("webhook para pedido inexistente na Shopify: %s", numero)
            await self._eventos.concluir(
                chave,
                StatusEvento.DESCARTADO,
                "pedido inexistente na Shopify",
                dono=dono,
            )
            return Desfecho(StatusEvento.DESCARTADO, grupo, "pedido inexistente")

        if not pedido.telefone:
            # Terminal, e nao falha: sem numero utilizavel nao ha o que reenviar.
            # A taxa disto e o que decide se vale buscar o telefone na propria
            # Frete Rapido (`quote/{id_frete}`) como segunda fonte.
            await self._eventos.concluir(
                chave, StatusEvento.SEM_CONTATO, dono=dono
            )
            return Desfecho(StatusEvento.SEM_CONTATO, grupo)

        # 6. Interruptor geral. Ate aqui tudo rodou de verdade -- inclusive a
        # confirmacao na fonte e a consulta a Shopify -- e e isso que da a
        # medicao real da Fase 1.
        if not self._s.notificacao_ativa:
            await self._eventos.concluir(chave, StatusEvento.OBSERVADO, dono=dono)
            logger.info(
                "aviso OBSERVADO (envio desligado): pedido %s, codigo %s, grupo %s",
                numero,
                evento.codigo,
                grupo.value,
            )
            return Desfecho(StatusEvento.OBSERVADO, grupo)

        # 7. Entrega. `confirmada` so e nula com a verificacao desligada -- o
        # interruptor de emergencia. Nesse modo o webhook volta a ser a fonte,
        # com a ressalva que isso carrega.
        #
        # Renovar o lease IMEDIATAMENTE antes do efeito externo estreita para
        # perto de zero a janela entre "sou dono" e "enviei". Sem isto, um lease
        # vencido durante a consulta a Shopify deixaria dois processos enviarem.
        if not await self._eventos.renovar(
            chave, dono=dono, lease_s=self._s.notificacao_lease_s
        ):
            logger.warning(
                "lease do pedido %s (codigo %s) perdido antes do envio; outro "
                "processo assumiu",
                numero,
                evento.codigo,
            )
            return Desfecho(StatusEvento.PENDENTE, grupo, "lease perdido")

        ocorrencia = confirmada or _da_webhook(evento)
        payload = montar_payload(
            numero, ocorrencia, grupo, pedido.telefone, pedido.nome_cliente
        )
        try:
            await self._n8n.enviar(payload)
        except N8nErro as exc:
            falha = truncar(redigir_excecao(exc), MAX_ERRO)
            logger.error("falha ao entregar aviso ao n8n: %s", falha)
            await self._eventos.concluir(
                chave, StatusEvento.PENDENTE, falha, dono=dono
            )
            return Desfecho(StatusEvento.PENDENTE, grupo, falha)

        await self._eventos.concluir(chave, StatusEvento.ENVIADO, dono=dono)
        logger.info(
            "aviso enviado: pedido %s, codigo %s, grupo %s",
            numero,
            evento.codigo,
            grupo.value,
        )
        return Desfecho(StatusEvento.ENVIADO, grupo)


def chave_idempotencia(numero: NumeroPedidoFR, ocorrencia: OcorrenciaFR) -> str:
    """Identidade estavel de um aviso, para o n8n recusar repeticoes.

    Derivada da ocorrencia CONFIRMADA, nunca do corpo do webhook: se viesse de
    la, bastaria variar um campo para gerar chave nova e furar a protecao --
    exatamente o que a chave existe para impedir.

    A data cai para `data_atualizacao` e depois para o volume quando
    `data_ocorrencia` e nula. Sem isso, duas ocorrencias legitimas do mesmo
    pedido e codigo sem data gerariam a MESMA chave para sempre, e a segunda
    tentativa de entrega -- dias depois -- seria descartada pelo n8n como
    repetida.

    Se nem isso existir, a chave e grosseira por construcao. O consumidor
    precisa expirar as chaves (TTL alinhado a janela de negocio); guarda-las
    para sempre transforma qualquer aviso legitimo futuro em duplicata.
    """
    data = atribuir_fuso(ocorrencia.data_ocorrencia or ocorrencia.data_atualizacao)
    partes = [
        str(numero),
        str(ocorrencia.codigo),
        data.isoformat() if data else "",
        ocorrencia.codigo_volume or "",
    ]
    return hashlib.sha256("|".join(partes).encode()).hexdigest()[:32]


def _da_webhook(evento: WebhookOcorrenciaFR) -> OcorrenciaFR:
    """Converte o payload do webhook numa ocorrencia, SEM confirmacao.

    So e usado com `NOTIFICACAO_VERIFICAR_NA_FONTE=false`, o interruptor de
    emergencia. Nesse modo o conteudo da mensagem volta a vir de quem chamou a
    rota -- e por isso o interruptor nao deveria conviver com o envio ligado
    (ver a validacao de boot em `config.py`).
    """
    return OcorrenciaFR(
        codigo=evento.codigo,
        nome=evento.nome,
        mensagem=evento.mensagem,
        data_ocorrencia=evento.data_ocorrencia,
        razao_social_transportadora=evento.nome_transportadora,
    )


def montar_payload(
    numero: NumeroPedidoFR,
    ocorrencia: OcorrenciaFR,
    grupo: Grupo,
    telefone: str,
    nome_cliente: str | None,
) -> dict[str, Any]:
    """Contrato com o n8n, montado SEM nenhum campo do corpo do webhook.

    Esta assinatura e a correcao de uma falha real, apontada em revisao de
    seguranca: a versao anterior recebia o `WebhookOcorrenciaFR` e copiava
    `nome`, `mensagem`, `transportadora` e os prazos direto dele. Confirmavamos
    o GATILHO e deixavamos passar o CONTEUDO -- quem tivesse o segredo escrevia
    o texto que chegava no WhatsApp do cliente.

    Agora tudo vem de uma de tres origens confiaveis: a ocorrencia devolvida
    pela API da Frete Rapido, a nossa propria classificacao, ou a Shopify
    (telefone e nome). O webhook so avisa que algo mudou.

    `prazo_devolucao` saiu do contrato por isso: so existia no corpo do webhook,
    e nao ha como confirma-lo. O texto do n8n perde o "ate dia X" e ganha
    "quanto antes" -- menos especifico, e verdadeiro. Se a data virar importante,
    precisa de uma fonte verificavel, nao do payload.

    Segue MINIMO em dado pessoal: telefone e primeiro nome, nada mais. O n8n
    retem os dados de execucao no banco dele, fora do alcance de
    `scripts/expurgar.py`.
    """
    data = atribuir_fuso(ocorrencia.data_ocorrencia)
    return {
        "evento": "acao_necessaria",
        # Chave ESTAVEL da ocorrencia confirmada. Existe porque a entrega ao n8n
        # e "ao menos uma vez", nao "exatamente uma": se ele aceitar o POST e
        # disparar o WhatsApp mas a resposta HTTP se perder, nos entendemos como
        # falha e tentamos de novo. Do lado de ca nao ha como distinguir "nao
        # recebeu" de "recebeu e a confirmacao sumiu".
        #
        # O fluxo do n8n DEVE registrar esta chave e recusar repetidas ANTES de
        # chamar o WhatsApp. Sem isso, o teto anti-spam continua valendo, mas a
        # duplicata deixa de ser impossivel e passa a ser apenas improvavel.
        "idempotencia": chave_idempotencia(numero, ocorrencia),
        "grupo": grupo.value,
        # Numero JA normalizado, nao a string crua do payload.
        "pedido": str(numero),
        "codigo": ocorrencia.codigo,
        # O `nome` da propria Frete Rapido, NAO traduzido: mesma regra de
        # `ocorrencias.py`. O catalogo muda sozinho e sempre em portugues
        # legivel; uma traducao nossa envelheceria.
        "rotulo": ocorrencia.nome,
        "descricao": ocorrencia.descricao,
        "transportadora": nome_amigavel(ocorrencia.razao_social_transportadora),
        "previsao_entrega": (
            ocorrencia.data_prevista_entrega.isoformat()
            if ocorrencia.data_prevista_entrega
            else None
        ),
        "telefone": telefone,
        "primeiro_nome": nome_cliente,
        "data_ocorrencia": data.isoformat() if data else None,
    }
