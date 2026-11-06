"""Teste de fumaça: a cadeia inteira da CLI (gerar -> calcular -> analisar -> fila) numa frota sintética minúscula.

Os testes de recuperação cobrem o motor; este cobre as costuras: exportação do modelo estrela, analítica sobre
a saída exportada e fila de manutenção. Tudo é gerado aqui (semente fixa), em disco temporário, sem rede.
"""
import numpy as np
import pandas as pd
import pytest

from telemetry_kpis import analitica, cli, config

INI, FIM = "2025-01-01", "2025-06-01"  # 5 meses: cobre a janela de 4 meses dos crônicos e a de 3 da fila
N_EQUIP = 40
TOP = 15


def _segundos(d: pd.DataFrame) -> pd.Series:
    return (d["fim"] - d["ini"]).dt.total_seconds().groupby(d["equipamento"]).sum()


def test_fumaca_pipeline_gerar_calcular_analisar_fila(tmp_path):
    bruto, saida = tmp_path / "bruto", tmp_path / "saida"
    cli.main(["gerar", "--equipamentos", str(N_EQUIP), "--inicio", INI, "--fim", FIM, "--seed", "7", "--taxa", "3",
              "--sem-ruido", "--saida", str(bruto)])
    cli.main(["calcular", "--log", str(bruto / "log"), "--equipamentos", str(bruto / "dim_equipamento.parquet"),
              "--manutencao", str(bruto / "manutencao.parquet"), "--inicio", INI, "--fim", FIM, "--threads", "1",
              "--saida", str(saida)])
    cli.main(["analisar", "--dados", str(saida)])
    cli.main(["fila", "--dados", str(saida), "--meses", "3", "--top", str(TOP), "--saida", str(saida / "fila.csv")])

    # 1. o modelo estrela inteiro foi exportado e tem linhas
    tabelas = ["dim_equipamento", "dim_subestacao", "dim_tempo", "fato_disponibilidade_diaria",
               "fato_disponibilidade_mensal", "fato_falha_comunicacao", "fato_comando"]
    t = {nome: pd.read_parquet(saida / f"{nome}.parquet") for nome in tabelas}
    for nome, df in t.items():
        assert len(df) > 0, f"{nome} exportada vazia"
    assert t["dim_equipamento"]["equipamento"].nunique() == N_EQUIP

    # 2. disponibilidade: dentro de [0, 1], o mensal fecha com o diário e, sem ruído, bate com a verdade do gerador
    mensal, diaria = t["fato_disponibilidade_mensal"], t["fato_disponibilidade_diaria"]
    assert mensal["disponibilidade"].between(0, 1).all()
    assert (mensal["seg_manutencao"] > 0).any(), "a frota sintética deveria ter janelas de manutenção"
    descontada = 1 - mensal["seg_indisp"] / (mensal["seg_periodo"] - mensal["seg_manutencao"])
    assert np.allclose(mensal["disponibilidade"], descontada)  # a coluna pronta usa o denominador sem manutenção
    soma = ["seg_indisp", "seg_periodo", "seg_manutencao"]
    pd.testing.assert_frame_equal(mensal.groupby("equipamento")[soma].sum().sort_index(),
                                  diaria.groupby("equipamento")[soma].sum().sort_index(), check_exact=False)
    eq = t["dim_equipamento"]["equipamento"]
    verdade = _segundos(pd.read_parquet(bruto / "verdade" / "intervalos.parquet")).reindex(eq).fillna(0)
    manut = _segundos(pd.read_parquet(bruto / "manutencao.parquet")).reindex(eq).fillna(0)
    T = (pd.Timestamp(FIM) - pd.Timestamp(INI)).total_seconds()
    esperado = 1 - (verdade - manut) / (T - manut)
    s = mensal.groupby("equipamento")[soma].sum()
    obtido = (1 - s["seg_indisp"] / (s["seg_periodo"] - s["seg_manutencao"])).reindex(eq)
    assert np.abs(obtido - esperado).max() < 1e-9
    assert (esperado < 1).any(), "a frota sintética deveria ter falhas"

    # 3. assertividade: uma linha por comando gerado e taxas em [0, 1]
    verdade_cmd = pd.read_parquet(bruto / "verdade" / "comandos.parquet")
    assert len(t["fato_comando"]) == len(verdade_cmd) > 0
    r = pd.read_csv(saida / "resumo_assertividade.csv")
    assert r["tentativas"].sum() == len(t["fato_comando"])
    assert r["assertividade"].between(0, 1).all()
    assert r["assertividade_com_comunicacao"].dropna().between(0, 1).all()

    # 4. analítica sobre a saída exportada: crônicos, flapping e Pareto
    cron, flap = pd.read_csv(saida / "cronicos.csv"), pd.read_csv(saida / "flapping.csv")
    assert len(cron) == len(flap) == N_EQUIP
    assert cron["meses_avaliados"].max() <= 4 and (cron["meses_abaixo"] <= cron["meses_avaliados"]).all()
    assert cron.loc[cron["cronico"], "disponibilidade_media"].max() < 0.98  # crônico está abaixo da meta
    por_eq = mensal.groupby("equipamento")["seg_indisp"].sum().div(3600).rename("horas_indisp").reset_index()
    p = analitica.pareto(por_eq)
    assert 0 < p["top_1pct"] <= p["top_5pct"] <= p["top_10pct"] <= p["top_20pct"] <= 1

    # 5. fila de manutenção: ordenada por valor por real, valor >= 0 e zero sem excesso de indisponibilidade
    fila = pd.read_csv(saida / "fila.csv")
    assert fila["prioridade"].tolist() == list(range(1, TOP + 1))
    assert fila["valor_por_real"].is_monotonic_decreasing
    assert (fila["valor_rs_ano"] >= 0).all() and (fila["dec_min_ano"] >= 0).all()
    assert (fila["valor_rs_ano"] > 0).any(), "a fila deveria ter equipamentos com valor"
    assert (fila.loc[fila["excesso_pp"] == 0, "valor_rs_ano"] == 0).all()
    # R$ e DEC saem dos mesmos parâmetros: a razão entre eles é a do preço por p.p.
    v = analitica.valor_por_pp(config.carregar()["valor"])
    com_valor = fila[fila["valor_rs_ano"] > 0]
    assert (com_valor["valor_rs_ano"] / com_valor["dec_min_ano"]).to_numpy() == pytest.approx(
        v["rs_social"] / v["dec_min"])
