"""O motor recupera a verdade do gerador: exata sem ruído e com erro pequeno com ruído."""
import numpy as np
import pandas as pd
import pytest

from telemetry_kpis import config, gerador
from telemetry_kpis.motor import Motor

INI, FIM = "2025-01-01", "2025-07-01"


def verdade_disp(s: gerador.Sintetico) -> pd.Series:
    T = (pd.Timestamp(FIM) - pd.Timestamp(INI)).total_seconds()
    eq = s.equipamentos["equipamento"]
    dur = lambda d: (d["fim"] - d["ini"]).dt.total_seconds()  # noqa: E731
    v = s.verdade_intervalos.assign(d=dur).groupby("equipamento")["d"].sum().reindex(eq).fillna(0)
    mt = s.manutencao.assign(d=dur).groupby("equipamento")["d"].sum().reindex(eq).fillna(0)
    return pd.Series((1 - (v - mt) / (T - mt)).to_numpy(), index=eq.to_numpy())


def calcular(s, **cfg):
    m = Motor(config.carregar(**cfg), threads=1)
    m.carregar_log(s.log)
    m.carregar_equipamentos(s.equipamentos)
    m.carregar_manutencao(s.manutencao)
    m.intervalos(INI, FIM)
    m.diario()
    m.comandos()
    disp = m.disponibilidade_agregada("equipamento").set_index("equipamento")["disponibilidade"]
    return m, disp


@pytest.fixture(scope="module")
def limpo():
    return gerador.gerar(gerador.Parametros(n_equipamentos=250, inicio=INI, fim=FIM, seed=1,
                                            ruido=gerador.Ruido.nenhum()))


@pytest.fixture(scope="module")
def ruidoso():
    return gerador.gerar(gerador.Parametros(n_equipamentos=250, inicio=INI, fim=FIM, seed=2))


def test_sem_ruido_disponibilidade_exata(limpo):
    _, disp = calcular(limpo)
    v = verdade_disp(limpo)
    assert np.abs(disp.reindex(v.index) - v).max() < 1e-9


def test_sem_ruido_comandos_exatos(limpo):
    m, _ = calcular(limpo)
    calc = m.df("fato_comando")["resultado"].value_counts().sort_index()
    verd = limpo.verdade_comandos["resultado"].value_counts().sort_index()
    pd.testing.assert_series_equal(calc, verd, check_names=False)


def test_com_ruido_erro_pequeno(ruidoso):
    _, disp = calcular(ruidoso)
    v = verdade_disp(ruidoso)
    erro_pp = (disp.reindex(v.index) - v) * 100
    assert erro_pp.abs().mean() < 0.05
    assert erro_pp.abs().quantile(0.95) < 0.2


def test_com_ruido_regra_ingenua_erra_mais(ruidoso):
    v = verdade_disp(ruidoso)
    _, bom = calcular(ruidoso)
    _, ruim = calcular(ruidoso, disponibilidade={"fim_inferido": "proximo_evento"})
    e_bom = ((bom.reindex(v.index) - v) * 100).abs().mean()
    e_ruim = ((ruim.reindex(v.index) - v) * 100).abs().mean()
    assert e_ruim > 3 * e_bom


def test_com_ruido_assertividade(ruidoso):
    m, _ = calcular(ruidoso)
    calc = m.df("fato_comando").sort_values("ts")
    verd = ruidoso.verdade_comandos.sort_values("ts")
    calc["ts"] = calc["ts"].astype("datetime64[us]")
    verd["ts"] = verd["ts"].astype("datetime64[us]")
    mm = pd.merge_asof(calc, verd[["equipamento", "familia", "ts", "resultado"]].rename(
        columns={"resultado": "verdade"}), on="ts", by=["equipamento", "familia"],
        tolerance=pd.Timedelta("2s"), direction="nearest")
    assert (mm["verdade"] == mm["resultado"]).mean() > 0.98


def test_gerador_reprodutivel():
    a = gerador.gerar(gerador.Parametros(n_equipamentos=50, inicio=INI, fim="2025-02-01", seed=7))
    b = gerador.gerar(gerador.Parametros(n_equipamentos=50, inicio=INI, fim="2025-02-01", seed=7))
    pd.testing.assert_frame_equal(a.log, b.log)
