"""Registro de execuções de RPA — o que medir e como não mentir na medição.

Todo robô da série grava aqui: início, fim, resultado, contadores. O painel
lê. A parte que exige cuidado não é armazenar — é **classificar corretamente
o que aconteceu**, porque as três armadilhas abaixo produzem painéis verdes
sobre operações quebradas:

**Execução que nunca terminou não é sucesso nem falha.** Robô morto por OOM
não grava o fim. Contar como "em andamento" para sempre esconde a queda;
contar como sucesso é pior. Aqui vira ``PERDIDA`` depois do prazo esperado.

**Sucesso com zero registros processados é suspeito.** O importador rodou,
não deu erro e importou nada — porque o caminho da planilha mudou. Tecnicamente
sucesso, operacionalmente uma falha silenciosa.

**Média esconde o que importa.** Duração média de 40 s com um pico de 20 min
parece saudável. A mediana e o p95 contam a história real.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("observabilidade")


class Situacao(str, Enum):
    """Desfecho de uma execução."""

    EM_ANDAMENTO = "em_andamento"
    SUCESSO = "sucesso"
    FALHA = "falha"
    PERDIDA = "perdida"          # começou e nunca reportou o fim
    VAZIA = "vazia"              # terminou bem sem processar nada


@dataclass
class Execucao:
    """Uma execução de robô."""

    id: str
    robo: str
    inicio: datetime
    fim: datetime | None = None
    situacao: Situacao = Situacao.EM_ANDAMENTO
    processados: int = 0
    falhados: int = 0
    erro: str = ""
    maquina: str = ""
    detalhes: dict[str, Any] = field(default_factory=dict)

    @property
    def duracao(self) -> float | None:
        return (self.fim - self.inicio).total_seconds() if self.fim else None

    @property
    def saudavel(self) -> bool:
        return self.situacao is Situacao.SUCESSO and self.falhados == 0


#: Quanto tempo depois do início uma execução sem fim vira ``PERDIDA``.
PRAZO_PADRAO = timedelta(hours=6)

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS execucoes (
    id           TEXT PRIMARY KEY,
    robo         TEXT NOT NULL,
    inicio       TEXT NOT NULL,
    fim          TEXT,
    situacao     TEXT NOT NULL,
    processados  INTEGER NOT NULL DEFAULT 0,
    falhados     INTEGER NOT NULL DEFAULT 0,
    erro         TEXT NOT NULL DEFAULT '',
    maquina      TEXT NOT NULL DEFAULT '',
    detalhes     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_robo_inicio ON execucoes (robo, inicio DESC);
CREATE INDEX IF NOT EXISTS idx_situacao ON execucoes (situacao);
"""


