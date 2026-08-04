"""Selecao do token da Frete Rapido entre os 3 CNPJs da operacao.

Cada CNPJ tem seu proprio token, e um token so enxerga os fretes do seu CNPJ.
Consultar com o token errado devolve vazio -- indistinguivel de "nao despachado".

O pedido da Shopify carrega uma TAG que identifica a empresa. A estrategia tem
dois caminhos:

- CAMINHO RAPIDO: a tag casa com um CNPJ configurado -> uma unica chamada.
- CAMINHO SEGURO: tag ausente, desconhecida, OU o token indicado voltou vazio
  -> consulta os demais em paralelo antes de concluir.

Por que gastar chamadas extras quando a tag aponta um CNPJ mas ele volta vazio:
o vazio e exatamente o caso ambiguo. Se o pedido foi marcado com a tag errada,
confiar cegamente nela responderia "nao despachado" para um pedido que existe.
O custo so aparece nesse caso raro.

FALHA FECHADA: se algum token falhar e nenhum devolver dados, o resultado marca
`houve_falha`. O chamador responde ERRO, nunca "sem rastreio" -- o token que
falhou pode ser justamente o que tinha os dados.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.schemas import Anomalia, OcorrenciaFR
from app.services.frete_rapido import ClienteFreteRapido, FreteRapidoErro
from app.services.normalizacao import NumeroPedidoFR

logger = logging.getLogger(__name__)


@dataclass
class ResultadoBusca:
    ocorrencias: list[OcorrenciaFR] = field(default_factory=list)
    # Chave do CNPJ que efetivamente atendeu, para log e para o cache.
    cnpj: str | None = None
    # Algum token falhou. Com lista vazia, isto obriga o chamador a responder
    # erro em vez de "sem rastreio".
    houve_falha: bool = False
    anomalias: list[Anomalia] = field(default_factory=list)


class BuscadorMultiCNPJ:
    def __init__(
        self, cliente: ClienteFreteRapido, tokens: dict[str, str]
    ) -> None:
        if not tokens:
            raise ValueError("nenhum token da Frete Rapido configurado")
        self._cliente = cliente
        self._tokens = tokens

    def _cnpj_da_tag(self, tags: list[str]) -> str | None:
        """Primeira tag do pedido que corresponde a um CNPJ configurado."""
        for tag in tags:
            if tag in self._tokens:
                return tag
        return None

    async def buscar_no_cnpj(
        self, numero: NumeroPedidoFR, cnpj: str | None
    ) -> ResultadoBusca:
        """Consulta APENAS o token do CNPJ indicado. Sem fallback, de proposito.

        Existe para a confirmacao do webhook, onde o CNPJ vem do segredo da URL
        e nao de uma tag digitada. Reusar o `buscar` aqui era nocivo em dois
        sentidos:

        1. **Custo.** Pedido inexistente devolve vazio no primeiro token e cai no
           fallback, consultando os outros dois. Um evento forjado custava 3
           chamadas -- e a cota da Frete Rapido (720/min) e COMPARTILHADA com a
           pagina de rastreio. Bastava repetir eventos forjados para saturar a
           cota e derrubar o fluxo principal.
        2. **Isolamento.** O fallback anulava o motivo de existir um segredo por
           cadastro: o segredo vazado do CNPJ A confirmaria pedidos de B e C.

        No fluxo de CONSULTA o fallback continua certo -- la a tag pode estar
        errada, e o custo de errar e responder "nao despachado" para um pedido
        que existe. Aqui a identidade e criptografica, nao um rotulo digitado.
        """
        if cnpj:
            token = self._tokens.get(cnpj)
            if token is None:
                # Chave de FR_WEBHOOK_SEGREDOS que nao existe em
                # FRETE_RAPIDO_TOKENS. Falhar alto: confirmar por outro token
                # seria exatamente o furo de isolamento que este metodo evita.
                raise FreteRapidoErro(
                    f"CNPJ {cnpj!r} nao tem token configurado "
                    "(FR_WEBHOOK_SEGREDOS e FRETE_RAPIDO_TOKENS divergem)"
                )
            return await self._buscar_em_um(numero, cnpj, token, [])

        # Sem CNPJ identificado (segredo avulso, sem separacao por cadastro).
        # So da para confirmar com seguranca quando nao ha o que escolher.
        if len(self._tokens) == 1:
            ((unico, token),) = self._tokens.items()
            return await self._buscar_em_um(numero, unico, token, [])

        raise FreteRapidoErro(
            "evento sem CNPJ identificado e ha mais de um token configurado: "
            "use FR_WEBHOOK_SEGREDOS, um segredo por CNPJ"
        )

    async def buscar(
        self, numero: NumeroPedidoFR, tags: list[str] | None = None
    ) -> ResultadoBusca:
        tags = tags or []
        anomalias: list[Anomalia] = []

        # Operacao de CNPJ unico: nao ha o que selecionar.
        if len(self._tokens) == 1:
            (cnpj, token), = self._tokens.items()
            return await self._buscar_em_um(numero, cnpj, token, anomalias)

        cnpj_da_tag = self._cnpj_da_tag(tags)

        if cnpj_da_tag is None:
            anomalias.append(
                Anomalia.TAG_CNPJ_DESCONHECIDA if tags else Anomalia.TAG_CNPJ_AUSENTE
            )
            logger.warning(
                "pedido %s sem tag de CNPJ reconhecida (tags=%s); consultando todos",
                numero,
                tags,
            )
            return await self._buscar_em_todos(numero, anomalias)

        # Caminho rapido.
        resultado = await self._buscar_em_um(
            numero, cnpj_da_tag, self._tokens[cnpj_da_tag], anomalias
        )
        if resultado.ocorrencias:
            return resultado

        # Vazio (ou falha) sob o CNPJ da tag: pode ser marcacao errada.
        logger.info(
            "pedido %s vazio no CNPJ da tag (%s); verificando os demais",
            numero,
            cnpj_da_tag,
        )
        restantes = {k: v for k, v in self._tokens.items() if k != cnpj_da_tag}
        alternativo = await self._buscar_em_todos(numero, anomalias, restantes)

        if alternativo.ocorrencias:
            alternativo.anomalias.append(Anomalia.CNPJ_DIVERGENTE_DA_TAG)
            logger.warning(
                "pedido %s marcado como %s mas os dados estao em %s",
                numero,
                cnpj_da_tag,
                alternativo.cnpj,
            )
            return alternativo

        # Nada em lugar nenhum: preserva a falha do caminho rapido, se houve.
        alternativo.houve_falha = alternativo.houve_falha or resultado.houve_falha
        return alternativo

    async def _buscar_em_um(
        self,
        numero: NumeroPedidoFR,
        cnpj: str,
        token: str,
        anomalias: list[Anomalia],
    ) -> ResultadoBusca:
        try:
            ocorrencias = await self._cliente.buscar_ocorrencias(numero, token)
        except FreteRapidoErro as exc:
            logger.error("falha no CNPJ %s para o pedido %s: %s", cnpj, numero, exc)
            return ResultadoBusca(
                houve_falha=True, cnpj=cnpj, anomalias=list(anomalias)
            )
        return ResultadoBusca(
            ocorrencias=ocorrencias,
            cnpj=cnpj if ocorrencias else None,
            anomalias=list(anomalias),
        )

    async def _buscar_em_todos(
        self,
        numero: NumeroPedidoFR,
        anomalias: list[Anomalia],
        tokens: dict[str, str] | None = None,
    ) -> ResultadoBusca:
        alvos = tokens if tokens is not None else self._tokens
        if not alvos:
            return ResultadoBusca(anomalias=list(anomalias))

        # Em paralelo: a latencia e a do mais lento, nao a soma.
        respostas = await asyncio.gather(
            *(
                self._cliente.buscar_ocorrencias(numero, token)
                for token in alvos.values()
            ),
            return_exceptions=True,
        )

        houve_falha = False
        com_dados: list[tuple[str, list[OcorrenciaFR]]] = []

        for cnpj, resposta in zip(alvos.keys(), respostas, strict=True):
            if isinstance(resposta, BaseException):
                houve_falha = True
                logger.error(
                    "falha no CNPJ %s para o pedido %s: %s", cnpj, numero, resposta
                )
                continue
            if resposta:
                com_dados.append((cnpj, resposta))

        resultado = ResultadoBusca(houve_falha=houve_falha, anomalias=list(anomalias))

        if not com_dados:
            return resultado

        if len(com_dados) > 1:
            # Nao deveria acontecer: o numero do pedido e unico na loja.
            resultado.anomalias.append(Anomalia.MULTIPLOS_CNPJS_COM_DADOS)
            logger.warning(
                "pedido %s tem ocorrencias em mais de um CNPJ: %s",
                numero,
                [c for c, _ in com_dados],
            )

        # Ordem deterministica: a configuracao, nao a ordem de chegada das
        # respostas paralelas.
        com_dados.sort(key=lambda item: list(alvos.keys()).index(item[0]))
        resultado.cnpj, resultado.ocorrencias = com_dados[0]
        return resultado
