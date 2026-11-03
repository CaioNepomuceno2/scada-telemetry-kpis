"""Painel de exemplo sobre o modelo estrela exportado por ``telemetry-kpis calcular``.

streamlit run app/app.py -- --dados exemplos/powerbi
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from telemetry_kpis import analitica, config

RAIZ = Path(__file__).resolve().parents[1]
ap = argparse.ArgumentParser()
ap.add_argument("--dados", default=str(RAIZ / "exemplos" / "powerbi"))
args, _ = ap.parse_known_args(sys.argv[1:])
pasta = Path(args.dados)

st.set_page_config(page_title="Telemetria SCADA", layout="wide")


@st.cache_data
def ler(nome: str) -> pd.DataFrame:
    return pd.read_parquet(pasta / f"{nome}.parquet")


eq = ler("dim_equipamento")
mensal = ler("fato_disponibilidade_mensal").merge(eq, on="equipamento")
cmd = ler("fato_comando").merge(eq, on="equipamento")

st.title("Telemetria de religadores e subestações")
st.caption(f"Dados: {pasta} (frota sintética de exemplo, se não indicado outro caminho)")

with st.sidebar:
    filtros = {}
    for col in ("regiao", "subestacao", "tipo", "tecnologia"):
        if col in eq:
            op = sorted(eq[col].dropna().unique())
            filtros[col] = st.multiselect(col.capitalize(), op)
    meta = st.slider("Meta de disponibilidade", 0.90, 1.0, 0.98, 0.005)

for col, sel in filtros.items():
    if sel:
        mensal = mensal[mensal[col].isin(sel)]
        cmd = cmd[cmd[col].isin(sel)]


def disp(df):
    return 1 - df["seg_indisp"].sum() / (df["seg_periodo"].sum() - df["seg_manutencao"].sum())


c1, c2, c3, c4 = st.columns(4)
c1.metric("Disponibilidade da comunicação", f"{disp(mensal):.2%}")
c2.metric("Assertividade dos comandos", f"{(cmd['resultado'] == 'SUCESSO').mean():.1%}")
c3.metric("Comandos sem comunicação", f"{(cmd['resultado'] == 'SEM_COMUNICACAO').mean():.1%}")
c4.metric("Equipamentos", f"{mensal['equipamento'].nunique():,}")

st.subheader("Disponibilidade mensal")
st.line_chart(mensal.groupby("ano_mes").apply(disp, include_groups=False).rename("disponibilidade"))

col_a, col_b = st.columns(2)
with col_a:
    st.subheader("Piores equipamentos")
    pior = (mensal.groupby(["equipamento", "subestacao", "tecnologia"])
            .apply(disp, include_groups=False).rename("disponibilidade").reset_index()
            .sort_values("disponibilidade").head(20))
    st.dataframe(pior.style.format({"disponibilidade": "{:.2%}"}), hide_index=True)
with col_b:
    st.subheader("Resultado dos telecomandos")
    st.bar_chart(cmd["resultado"].value_counts(normalize=True))

st.subheader("Fila de manutenção priorizada por valor")
cfg = config.carregar()["valor"]
cfg = {**cfg, "disponibilidade_pos_reparo": max(meta, cfg["disponibilidade_pos_reparo"])}
recente = sorted(mensal["ano_mes"].unique())[-3:]
m3 = mensal[mensal["ano_mes"].isin(recente)].groupby("equipamento").apply(disp, include_groups=False)
fila = analitica.fila_manutencao(m3.rename("disponibilidade").reset_index(), eq, cfg)
st.dataframe(fila[["prioridade", "equipamento", "tecnologia", "disponibilidade", "excesso_pp", "clientes_jusante",
                   "valor_rs_ano", "dec_min_ano", "custo_visita_rs"]].head(30)
             .style.format({"disponibilidade": "{:.2%}", "excesso_pp": "{:.2f}", "valor_rs_ano": "R$ {:,.0f}",
                            "dec_min_ano": "{:.4f}"}), hide_index=True)
st.caption("Valor por p.p. de indisponibilidade: projeto automation-impact (Energisa MT, faixa média). "
           "Custos de visita são premissas ilustrativas em config_padrao.yaml.")