class Registro:
    """Persistência em SQLite.

    SQLite e não Postgres: o volume é de dezenas de execuções por dia, o
    arquivo vai junto do robô e não há mais um serviço para manter de pé.
    Quando o painel precisar de dado de várias máquinas, o mesmo arquivo é
    sincronizado — ou a classe ganha outro backend sem mudar quem a usa.
    """

    def __init__(self, caminho: Path, *, prazo: timedelta = PRAZO_PADRAO) -> None:
        self.caminho = caminho
        self.prazo = prazo
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        with self._conexao() as con:
            con.executescript(_ESQUEMA)

    @contextmanager
    def _conexao(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.caminho, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            # WAL permite o painel ler enquanto um robô escreve — sem isso,
            # abrir o dashboard durante uma carga trava a carga.
            con.execute("PRAGMA journal_mode=WAL")
            yield con
            con.commit()
        finally:
            con.close()

    # ------------------------------------------------------------------ #

    def iniciar(self, robo: str, **detalhes: Any) -> Execucao:
        """Registra o início de uma execução."""
        execucao = Execucao(
            id=uuid.uuid4().hex,
            robo=robo,
            inicio=datetime.now(timezone.utc),
            maquina=os.environ.get("HOSTNAME") or socket.gethostname(),
            detalhes=dict(detalhes),
        )
        with self._conexao() as con:
            con.execute(
                "INSERT INTO execucoes (id, robo, inicio, situacao, maquina, detalhes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    execucao.id, execucao.robo, execucao.inicio.isoformat(),
                    execucao.situacao.value, execucao.maquina,
                    json.dumps(execucao.detalhes, ensure_ascii=False, default=str),
                ),
            )
        return execucao

    def concluir(
        self,
        execucao: Execucao,
        *,
        processados: int = 0,
        falhados: int = 0,
        erro: str = "",
    ) -> Execucao:
        """Fecha a execução, classificando o desfecho."""
        execucao.fim = datetime.now(timezone.utc)
        execucao.processados = processados
        execucao.falhados = falhados
        execucao.erro = erro

        if erro:
            execucao.situacao = Situacao.FALHA
        elif processados == 0:
            # Rodou, nao deu erro e nao fez nada. Tecnicamente sucesso;
            # operacionalmente, quase sempre uma falha silenciosa.
            execucao.situacao = Situacao.VAZIA
        else:
            execucao.situacao = Situacao.SUCESSO

        with self._conexao() as con:
            con.execute(
                "UPDATE execucoes SET fim=?, situacao=?, processados=?, falhados=?, "
                "erro=? WHERE id=?",
                (
                    execucao.fim.isoformat(), execucao.situacao.value,
                    processados, falhados, erro[:2000], execucao.id,
                ),
            )
        return execucao

    def marcar_perdidas(self, agora: datetime | None = None) -> int:
        """Converte em ``PERDIDA`` o que começou e nunca terminou.

        Sem isso, robô morto por falta de memória fica "em andamento" para
        sempre e some do radar — o pior desfecho possível para um painel.
        """
        limite = (agora or datetime.now(timezone.utc)) - self.prazo
        with self._conexao() as con:
            cursor = con.execute(
                "UPDATE execucoes SET situacao=? WHERE situacao=? AND inicio < ?",
                (Situacao.PERDIDA.value, Situacao.EM_ANDAMENTO.value, limite.isoformat()),
            )
            return cursor.rowcount

    def listar(
        self, *, robo: str | None = None, desde: datetime | None = None, limite: int = 500
    ) -> list[Execucao]:
        consulta = "SELECT * FROM execucoes WHERE 1=1"
        parametros: list[Any] = []
        if robo:
            consulta += " AND robo = ?"
            parametros.append(robo)
        if desde:
            consulta += " AND inicio >= ?"
            parametros.append(desde.isoformat())
        consulta += " ORDER BY inicio DESC LIMIT ?"
        parametros.append(limite)

        with self._conexao() as con:
            return [_da_linha(l) for l in con.execute(consulta, parametros)]

    def robos(self) -> list[str]:
        with self._conexao() as con:
            return [l[0] for l in con.execute("SELECT DISTINCT robo FROM execucoes ORDER BY 1")]

    @contextmanager
    def executando(self, robo: str, **detalhes: Any) -> Iterator[Execucao]:
        """Contexto que registra início, fim e exceção automaticamente.

        Uso::

            with registro.executando("importador-leads") as execucao:
                execucao.processados = importar()

        A exceção é re-levantada depois de gravada: o robô ainda precisa
        falhar visivelmente para o agendador.
        """
        execucao = self.iniciar(robo, **detalhes)
        try:
            yield execucao
        except Exception as exc:
            self.concluir(
                execucao,
                processados=execucao.processados,
                falhados=execucao.falhados,
                erro=f"{type(exc).__name__}: {exc}",
            )
            raise
        else:
            self.concluir(
                execucao,
                processados=execucao.processados,
                falhados=execucao.falhados,
            )


def _da_linha(linha: sqlite3.Row) -> Execucao:
    return Execucao(
        id=linha["id"],
        robo=linha["robo"],
        inicio=datetime.fromisoformat(linha["inicio"]),
        fim=datetime.fromisoformat(linha["fim"]) if linha["fim"] else None,
        situacao=Situacao(linha["situacao"]),
        processados=linha["processados"],
        falhados=linha["falhados"],
        erro=linha["erro"],
        maquina=linha["maquina"],
        detalhes=json.loads(linha["detalhes"] or "{}"),
    )


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def cronometro() -> float:
    return time.monotonic()
