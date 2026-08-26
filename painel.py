"""Painel visual dos RPAs.

    streamlit run painel.py -- --banco state/rpa.db

O painel é deliberadamente simples: um cartão por robô, alertas no topo e a
lista de execuções recentes embaixo. Painel de RPA que precisa de explicação
não é olhado — e painel que não é olhado não serve para nada.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from observabilidade.metricas import agregar, avaliar, janela  # noqa: E402
from observabilidade.registro import Registro, Situacao  # noqa: E402

CORES = {
    Situacao.SUCESSO: "🟢",
    Situacao.FALHA: "🔴",
    Situacao.VAZIA: "🟡",
    Situacao.PERDIDA: "🟠",
    Situacao.EM_ANDAMENTO: "🔵",
}


def main() -> None:
    st.set_page_config(page_title="RPAs do Bitrix24", page_icon="🤖", layout="wide")
    st.title("🤖 RPAs do Bitrix24")

    banco = Path(st.sidebar.text_input("Banco", "state/rpa.db"))
    horas = st.sidebar.slider("Janela (horas)", 6, 24 * 14, 24)
    taxa_minima = st.sidebar.slider("Taxa mínima de sucesso", 0.0, 1.0, 0.8, 0.05)

    if not banco.exists():
        st.error(f"Banco não encontrado: {banco}")
        st.stop()

    registro = Registro(banco)
    perdidas = registro.marcar_perdidas()
    execucoes = registro.listar(desde=janela(horas), limite=2000)
    indicadores = agregar(execucoes)
    alertas = avaliar(indicadores, taxa_minima=taxa_minima)

    if perdidas:
        st.warning(f"{perdidas} execução(ões) marcadas como perdidas agora.")

    criticos = [a for a in alertas if a.gravidade == "critico"]
    if criticos:
        st.error(f"### {len(criticos)} alertas críticos")
        for alerta in criticos:
            st.write(f"**{alerta.robo}** — {alerta.mensagem}")
    elif alertas:
        st.warning(f"{len(alertas)} avisos")
    else:
        st.success("Nenhum alerta na janela selecionada.")

    st.divider()
    st.subheader("Robôs")

    for _, indicador in sorted(indicadores.items()):
        with st.container(border=True):
            colunas = st.columns([3, 1, 1, 1, 1])
            colunas[0].markdown(f"**{indicador.robo}**")
            colunas[1].metric("Execuções", indicador.execucoes)
            colunas[2].metric("Sucesso", f"{indicador.taxa_de_sucesso:.0%}")
            # Mediana e p95 lado a lado: a media sozinha esconderia o pico.
            colunas[3].metric("Mediana", f"{indicador.duracao_mediana:.0f}s")
            colunas[4].metric("p95", f"{indicador.duracao_p95:.0f}s")
            if indicador.ultimo_erro:
                st.caption(f"Último erro: {indicador.ultimo_erro[:200]}")

    st.divider()
    st.subheader("Execuções recentes")
    st.dataframe(
        [
            {
                "": CORES.get(e.situacao, "⚪"),
                "Robô": e.robo,
                "Início": e.inicio.strftime("%d/%m %H:%M"),
                "Duração": f"{e.duracao:.0f}s" if e.duracao else "—",
                "Processados": e.processados,
                "Falhados": e.falhados,
                "Erro": e.erro[:80],
            }
            for e in execucoes[:100]
        ],
        use_container_width=True,
        hide_index=True,
    )


if __name__ == "__main__":
    main()
