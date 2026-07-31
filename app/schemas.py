"""Contrato da API e modelos de entrada das integracoes.

O modelo `OcorrenciaFR` funciona como LISTA DE PERMISSAO: o payload da Frete
Rapido traz `nome_entregador`, `cnpj_cpf_entregador` e `comprovantes` -- dados
pessoais de terceiros, sem utilidade para a resposta que entregamos. Como o
modelo declara apenas os campos permitidos e ignora o resto, esses dados sao
descartados antes de qualquer persistencia. Campos novos que a API venha a
adicionar tambem ficam de fora por padrao, em vez de entrarem por descuido.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Teto para textos vindos de terceiros (transportadoras). Aplicado ja no
# parsing, antes de chegar ao cache, aos logs ou a pagina.
MAX_TEXTO_EXTERNO = 1000


class Grupo(StrEnum):
    """Agrupamento visual de um codigo de ocorrencia.

    O grupo NAO e decorativo: dirige icone, destaque, posicao na linha do tempo e
    a indicacao de que o cliente precisa agir.
    """

    PREPARANDO = "preparando"
    COLETADO = "coletado"
    EM_TRANSITO = "em_transito"
    SAIU_PARA_ENTREGA = "saiu_para_entrega"
    ENTREGUE = "entregue"
    AGUARDANDO_RETIRADA = "aguardando_retirada"
    ENTREGA_PARCIAL = "entrega_parcial"
    TENTATIVA_FALHA = "tentativa_falha"
    PROBLEMA = "problema"
    DEVOLUCAO = "devolucao"
    SINISTRO = "sinistro"
    CANCELADO = "cancelado"
    EM_ANDAMENTO = "em_andamento"  # fallback de codigo nao mapeado


class Resultado(StrEnum):
    SUCESSO = "sucesso"
    SEM_RASTREIO = "sem_rastreio"
    VAZIO_FR = "vazio_fr"
    NAO_ENCONTRADO = "nao_encontrado"
    ERRO_EXTERNO = "erro_externo"


class Anomalia(StrEnum):
    """Situacoes que nao quebram a resposta mas precisam ficar visiveis."""

    MULTIPLOS_FULFILLMENTS = "multiplos_fulfillments"
    MULTIPLOS_TRACKINGS = "multiplos_trackings"
    PAGINA_CHEIA_SEM_CORRESPONDENCIA = "pagina_cheia_sem_correspondencia"

    # --- Multi-CNPJ ---
    # O pedido nao tem tag de empresa: caimos na busca em todos os tokens.
    TAG_CNPJ_AUSENTE = "tag_cnpj_ausente"
    # Ha tag, mas ela nao consta na configuracao (renomeada? CNPJ novo?).
    TAG_CNPJ_DESCONHECIDA = "tag_cnpj_desconhecida"
    # A tag apontava um CNPJ, mas os dados estavam em outro: pedido mal marcado.
    CNPJ_DIVERGENTE_DA_TAG = "cnpj_divergente_da_tag"
    # Mais de um CNPJ devolveu ocorrencias para o mesmo numero de pedido.
    MULTIPLOS_CNPJS_COM_DADOS = "multiplos_cnpjs_com_dados"


# --------------------------------------------------------------------------
# Entrada
# --------------------------------------------------------------------------


class ConsultaRequest(BaseModel):
    email: EmailStr
    numero_pedido: str = Field(min_length=1, max_length=64)


class OcorrenciaFR(BaseModel):
    """Uma ocorrencia da Frete Rapido, ja filtrada pela lista de permissao."""

    model_config = ConfigDict(extra="ignore")

    codigo: int
    nome: str | None = None
    data_ocorrencia: datetime | None = None
    data_atualizacao: datetime | None = None
    data_prevista_entrega: date | None = None
    descricao_ocorrencia: str | None = None
    mensagem: str | None = None
    razao_social_transportadora: str | None = None
    codigo_volume: str | None = None

    # Preenchido na leitura, nao vem da API: e o desempate da ordenacao quando
    # duas ocorrencias tem o mesmo instante.
    indice_origem: int = 0

    @field_validator(
        "nome",
        "descricao_ocorrencia",
        "mensagem",
        "razao_social_transportadora",
        "codigo_volume",
        mode="before",
    )
    @classmethod
    def _limitar_texto_externo(cls, v: Any) -> Any:
        """Corta textos vindos de terceiros.

        `nome`, `mensagem` e afins sao preenchidos por transportadoras. Sem
        teto, um valor gigante -- por erro ou por ma-fe do fornecedor -- incharia
        o cache, os logs e o DOM da pagina do cliente.
        """
        if isinstance(v, str) and len(v) > MAX_TEXTO_EXTERNO:
            return v[:MAX_TEXTO_EXTERNO]
        return v

    @field_validator("codigo", mode="before")
    @classmethod
    def _codigo_int_ou_string(cls, v: Any) -> Any:
        """As fixtures reais trazem inteiro; a documentacao mostra string.

        Aceitar so a forma observada deixaria a aplicacao quebrar no dia em que a
        API devolvesse a forma documentada, que e igualmente legitima.
        """
        if isinstance(v, str):
            limpo = v.strip()
            if not limpo.isdigit():
                raise ValueError(f"codigo de ocorrencia nao numerico: {v!r}")
            return int(limpo)
        return v

    @property
    def descricao(self) -> str | None:
        """`descricao_ocorrencia` com fallback para `mensagem`."""
        return self.descricao_ocorrencia or self.mensagem


# --------------------------------------------------------------------------
# Saida
# --------------------------------------------------------------------------


class EventoResposta(BaseModel):
    codigo: int
    rotulo: str | None  # o `nome` da propria Frete Rapido, nao traduzimos
    grupo: Grupo
    data: datetime | None
    descricao: str | None = None


class RespostaSucesso(BaseModel):
    """Resposta de sucesso.

    NAO expomos o codigo gravado no fulfillment da Shopify. Verificado em
    30/07/2026: o ERP grava ali o `id_frete` da Frete Rapido (`FR260723D6KTG`),
    que NAO e rastreavel no site da transportadora. Exibi-lo como "codigo de
    rastreio" faria o cliente tentar usa-lo na Jadlog, falhar, e procurar o
    suporte -- exatamente o que esta pagina existe para evitar.
    """

    resultado: Literal[Resultado.SUCESSO] = Resultado.SUCESSO
    pedido: str
    status_atual: EventoResposta
    historico: list[EventoResposta]
    # Opcionais mesmo em sucesso: um pedido ainda nao despachado tem ocorrencias
    # mas pode nao ter transportadora nem previsao definidas.
    transportadora: str | None = None
    previsao_entrega: date | None = None
    # A previsao venceu e o pedido nao chegou. Calculado no servidor, que conhece
    # a data corrente e o grupo do status; a UI apenas renderiza.
    entrega_atrasada: bool = False


class RespostaSemRastreio(BaseModel):
    resultado: Literal[Resultado.SEM_RASTREIO] = Resultado.SEM_RASTREIO
    pedido: str
    status_atual: None = None
    historico: list[EventoResposta] = Field(default_factory=list)
    mensagem: str


class RespostaVazioFR(BaseModel):
    """Ha fulfillment na Shopify, mas a Frete Rapido nao devolveu ocorrencias."""

    resultado: Literal[Resultado.VAZIO_FR] = Resultado.VAZIO_FR
    pedido: str
    status_atual: None = None
    historico: list[EventoResposta] = Field(default_factory=list)
    mensagem: str


class RespostaErro(BaseModel):
    """Usada por `nao_encontrado` e `erro_externo`.

    Para `nao_encontrado` a mensagem e SEMPRE a mesma, exista o pedido ou nao:
    diferenciar permitiria descobrir quais numeros existem.
    """

    resultado: Literal[Resultado.NAO_ENCONTRADO, Resultado.ERRO_EXTERNO]
    mensagem: str


RespostaConsulta = Annotated[
    RespostaSucesso | RespostaSemRastreio | RespostaVazioFR | RespostaErro,
    Field(discriminator="resultado"),
]
