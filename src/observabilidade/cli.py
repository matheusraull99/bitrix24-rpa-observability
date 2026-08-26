"""Linha de comando do painel: verificação para o agendador.

O painel visual (`painel.py`, em Streamlit) é para olhar. Este comando é para
o cron chamar de hora em hora: ele sai com código diferente de zero quando há
alerta crítico, e é assim que a operação descobre um problema sem que alguém
precise abrir uma tela.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .metricas import agregar, avaliar, janela
from .registro import Registro


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="painel-rpa",
        description="Verifica a saude dos RPAs e alerta sobre falhas e silencio.",
    )
    p.add_argument("--banco", type=Path, default=Path("state/rpa.db"))
    p.add_argument("--horas", type=int, default=24, help="janela de analise")
    p.add_argument(
        "--frequencias",
        type=Path,
        help='JSON com {"nome-do-robo": horas_esperadas}',
    )
    p.add_argument("--taxa-minima", type=float, default=0.8)
    p.add_argument("--json", action="store_true", help="saida em JSON")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    if not args.banco.exists():
        print(f"banco nao encontrado: {args.banco}", file=sys.stderr)
        return 2

    registro = Registro(args.banco)
    perdidas = registro.marcar_perdidas()

    frequencias = (
        json.loads(args.frequencias.read_text("utf-8")) if args.frequencias else {}
    )
    execucoes = registro.listar(desde=janela(args.horas))
    indicadores = agregar(execucoes)
    alertas = avaliar(indicadores, frequencias, taxa_minima=args.taxa_minima)

    criticos = [a for a in alertas if a.gravidade == "critico"]

    if args.json:
        print(
            json.dumps(
                {
                    "janela_horas": args.horas,
                    "execucoes": len(execucoes),
                    "marcadas_perdidas": perdidas,
                    "robos": {
                        nome: {
                            "execucoes": i.execucoes,
                            "taxa_de_sucesso": round(i.taxa_de_sucesso, 3),
                            "duracao_mediana": round(i.duracao_mediana, 1),
                            "duracao_p95": round(i.duracao_p95, 1),
                            "processados": i.processados,
                        }
                        for nome, i in sorted(indicadores.items())
                    },
                    "alertas": [
                        {"robo": a.robo, "tipo": a.tipo, "gravidade": a.gravidade,
                         "mensagem": a.mensagem}
                        for a in alertas
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if criticos else 0

    print(f"\nUltimas {args.horas}h — {len(execucoes)} execucoes")
    if perdidas:
        print(f"  {perdidas} execucao(oes) marcadas como perdidas agora")

    print()
    for _, indicador in sorted(indicadores.items()):
        marca = "  " if indicador.taxa_de_sucesso >= args.taxa_minima else "! "
        print(f"{marca}{indicador.resumo()}")

    if alertas:
        print(f"\n{len(alertas)} alertas:")
        for alerta in alertas:
            simbolo = "[CRITICO]" if alerta.gravidade == "critico" else "[aviso]  "
            print(f"  {simbolo} {alerta.robo}: {alerta.mensagem}")
    else:
        print("\nnenhum alerta")

    return 1 if criticos else 0


if __name__ == "__main__":
    raise SystemExit(main())
