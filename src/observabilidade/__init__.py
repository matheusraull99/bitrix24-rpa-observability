"""Observabilidade dos RPAs: registro de execucoes, metricas e alertas."""

from .metricas import (
    FREQUENCIA_PADRAO,
    Alerta,
    Indicadores,
    agregar,
    avaliar,
    janela,
    percentil,
)
from .registro import PRAZO_PADRAO, Execucao, Registro, Situacao

__version__ = "1.0.0"

__all__ = [
    "FREQUENCIA_PADRAO",
    "PRAZO_PADRAO",
    "Alerta",
    "Execucao",
    "Indicadores",
    "Registro",
    "Situacao",
    "agregar",
    "avaliar",
    "janela",
    "percentil",
]
