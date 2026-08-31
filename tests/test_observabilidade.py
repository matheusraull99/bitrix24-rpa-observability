"""Testes do registro e das métricas."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from observabilidade.metricas import Alerta, agregar, avaliar, percentil
from observabilidade.registro import Execucao, Registro, Situacao

AGORA = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def registro(tmp_path) -> Registro:
    return Registro(tmp_path / "rpa.db")


def execucao(
    robo="importador",
    situacao=Situacao.SUCESSO,
    inicio=AGORA,
    duracao_s=30.0,
    processados=100,
    falhados=0,
    erro="",
) -> Execucao:
    return Execucao(
        id=f"{robo}-{inicio.isoformat()}",
        robo=robo,
        inicio=inicio,
        fim=inicio + timedelta(seconds=duracao_s) if duracao_s else None,
        situacao=situacao,
        processados=processados,
        falhados=falhados,
        erro=erro,
    )


class TestRegistro:
    def test_ciclo_completo(self, registro):
        exec_ = registro.iniciar("importador", origem="planilha.csv")
        assert exec_.situacao is Situacao.EM_ANDAMENTO

        registro.concluir(exec_, processados=42)
        assert exec_.situacao is Situacao.SUCESSO
        assert exec_.duracao is not None

    def test_erro_vira_falha(self, registro):
        exec_ = registro.iniciar("importador")
        registro.concluir(exec_, erro="portal fora do ar")
        assert exec_.situacao is Situacao.FALHA

    def test_zero_processados_vira_vazia(self, registro):
        """Rodou, nao deu erro e nao fez nada: falha silenciosa."""
        exec_ = registro.iniciar("importador")
        registro.concluir(exec_, processados=0)
        assert exec_.situacao is Situacao.VAZIA

    def test_persiste_entre_instancias(self, tmp_path):
        caminho = tmp_path / "rpa.db"
        primeiro = Registro(caminho)
        exec_ = primeiro.iniciar("importador")
        primeiro.concluir(exec_, processados=5)

        segundo = Registro(caminho)
        assert len(segundo.listar()) == 1

    def test_guarda_detalhes(self, registro):
        exec_ = registro.iniciar("importador", arquivo="a.csv", linhas=500)
        registro.concluir(exec_, processados=500)
        assert registro.listar()[0].detalhes == {"arquivo": "a.csv", "linhas": 500}

    def test_lista_os_robos(self, registro):
        for robo in ("importador", "vigia", "importador"):
            registro.concluir(registro.iniciar(robo), processados=1)
        assert registro.robos() == ["importador", "vigia"]

    def test_filtra_por_robo(self, registro):
        registro.concluir(registro.iniciar("importador"), processados=1)
        registro.concluir(registro.iniciar("vigia"), processados=1)
        assert len(registro.listar(robo="vigia")) == 1


class TestPerdidas:
    def test_execucao_antiga_sem_fim_vira_perdida(self, tmp_path):
        """Robo morto por OOM ficaria 'em andamento' para sempre."""
        registro = Registro(tmp_path / "rpa.db", prazo=timedelta(hours=6))
        registro.iniciar("importador")

        # Empurra o inicio para 8 horas atras.
        with registro._conexao() as con:
            con.execute(
                "UPDATE execucoes SET inicio=?",
                ((AGORA - timedelta(hours=8)).isoformat(),),
            )

        assert registro.marcar_perdidas(agora=AGORA) == 1
        assert registro.listar()[0].situacao is Situacao.PERDIDA

    def test_execucao_recente_continua_em_andamento(self, tmp_path):
        registro = Registro(tmp_path / "rpa.db", prazo=timedelta(hours=6))
        registro.iniciar("importador")
        # Sem `agora`: usa o relogio real, contra o qual `iniciar` gravou.
        assert registro.marcar_perdidas() == 0
        assert registro.listar()[0].situacao is Situacao.EM_ANDAMENTO


class TestContexto:
    def test_sucesso_automatico(self, registro):
        with registro.executando("importador") as exec_:
            exec_.processados = 10
        assert registro.listar()[0].situacao is Situacao.SUCESSO

    def test_excecao_vira_falha_e_e_relevantada(self, registro):
        """O robo ainda precisa falhar visivelmente para o agendador."""
        with pytest.raises(RuntimeError, match="portal fora"), registro.executando("importador"):
            raise RuntimeError("portal fora")

        gravada = registro.listar()[0]
        assert gravada.situacao is Situacao.FALHA
        assert "RuntimeError: portal fora" in gravada.erro

    def test_contadores_parciais_sao_gravados_na_falha(self, registro):
        """Saber que 300 entraram antes do erro muda a decisao de reprocesso."""
        with pytest.raises(RuntimeError), registro.executando("importador") as exec_:
            exec_.processados = 300
            raise RuntimeError("caiu no meio")

        assert registro.listar()[0].processados == 300


class TestPercentil:
    def test_mediana(self):
        assert percentil([1, 2, 3, 4, 5], 0.5) == 3

    def test_p95_pega_a_cauda(self):
        """Com 5% de execucoes lentas, o p95 mostra o que a mediana esconde."""
        valores = [30.0] * 90 + [1200.0] * 10
        assert percentil(valores, 0.5) == 30.0
        assert percentil(valores, 0.95) > 1000

    def test_p95_nao_isola_um_unico_outlier_em_amostra_pequena(self):
        """Propriedade da estatistica, nao defeito: 1 em 20 nao e o percentil 95.

        Documentado porque a leitura ingenua do painel seria "o p95 nao pegou
        o pico de 20 minutos" — e a resposta e que 20 execucoes sao poucas
        para esse percentil dizer alguma coisa.
        """
        valores = [30.0] * 19 + [1200.0]
        assert percentil(valores, 0.95) < 200

    def test_lista_vazia(self):
        assert percentil([], 0.5) == 0.0

    def test_um_valor(self):
        assert percentil([42.0], 0.95) == 42.0


class TestAgregacao:
    def test_conta_por_situacao(self):
        execucoes = [
            execucao(situacao=Situacao.SUCESSO),
            execucao(situacao=Situacao.FALHA, erro="erro X", processados=0),
            execucao(situacao=Situacao.VAZIA, processados=0),
            execucao(situacao=Situacao.PERDIDA, duracao_s=0),
        ]
        ind = agregar(execucoes)["importador"]
        assert (ind.sucessos, ind.falhas, ind.vazias, ind.perdidas) == (1, 1, 1, 1)

    def test_taxa_de_sucesso(self):
        execucoes = [execucao() for _ in range(3)] + [execucao(situacao=Situacao.FALHA)]
        assert agregar(execucoes)["importador"].taxa_de_sucesso == 0.75

    def test_mediana_e_p95(self):
        execucoes = [execucao(duracao_s=30.0) for _ in range(90)]
        execucoes += [execucao(duracao_s=1200.0) for _ in range(10)]
        ind = agregar(execucoes)["importador"]
        assert ind.duracao_mediana == 30.0
        assert ind.duracao_p95 > 1000, "a mediana sozinha esconderia a cauda"

    def test_separa_robos(self):
        agregado = agregar([execucao(robo="a"), execucao(robo="b")])
        assert set(agregado) == {"a", "b"}

    def test_guarda_o_ultimo_erro(self):
        execucoes = [
            execucao(situacao=Situacao.FALHA, erro="mais recente", inicio=AGORA),
            execucao(
                situacao=Situacao.FALHA, erro="mais antigo",
                inicio=AGORA - timedelta(hours=2),
            ),
        ]
        assert agregar(execucoes)["importador"].ultimo_erro == "mais recente"


class TestAlertas:
    def test_perdida_e_critico(self):
        ind = agregar([execucao(situacao=Situacao.PERDIDA, duracao_s=0)])
        alertas = avaliar(ind, agora=AGORA)
        assert any(a.tipo == "perdida" and a.gravidade == "critico" for a in alertas)

    def test_taxa_baixa_alarma_com_o_erro(self):
        execucoes = [execucao(situacao=Situacao.FALHA, erro="ACCESS_DENIED")] * 3
        execucoes.append(execucao())
        alertas = avaliar(agregar(execucoes), agora=AGORA)
        taxa = next(a for a in alertas if a.tipo == "taxa_de_sucesso")
        assert "ACCESS_DENIED" in taxa.mensagem

    def test_tudo_vazio_e_critico(self):
        ind = agregar([execucao(situacao=Situacao.VAZIA, processados=0) for _ in range(3)])
        alertas = avaliar(ind, agora=AGORA)
        assert any(a.tipo == "sempre_vazio" and a.gravidade == "critico" for a in alertas)

    def test_uma_vazia_e_so_aviso(self):
        execucoes = [execucao() for _ in range(5)]
        execucoes.append(execucao(situacao=Situacao.VAZIA, processados=0))
        alertas = avaliar(agregar(execucoes), agora=AGORA)
        vazio = next(a for a in alertas if a.tipo == "vazio")
        assert vazio.gravidade == "aviso"

    def test_silencio_e_detectado(self):
        """O alerta que mais salva operacao — e que nenhuma metrica pega."""
        ind = agregar([execucao(inicio=AGORA - timedelta(hours=48))])
        alertas = avaliar(ind, {"importador": 26.0}, agora=AGORA)
        assert any(a.tipo == "silencio" for a in alertas)

    def test_dentro_da_frequencia_nao_alarma(self):
        ind = agregar([execucao(inicio=AGORA - timedelta(hours=12))])
        alertas = avaliar(ind, {"importador": 26.0}, agora=AGORA)
        assert not any(a.tipo == "silencio" for a in alertas)

    def test_robo_configurado_que_nunca_rodou(self):
        """O silencio mais absoluto: nao ha nem linha para agregar."""
        alertas = avaliar({}, {"vigia-sla": 26.0}, agora=AGORA)
        assert alertas == [
            Alerta("vigia-sla", "nunca_rodou",
                   "configurado para rodar a cada 26h e sem nenhuma execucao registrada",
                   "critico")
        ]

    def test_operacao_saudavel_nao_gera_alerta(self):
        ind = agregar([execucao(inicio=AGORA - timedelta(hours=2)) for _ in range(10)])
        assert avaliar(ind, {"importador": 26.0}, agora=AGORA) == []
