"""Mapa codigo de ocorrencia -> grupo visual.

NAO traduzimos codigos: o rotulo exibido ao cliente e o campo `nome` que a
propria Frete Rapido devolve, ja em portugues legivel e sempre atualizado. Este
mapa define apenas o GRUPO.

O grupo nao e decorativo -- dirige icone, destaque, posicao na linha do tempo,
texto explicativo e a indicacao de que o cliente precisa agir. Classificar mal
pinta de verde uma devolucao ao remetente, ou deixa de avisar que a encomenda
aguarda retirada e voltara se ninguem buscar.

Catalogo oficial: https://dev.freterapido.com.br/common/ocorrencias_da_fr/
Vai de 0 a 355 COM LACUNAS -- 355 e o maior codigo, nao a quantidade.

A organizacao e por grupo (e nao codigo a codigo) justamente para permitir
conferencia visual: e facil ver que "Reversa" caiu em DEVOLUCAO e nao em
ENTREGUE.
"""

from __future__ import annotations

from app.schemas import Grupo

# Codigos cujo significado nao e conclusivo pela descricao do catalogo.
# Ficam FORA do mapa de proposito: caem no fallback EM_ANDAMENTO, que e neutro.
# Confirmar com a Frete Rapido antes de classificar.
AMBIGUOS: frozenset[int] = frozenset(
    {
        269,  # "Entrega realizada em ponto pickup" - recebida pelo cliente ou so depositada?
        285,  # "Finalizado" - finalizado o que?
    }
)

