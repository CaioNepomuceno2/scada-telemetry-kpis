import numpy as np
import pandas as pd
import pytest

from telemetry_kpis import analitica, config


def test_cronicos():
    mensal = pd.DataFrame({
        "equipamento": ["A"] * 4 + ["B"] * 4,
        "ano_mes": ["2025-01", "2025-02", "2025-03", "2025-04"] * 2,
        "disponibilidade": [0.90, 0.95, 0.97, 0.99, 0.99, 0.97, 0.99, 0.99],
    })
    c = analitica.cronicos(mensal, meta=0.98, janela_meses=4, meses_abaixo_meta=3).set_index("equipamento")
    assert c.loc["A", "cronico"] and not c.loc["B", "cronico"]


def test_flapping():
    t0 = pd.Timestamp("2025-01-01")
    ini = [t0 + pd.Timedelta(hours=h) for h in range(40)] + [t0 + pd.Timedelta(days=40, hours=h) for h in range(40)]
    a = pd.DataFrame({"equipamento": "A", "ini": ini})
    a["fim"] = a["ini"] + pd.Timedelta(minutes=1)
    b = pd.DataFrame({"equipamento": "B", "ini": [t0], "fim": [t0 + pd.Timedelta(hours=5)]})
    f = analitica.flapping(pd.concat([a, b]), falhas_mes_min=30, duracao_mediana_max_min=5, meses_min=2)
    f = f.set_index("equipamento")
    assert f.loc["A", "flapping"] and not f.loc["B", "flapping"]


def test_pareto():
    base = pd.DataFrame({"horas_indisp": [90] + [1] * 10 + [0] * 89})
    p = analitica.pareto(base, fracoes=(0.01, 0.10))
    assert p["top_1pct"] == pytest.approx(0.9) and p["top_10pct"] == pytest.approx(0.99)


def test_ewma_detecta_degradacao_e_poupa_os_estaveis():
    rng = np.random.default_rng(0)
    n, sem = 200, 52
    x = rng.poisson(0.5, (n, sem)).astype(float)
    x[0, 30:] = rng.poisson(3.0, sem - 30)  # degrada a partir da semana 30
    semanal = pd.DataFrame(x, index=[f"E{i}" for i in range(n)])
    r = analitica.ewma_poisson(semanal, lam=0.2, L=4.5, semanas_base=12).set_index("equipamento")
    assert 30 <= r.loc["E0", "semana_primeiro_alarme"] <= 34
    assert r["semana_primeiro_alarme"].drop("E0").notna().mean() < 0.05


def test_fila_ordena_por_valor_por_real():
    cfg = config.carregar()["valor"]
    eq = pd.DataFrame({"equipamento": ["A", "B", "C"], "tecnologia": ["radio", "radio", "fibra"],
                       "clientes_jusante": [1000, 1000, 4000], "ano_instalacao": [2010, 2000, 2020]})
    disp = pd.DataFrame({"equipamento": ["A", "B", "C"], "disponibilidade": [0.90, 0.995, 0.95]})
    f = analitica.fila_manutencao(disp, eq, cfg)
    assert f["equipamento"].tolist()[0] == "C"  # mais clientes compensam a visita mais cara
    b = f.set_index("equipamento").loc["B"]
    assert b["excesso_pp"] == 0 and b["valor_rs_ano"] == 0
    a = f.set_index("equipamento").loc["A"]
    v = analitica.valor_por_pp(cfg)
    peso = 1000 / eq["clientes_jusante"].mean()
    assert a["valor_rs_ano"] == pytest.approx(9 * v["rs_social"] * peso * cfg["prob_sucesso_reparo"])


def test_comparar_estrategias():
    cfg = config.carregar()["valor"]
    rng = np.random.default_rng(1)
    n = 300
    eq = pd.DataFrame({"equipamento": [f"E{i}" for i in range(n)], "tecnologia": rng.choice(["radio", "fibra"], n),
                       "clientes_jusante": rng.integers(100, 3000, n), "ano_instalacao": rng.integers(2000, 2025, n)})
    ruim = rng.random(n) < 0.1
    obs = pd.DataFrame({"equipamento": eq["equipamento"], "disponibilidade": np.where(ruim, 0.9, 0.995)})
    fut = obs.copy()
    r = analitica.comparar_estrategias(obs, fut, eq, cfg, n_visitas=(20,), n_sorteios=50)
    g = r.set_index("estrategia")["rs_ano"]
    assert g["valor"] >= g["pior_disponibilidade"] > g["aleatoria"]
