"""Linha de comando: ``telemetry-kpis {gerar,calcular,analisar,fila}``."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from telemetry_kpis import analitica, config, gerador, powerbi
from telemetry_kpis.motor import Motor


def _gerar(a):
    ruido = gerador.Ruido.nenhum() if a.sem_ruido else gerador.Ruido()
    s = gerador.gerar(gerador.Parametros(n_equipamentos=a.equipamentos, inicio=a.inicio, fim=a.fim, seed=a.seed,
                                         taxa=a.taxa, ruido=ruido))
    out = Path(a.saida)
    (out / "verdade").mkdir(parents=True, exist_ok=True)
    gerador.salvar_log_particionado(s.log, out / "log")
    s.equipamentos.to_parquet(out / "dim_equipamento.parquet", index=False)
    s.manutencao.to_parquet(out / "manutencao.parquet", index=False)
    s.verdade_intervalos.to_parquet(out / "verdade" / "intervalos.parquet", index=False)
    s.verdade_comandos.to_parquet(out / "verdade" / "comandos.parquet", index=False)
    print(f"{len(s.log):,} eventos, {len(s.equipamentos):,} equipamentos -> {out}")


def _calcular(a):
    cfg = config.carregar(a.config)
    m = Motor(cfg, threads=a.threads)
    m.carregar_log(a.log)
    if a.equipamentos:
        m.carregar_equipamentos(a.equipamentos)
    if a.manutencao:
        m.carregar_manutencao(a.manutencao)
    m.intervalos(a.inicio, a.fim)
    m.diario()
    m.mensal()
    m.comandos()
    out = Path(a.saida)
    arquivos = powerbi.exportar(m, out)
    m.disponibilidade_agregada("equipamento", mensal=True).to_csv(out / "resumo_disponibilidade_mensal.csv",
                                                                  index=False)
    m.assertividade_agregada("equipamento").to_csv(out / "resumo_assertividade.csv", index=False)
    (out / "estatisticas.json").write_text(json.dumps(m.stats, indent=1, default=str))
    print(json.dumps(m.stats, indent=1, default=str))
    print("\n".join(str(p) for p in arquivos.values()))


def _analisar(a):
    cfg = config.carregar(a.config)["analitica"]
    pasta = Path(a.dados)
    mensal = pd.read_parquet(pasta / "fato_disponibilidade_mensal.parquet")
    falhas = pd.read_parquet(pasta / "fato_falha_comunicacao.parquet")
    cron = analitica.cronicos(mensal, cfg["meta_disponibilidade"], **cfg["cronico"])
    flap = analitica.flapping(falhas, **cfg["flapping"])
    cron.to_csv(pasta / "cronicos.csv", index=False)
    flap.to_csv(pasta / "flapping.csv", index=False)
    por_eq = mensal.groupby("equipamento")["seg_indisp"].sum().div(3600).rename("horas_indisp").reset_index()
    print(f"crônicos: {int(cron['cronico'].sum())} | flapping: {int(flap['flapping'].sum())}")
    print("Pareto da indisponibilidade:", analitica.pareto(por_eq))


def _fila(a):
    cfg = config.carregar(a.config)["valor"]
    pasta = Path(a.dados)
    mensal = pd.read_parquet(pasta / "fato_disponibilidade_mensal.parquet")
    meses = sorted(mensal["ano_mes"].unique())[-a.meses:]
    m = mensal[mensal["ano_mes"].isin(meses)].groupby("equipamento")[["seg_indisp", "seg_periodo",
                                                                      "seg_manutencao"]].sum()
    disp = (1 - m["seg_indisp"] / (m["seg_periodo"] - m["seg_manutencao"])).rename("disponibilidade").reset_index()
    eq = pd.read_parquet(pasta / "dim_equipamento.parquet")
    fila = analitica.fila_manutencao(disp, eq, cfg)
    cols = ["prioridade", "equipamento", "tecnologia", "disponibilidade", "excesso_pp", "clientes_jusante",
            "valor_rs_ano", "dec_min_ano", "custo_visita_rs", "valor_por_real"]
    fila[cols].head(a.top).to_csv(a.saida, index=False)
    print(fila[cols].head(min(a.top, 15)).to_string(index=False))


def main(argv=None):
    p = argparse.ArgumentParser(prog="telemetry-kpis", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gerar", help="gera frota e log sintéticos (com a verdade)")
    g.add_argument("--equipamentos", type=int, default=2000)
    g.add_argument("--inicio", default="2025-01-01")
    g.add_argument("--fim", default="2026-01-01")
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--taxa", type=float, default=1.0)
    g.add_argument("--sem-ruido", action="store_true")
    g.add_argument("--saida", required=True)
    g.set_defaults(f=_gerar)

    c = sub.add_parser("calcular", help="disponibilidade + assertividade + modelo estrela")
    c.add_argument("--log", required=True, help="Parquet ou pasta com Parquet (pode ser particionada)")
    c.add_argument("--equipamentos")
    c.add_argument("--manutencao")
    c.add_argument("--inicio", required=True)
    c.add_argument("--fim", required=True)
    c.add_argument("--config")
    c.add_argument("--threads", type=int)
    c.add_argument("--saida", required=True)
    c.set_defaults(f=_calcular)

    n = sub.add_parser("analisar", help="crônicos, flapping e Pareto sobre a saída de 'calcular'")
    n.add_argument("--dados", required=True)
    n.add_argument("--config")
    n.set_defaults(f=_analisar)

    f = sub.add_parser("fila", help="fila de manutenção priorizada por valor")
    f.add_argument("--dados", required=True, help="pasta de saída do 'calcular'")
    f.add_argument("--meses", type=int, default=3)
    f.add_argument("--top", type=int, default=100)
    f.add_argument("--config")
    f.add_argument("--saida", default="fila_manutencao.csv")
    f.set_defaults(f=_fila)

    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