_POR_GRUPO: dict[Grupo, frozenset[int]] = {
    # Contratado, etiqueta emitida, coleta agendada: ainda nao saiu.
    Grupo.PREPARANDO: frozenset(
        {
            0,  # Contratado
            1,  # Aguardando coleta / postagem
            128,  # Solicitacao de coleta recebida pelo transportador
            130,  # Recoleta autorizada
            145,  # A caminho do endereco de COLETA (vai buscar, nao entregar)
            169,  # Buscando condutor
            187,  # Coleta agendada com o remetente
            188,  # Coleta cadastrada no sistema
            193,  # Coleta reagendada
            196,  # CT-e autorizado
            197,  # CT-e emitido
            275,  # Etiqueta emitida
            315,  # Pedido importado no sistema da transportadora
            355,  # Motoboy a caminho da loja
        }
    ),
    Grupo.COLETADO: frozenset(
        {
            15,  # Coletado / Postado
            120,  # Mercadoria embarcada
            121,  # Embarcado
            141,  # Mercadoria coletada no cliente
            191,  # Coleta com troca de mercadoria concluida
            194,  # Coleta realizada com nao conformidade
            238,  # Encomenda conferida no momento da coleta
            349,  # Despachado
        }
    ),
    Grupo.EM_TRANSITO: frozenset(
        {
            2,  # Em transito
            16,  # Em transferencia
            20,  # Entrega agendada
            23,  # Redespacho Correios
            26,  # Em conferencia na barreira fiscal
            30,  # Redespacho transportadora
            37,  # Chegada na cidade ou filial de destino
            102,  # NF liberada para entrega
            106,  # Carga em armazem
            113,  # Em conferencia no destinatario
            173,  # Canhoto retido para conferencia
            174,  # Carga aguardando voo de conexao
            175,  # Carga alocada em aeronave
            177,  # Carga desembarcada da aeronave
            178,  # Carga em separacao para embarque aereo
            186,  # Chegada na unidade
            198,  # Desembarque iniciado
            237,  # Embarque aereo confirmado
            240,  # Encomenda liberada apos analise fiscal
            295,  # Mercadoria liberada da custodia para entrega
            296,  # Mercadoria liberada pela fiscalizacao
            307,  # Objeto em correcao de rota
            308,  # Parada operacional - horario de almoco
            319,  # "Pronto para entrega" - pronto nao e o mesmo que em rota
            320,  # Recebido e processado no CD
            337,  # Saida da unidade
            345,  # Voo com escala programada
            352,  # Recebido na base
        }
    ),
    # Estado que merece destaque: chega hoje.
    Grupo.SAIU_PARA_ENTREGA: frozenset(
        {
            17,  # Em rota para entrega
        }
    ),
    Grupo.ENTREGUE: frozenset(
        {
            3,  # Entregue
            146,  # Acareacao concluida - mercadoria entregue
            258,  # Entrega com ressalvas registradas
            270,  # Entrega realizada fora do prazo
            271,  # Entrega realizada pelo redespacho no cliente
            348,  # Entregue na caixa de correios inteligente
        }
    ),
    # EXIGE ACAO: a encomenda volta se ninguem buscar.
    Grupo.AGUARDANDO_RETIRADA: frozenset(
        {
            19,  # Disponivel para retirada
            118,  # Aguardando retirada
            140,  # Entregue no PONTO DE COLETA - o cliente ainda nao recebeu
            152,  # Aguardando agendamento para retirada no aeroporto
            214,  # Destinatario solicitou retirada na unidade
            228,  # Disponivel no locker
            230,  # Disponivel para retirada - ponto Pickup
            231,  # Disponivel para retirada - rede PUDO
            232,  # Disponivel para retirada nos Correios
            239,  # Encomenda depositada no PUDO
        }
    ),
    Grupo.ENTREGA_PARCIAL: frozenset(
        {
            11,  # Entrega parcial - nao e desfecho feliz
            201,  # Destinatario aceitou entrega parcial
        }
    ),
    # EXIGE ACAO: normalmente ha nova tentativa, mas o prazo e limitado.
    Grupo.TENTATIVA_FALHA: frozenset(
        {
            5,  # Entrega nao realizada
            6,  # Reentrega / primeira tentativa
            32,  # Destinatario ausente
            34,  # Entrega nao realizada - feriado local
            35,  # Reentrega / terceira tentativa
            36,  # Reentrega / segunda tentativa
            38,  # Estabelecimento fechado
            129,  # Reentrega autorizada
            132,  # Reentrega / recoleta solicitada pelo embarcador
            205,  # Destinatario ausente - 1a tentativa
            206,  # Destinatario ausente - 2a tentativa
            207,  # Destinatario ausente - 3a tentativa
            208,  # Destinatario ausente por ferias
            261,  # Entrega nao realizada - cliente fechado para balanco
            263,  # Entrega nao realizada - falta de espaco no deposito
            264,  # Entrega nao realizada - sem documento de identificacao
            265,  # Entrega nao realizada por restricao de horario
            305,  # Nova tentativa de entrega solicitada
            332,  # Reentrega gratuita solicitada
            333,  # Reentrega solicitada pelo destinatario
            342,  # Tentativa de entrega frustrada - tempo de espera excedido
        }
    ),
    # Sem texto de orientacao na pagina (ver `ORIENTACAO` em rastreio.js): o
    # cliente ve o rotulo da propria Frete Rapido e nada mais. E o grupo certo
    # para o que exige tratamento humano, nao instrucao automatica.
    Grupo.PROBLEMA: frozenset(
        {
            # 262 "destinatario falecido" fica AQUI, e nao em TENTATIVA_FALHA,
            # apesar de ser literalmente uma tentativa de entrega frustrada.
            # Naquele grupo a pagina respondia "normalmente uma nova tentativa
            # acontece nos proximos dias uteis" -- uma promessa automatica e
            # cruel para quem acabou de perder alguem. E, com o aviso proativo
            # ligado, viraria um WhatsApp para o telefone do falecido.
            #
            # A classificacao correta e a que produz o comportamento correto.
            262,
            21, 24, 25, 28, 29, 31, 33, 40, 41, 42, 43, 44, 45,
            97, 98, 101, 103, 105, 107, 108, 109, 110, 111, 112,
            116, 117, 119, 122, 125, 126, 135, 136, 137,
            142, 143, 144, 147, 149, 150, 151, 153, 154, 155, 156, 157, 158, 159,
            160, 161, 162, 163, 164, 165, 166, 167, 168,
            176, 180, 181, 182, 183, 184, 185, 190, 192, 195,
            199, 200, 202, 203, 204, 209, 210, 211, 212, 213,
            233, 234, 235, 241, 242, 243, 244, 245,
            246, 247, 248, 249, 250, 251, 252, 253, 256, 257, 259, 260,
            266, 267, 268, 272, 273, 274, 276, 278, 279, 280, 281, 282, 283, 284,
            286, 287, 288, 289, 290, 291, 292, 293, 294, 297, 298, 299, 300,
            301, 302, 303, 304, 317, 318, 321, 322,
            323, 324, 325, 326, 327, 328, 329, 330, 331, 335, 336, 338, 339,
            341, 343, 344, 347, 350, 351,
        }
    ),
    Grupo.DEVOLUCAO: frozenset(
        {
            7, 8, 9, 12, 13, 14,  # devolucao total e parcial
            22,  # Pedido recusado pelo destinatario
            104,  # Devolucao autorizada pelo cliente
            114,  # Recusado por avaria
            115,  # Recusa por falta
            215, 216, 217, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227,
            306,  # Objeto devolvido pelos Correios
            # Logistica REVERSA: a mercadoria volta ao remetente.
            # 314 e "Pedido entregue - Reversa": entregue de volta A LOJA,
            # jamais verde para o cliente.
            309, 310, 311, 312, 313, 314,
            316,  # Processo de devolucao iniciado
            340,  # Solicitacao de reversa
        }
    ),
    Grupo.SINISTRO: frozenset(
        {
            18,  # Sinistro / roubo
            27,  # Extravio
            127,  # Extravio por falta de informacao
            131,  # Perda embarcador
            133,  # Extravio por acareacao nao atendida
            134,  # Extravio por baixa indevida
            138,  # Sinistro
            139,  # Roubo
            148,  # Acareacao sem sucesso - mercadoria extraviada
            179,  # Carga extraviada pela companhia aerea
            236,  # Embalagem danificada por sinistro
            277,  # Extravio durante transferencia aerea
        }
    ),
    Grupo.CANCELADO: frozenset(
        {
            99,  # Cancelado
            170,  # Cancelado - erro na impressao
            171,  # Cancelado pelo destinatario
            172,  # Cancelado pelo remetente
            189,  # Coleta cancelada - sem itens disponiveis
            254,  # Entrega cancelada
            255,  # Entrega cancelada - validade expirada
            334,  # Remessa cancelada - erro nas informacoes
            346,  # Cancelado - problema
            353,  # Cancelado - motoboy nao encontrado
            354,  # Cancelado pela transportadora
        }
    ),
}


def _inverter() -> dict[int, Grupo]:
    mapa: dict[int, Grupo] = {}
    for grupo, codigos in _POR_GRUPO.items():
        for codigo in codigos:
            if codigo in mapa:
                raise RuntimeError(
                    f"codigo {codigo} classificado em dois grupos: "
                    f"{mapa[codigo]} e {grupo}"
                )
            mapa[codigo] = grupo
    return mapa


POR_CODIGO: dict[int, Grupo] = _inverter()


def classificar(codigo: int) -> Grupo:
    """Grupo de um codigo. Codigo desconhecido nunca quebra: cai no neutro.

    A Frete Rapido segue adicionando codigos; o fallback garante que um codigo
    novo apareca com o rotulo correto (vindo da API) e tratamento visual neutro,
    em vez de quebrar a pagina.
    """
    return POR_CODIGO.get(codigo, Grupo.EM_ANDAMENTO)


def exige_acao_do_cliente(grupo: Grupo) -> bool:
    """Grupos em que a pagina de fato evita um contato no suporte."""
    return grupo in (Grupo.AGUARDANDO_RETIRADA, Grupo.TENTATIVA_FALHA)
