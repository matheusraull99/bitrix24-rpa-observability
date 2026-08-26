"""Agregação das execuções em indicadores que dizem alguma coisa.

Duas regras que orientam o módulo:

**Percentil, não média.** Duração média de 40 s com um pico de 20 min parece
saudável e não é. A mediana mostra o caso típico; o p95 mostra o que o
usuário sente quando as coisas vão mal.

**Ausência é sinal.** Robô diário que não roda hoje não aparece em métrica
nenhuma — ele simplesmente não gera linha. Detectar *silêncio* exige comparar
com a frequência esperada, e é o alerta que mais salva operação.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .registro import Execucao, Situacao


def percentil(valores: list[float], p: float) -> float:
    """Percentil por interpolação linear, sem depender de numpy."""
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    if len(ordenados) == 1:
        return ordenados[0]
    posicao = (len(ordenados) - 1) * p
    baixo = int(posicao)
    alto = min(baixo + 1, len(ordenados) - 1)
    peso = posicao - baixo
    return ordenados[baixo] * (1 - peso) + ordenados[alto] * peso


@dataclass
class Indicadores:
    """Resumo de um robô num período."""

    robo: str
    execucoes: int = 0
    sucessos: int = 0
    falhas: int = 0
    vazias: int = 0
    perdidas: int = 0
    processados: int = 0
    registros_falhados: int = 0
    duracoes: list[float] = field(default_factory=list)
    ultima_execucao: datetime | None = None
    ultimo_sucesso: datetime | None = None
    ultimo_erro: str = ""

    @property
    def taxa_de_sucesso(self) -> float:
        return self.sucessos / self.execucoes if self.execucoes else 0.0

    @property
    def duracao_mediana(self) -> float:
        return percentil(self.duracoes, 0.5)

    @property
    def duracao_p95(self) -> float:
        return percentil(self.duracoes, 0.95)

    @property
    def horas_desde_o_ultimo_sucesso(self) -> float | None:
        if self.ultimo_sucesso is None:
            return None
        return (datetime.now(timezone.utc) - self.ultimo_sucesso).total_seconds() / 3600

    def resumo(self) -> str:
        return (
            f"{self.robo}: {self.execucoes} execucoes | "
            f"{self.taxa_de_sucesso:.0%} sucesso | "
            f"mediana {self.duracao_mediana:.0f}s, p95 {self.duracao_p95:.0f}s | "
            f"{self.processados} registros"
        )


def agregar(execucoes: list[Execucao]) -> dict[str, Indicadores]:
    """Agrupa as execuções por robô."""
    por_robo: dict[str, Indicadores] = {}

    for execucao in execucoes:
        indicadores = por_robo.setdefault(execucao.robo, Indicadores(robo=execucao.robo))
        indicadores.execucoes += 1
        indicadores.processados += execucao.processados
        indicadores.registros_falhados += execucao.falhados

        if execucao.situacao is Situacao.SUCESSO:
            indicadores.sucessos += 1
            if (
                indicadores.ultimo_sucesso is None
                or execucao.inicio > indicadores.ultimo_sucesso
            ):
                indicadores.ultimo_sucesso = execucao.inicio
        elif execucao.situacao is Situacao.FALHA:
            indicadores.falhas += 1
            if not indicadores.ultimo_erro:  # a lista vem ordenada por inicio desc
                indicadores.ultimo_erro = execucao.erro
        elif execucao.situacao is Situacao.VAZIA:
            indicadores.vazias += 1
        elif execucao.situacao is Situacao.PERDIDA:
            indicadores.perdidas += 1

        if execucao.duracao is not None:
            indicadores.duracoes.append(execucao.duracao)
        if indicadores.ultima_execucao is None or execucao.inicio > indicadores.ultima_execucao:
            indicadores.ultima_execucao = execucao.inicio

    return por_robo


@dataclass
class Alerta:
    """Algo que merece interromper alguém."""

    robo: str
    tipo: str
    mensagem: str
    gravidade: str  # "critico" ou "aviso"


#: Frequência esperada por robô, em horas. Robô que não roda no prazo é o
#: alerta que mais salva operação — e o que nenhuma métrica de execução pega,
#: porque ele simplesmente não gera linha.
FREQUENCIA_PADRAO = {"diario": 26.0, "horario": 2.0, "semanal": 8 * 24.0}


def avaliar(
    indicadores: dict[str, Indicadores],
    frequencias: dict[str, float] | None = None,
    *,
    agora: datetime | None = None,
    taxa_minima: float = 0.8,
) -> list[Alerta]:
    """Gera os alertas a partir dos indicadores.

    Args:
        indicadores: agregado por robô.
        frequencias: mapa ``robo -> horas esperadas entre execuções``.
        taxa_minima: abaixo disso a taxa de sucesso vira alerta.
    """
    agora = agora or datetime.now(timezone.utc)
    frequencias = frequencias or {}
    alertas: list[Alerta] = []

    for robo, ind in sorted(indicadores.items()):
        if ind.perdidas:
            alertas.append(
                Alerta(
                    robo, "perdida",
                    f"{ind.perdidas} execucao(oes) comecaram e nunca terminaram — "
                    "provavel queda do processo",
                    "critico",
                )
            )

        if ind.execucoes and ind.taxa_de_sucesso < taxa_minima:
            alertas.append(
                Alerta(
                    robo, "taxa_de_sucesso",
                    f"taxa de sucesso em {ind.taxa_de_sucesso:.0%} "
                    f"({ind.falhas} falhas em {ind.execucoes})"
                    + (f". Ultimo erro: {ind.ultimo_erro[:120]}" if ind.ultimo_erro else ""),
                    "critico",
                )
            )

        if ind.vazias and ind.vazias == ind.execucoes:
            alertas.append(
                Alerta(
                    robo, "sempre_vazio",
                    "todas as execucoes terminaram sem processar nada — "
                    "fonte de dados mudou?",
                    "critico",
                )
            )
        elif ind.vazias:
            alertas.append(
                Alerta(robo, "vazio", f"{ind.vazias} execucao(oes) sem processar nada",
                       "aviso")
            )

        esperada = frequencias.get(robo)
        if esperada and ind.ultima_execucao:
            horas = (agora - ind.ultima_execucao).total_seconds() / 3600
            if horas > esperada:
                alertas.append(
                    Alerta(
                        robo, "silencio",
                        f"nao roda ha {horas:.0f}h; o esperado e a cada {esperada:.0f}h",
                        "critico",
                    )
                )

    # Robô configurado que nunca apareceu: o silêncio mais absoluto.
    for robo, esperada in sorted(frequencias.items()):
        if robo not in indicadores:
            alertas.append(
                Alerta(robo, "nunca_rodou",
                       f"configurado para rodar a cada {esperada:.0f}h e sem "
                       "nenhuma execucao registrada",
                       "critico")
            )

    return alertas


def janela(horas: int, agora: datetime | None = None) -> datetime:
    """Início da janela de análise."""
    return (agora or datetime.now(timezone.utc)) - timedelta(hours=horas)
