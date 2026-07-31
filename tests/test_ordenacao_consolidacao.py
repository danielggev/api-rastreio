"""Ordenacao estavel, consolidacao de campos e atribuicao de fuso."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.schemas import Grupo, OcorrenciaFR
from app.services.consolidacao import codigo_volume, previsao_entrega, transportadora
from app.services.datas import atribuir_fuso, entrega_atrasada, hoje_local
from app.services.ocorrencias import classificar
from app.services.ordenacao import indexar, ordenar_desc

FIXTURES = Path(__file__).parent / "fixtures"


def _carregar(nome: str) -> list[OcorrenciaFR]:
    dados = json.loads((FIXTURES / nome).read_text(encoding="utf-8"))
    return indexar([OcorrenciaFR.model_validate(o) for o in dados])


def test_desempate_com_timestamps_identicos_fixture_real() -> None:
    """TESTE OBRIGATORIO do plano.

    Em `resposta-59483.json` os codigos 0 e 1 tem o MESMO timestamp. O status
    atual deve ser o 1 ("Aguardando coleta"), nunca o 0 ("Contratado") -- exibir
    o 0 diria ao cliente que o pedido esta menos avancado do que esta.
    """
    ocorrencias = _carregar("resposta-59483.json")
    assert ocorrencias[0].data_ocorrencia == ocorrencias[1].data_ocorrencia

    ordenadas = ordenar_desc(ocorrencias)
    assert ordenadas[0].codigo == 1
    assert ordenadas[-1].codigo == 0


def test_desempate_e_deterministico_em_varias_execucoes() -> None:
    ocorrencias = _carregar("resposta-59483.json")
    resultados = {tuple(o.codigo for o in ordenar_desc(ocorrencias)) for _ in range(20)}
    assert len(resultados) == 1


def test_ordena_por_data_quando_ha_diferenca() -> None:
    ocorrencias = indexar(
        [
            OcorrenciaFR(codigo=0, data_ocorrencia=datetime(2026, 7, 20, 10, 0)),
            OcorrenciaFR(codigo=2, data_ocorrencia=datetime(2026, 7, 25, 10, 0)),
            OcorrenciaFR(codigo=1, data_ocorrencia=datetime(2026, 7, 22, 10, 0)),
        ]
    )
    assert [o.codigo for o in ordenar_desc(ocorrencias)] == [2, 1, 0]


def test_ocorrencia_sem_data_nao_vira_status_atual() -> None:
    ocorrencias = indexar(
        [
            OcorrenciaFR(codigo=99, data_ocorrencia=None),
            OcorrenciaFR(codigo=2, data_ocorrencia=datetime(2026, 7, 25, 10, 0)),
        ]
    )
    assert ordenar_desc(ocorrencias)[0].codigo == 2


def test_previsao_valida_nao_e_apagada_por_ocorrencia_recente_nula() -> None:
    """TESTE OBRIGATORIO do plano.

    Pegar sempre o valor da ocorrencia mais recente apagaria uma previsao
    legitima registrada antes.
    """
    ordenadas = [
        OcorrenciaFR(codigo=2, data_prevista_entrega=None),  # mais recente, nula
        OcorrenciaFR(codigo=1, data_prevista_entrega=date(2026, 8, 2)),
        OcorrenciaFR(codigo=0, data_prevista_entrega=date(2026, 7, 29)),
    ]
    assert previsao_entrega(ordenadas) == date(2026, 8, 2)


def test_transportadora_nao_nula_mais_recente() -> None:
    ordenadas = [
        OcorrenciaFR(codigo=2, razao_social_transportadora=None),
        OcorrenciaFR(codigo=1, razao_social_transportadora="JADLOG LOGISTICA S.A"),
    ]
    assert transportadora(ordenadas) == "JADLOG LOGISTICA S.A"


def test_consolidacao_devolve_nulo_quando_ninguem_tem_valor() -> None:
    ordenadas = [OcorrenciaFR(codigo=1), OcorrenciaFR(codigo=0)]
    assert previsao_entrega(ordenadas) is None
    assert transportadora(ordenadas) is None
    assert codigo_volume(ordenadas) is None


def test_fixture_real_consolida_previsao_e_transportadora() -> None:
    ordenadas = ordenar_desc(_carregar("resposta-59552.json"))
    assert previsao_entrega(ordenadas) == date(2026, 7, 29)
    assert transportadora(ordenadas) == "JADLOG LOGISTICA S.A"


def test_fixture_real_de_pedido_entregue() -> None:
    """Pedido 59551: unico estado terminal capturado da operacao real.

    Tres ocorrencias, com "Entregue" no topo e os codigos 0 e 1 empatados no
    mesmo instante -- exercita ordenacao e classificacao de uma vez.
    """
    ordenadas = ordenar_desc(_carregar("resposta-59551.json"))

    assert [o.codigo for o in ordenadas] == [3, 1, 0]
    assert ordenadas[0].nome == "Entregue"
    assert classificar(ordenadas[0].codigo) is Grupo.ENTREGUE
    # Transportadora diferente da do 59552: a operacao usa varias.
    assert transportadora(ordenadas) == "EMPRESA BRASILEIRA DE CORREIOS E TELEGRAFOS"


def test_entrega_apos_a_previsao_nao_e_reportada_como_atraso() -> None:
    """Caso real do pedido 59551: previsto 27/07, entregue 30/07.

    Avisar de atraso para quem ja recebeu seria ruido -- o cliente abriu a
    pagina para saber onde esta a encomenda, e ela chegou.
    """
    ordenadas = ordenar_desc(_carregar("resposta-59551.json"))
    previsao = previsao_entrega(ordenadas)
    entregue = classificar(ordenadas[0].codigo) is Grupo.ENTREGUE

    assert previsao == date(2026, 7, 27)
    assert entregue
    assert not entrega_atrasada(previsao, entregue=entregue)


def test_dado_pessoal_do_entregador_nao_sobrevive_ao_parsing_real() -> None:
    """A fixture real traz `cnpj_cpf_entregador` e `nome_entregador`.

    Vem nulos neste pedido, mas o que importa e que os campos nao existem no
    modelo -- entao tambem nao existiriam se viessem preenchidos.
    """
    for ocorrencia in _carregar("resposta-59551.json"):
        serializada = ocorrencia.model_dump()
        assert "cnpj_cpf_entregador" not in serializada
        assert "nome_entregador" not in serializada
        assert "comprovantes" not in serializada


def test_atribuir_fuso_nao_desloca_o_relogio() -> None:
    """Atribuicao declara a origem; nao muda os numeros do horario."""
    naive = datetime(2026, 7, 23, 15, 37, 12)
    com_fuso = atribuir_fuso(naive, ZoneInfo("America/Sao_Paulo"))
    assert com_fuso is not None
    assert (com_fuso.hour, com_fuso.minute) == (15, 37)
    assert com_fuso.utcoffset() is not None


def test_atribuir_fuso_respeita_data_que_ja_tem_fuso() -> None:
    """Se a API passar a mandar fuso, reatribuir corromperia o instante."""
    ja_aware = datetime(2026, 7, 23, 15, 37, 12, tzinfo=ZoneInfo("UTC"))
    assert atribuir_fuso(ja_aware, ZoneInfo("America/Sao_Paulo")) == ja_aware


def test_atribuir_fuso_aceita_nulo() -> None:
    assert atribuir_fuso(None) is None


# --------------------------------------------------------------------------
# Previsao vencida
# --------------------------------------------------------------------------


def test_previsao_vencida_com_pedido_a_caminho_e_atraso() -> None:
    """Caso real: pedido 59552 previsto para 29/07 e ainda "aguardando coleta".

    Exibir "Previsao de entrega: 29/07" no dia 30 faz o cliente achar que o
    sistema esta quebrado, quando na verdade o frete atrasou.
    """
    ontem = date.today() - timedelta(days=1)
    assert entrega_atrasada(ontem, entregue=False)


def test_previsao_vencida_mas_ja_entregue_nao_e_atraso() -> None:
    """Entregue com atraso ja e passado; o cliente recebeu."""
    ontem = date.today() - timedelta(days=1)
    assert not entrega_atrasada(ontem, entregue=True)


def test_previsao_futura_nao_e_atraso() -> None:
    amanha = date.today() + timedelta(days=1)
    assert not entrega_atrasada(amanha, entregue=False)


def test_previsao_para_hoje_ainda_nao_e_atraso() -> None:
    """O prazo vence no fim do dia, nao no comeco."""
    assert not entrega_atrasada(date.today(), entregue=False)


def test_sem_previsao_nao_ha_atraso_a_declarar() -> None:
    assert not entrega_atrasada(None, entregue=False)


def test_hoje_local_usa_o_fuso_de_exibicao() -> None:
    """`date.today()` do servidor daria o dia errado se a maquina rodar em UTC."""
    assert hoje_local(ZoneInfo("America/Sao_Paulo")) == datetime.now(
        ZoneInfo("America/Sao_Paulo")
    ).date()
