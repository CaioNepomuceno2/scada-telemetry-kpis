"""Analítica de gestão de ativos sobre os indicadores de telemetria.

* ``cronicos``: equipamentos abaixo da meta em vários meses da janela recente;
* ``flapping``: muitas quedas curtas no mês (oscilação do canal);
* ``pareto``: quanto da indisponibilidade está concentrada em poucos equipamentos;
* ``ewma_poisson``: alarme de degradação pela contagem semanal de falhas (carta EWMA de Poisson);
* ``fila_manutencao``: prioriza visitas pelo valor (R$ e minutos de DEC) por real gasto;
* ``comparar_estrategias``: avalia a fila contra regras simples, com o resultado medido no período seguinte.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------- reincidência
def cronicos(mensal: pd.DataFrame, meta: float = 0.98, janela_meses: int = 4,
             meses_abaixo_meta: int = 3) -> pd.DataFrame:
    """Crônico = disponibilidade abaixo da ``meta`` em pelo menos ``meses_abaixo_meta`` dos últimos
    ``janela_meses`` meses. ``mensal`` precisa de ``equipamento, ano_mes, disponibilidade``."""
    meses = sorted(mensal["ano_mes"].unique())[-janela_meses:]
    m = mensal[mensal["ano_mes"].isin(meses)]
    g = m.groupby("equipamento").agg(
        meses_avaliados=("ano_mes", "nunique"),
        meses_abaixo=("disponibilidade", lambda s: int((s < meta).sum())),
        disponibilidade_media=("disponibilidade", "mean"),
    ).reset_index()
    g["cronico"] = g["meses_abaixo"] >= meses_abaixo_meta
    return g.sort_values(["cronico", "disponibilidade_media"], ascending=[False, True], ignore_index=True)


def flapping(intervalos: pd.DataFrame, falhas_mes_min: int = 30, duracao_mediana_max_min: float = 5.0,
             meses_min: int = 2) -> pd.DataFrame:
    """Flapping = em ``meses_min`` meses ou mais, ``falhas_mes_min`` quedas ou mais com mediana curta."""
    i = intervalos.assign(ano_mes=intervalos["ini"].dt.strftime("%Y-%m"),
                          dur_min=(intervalos["fim"] - intervalos["ini"]).dt.total_seconds() / 60)
    mes = i.groupby(["equipamento", "ano_mes"]).agg(n=("dur_min", "size"), med=("dur_min", "median")).reset_index()
    mes["flap"] = (mes["n"] >= falhas_mes_min) & (mes["med"] <= duracao_mediana_max_min)
    g = mes.groupby("equipamento").agg(meses_flapping=("flap", "sum"), falhas_mes_max=("n", "max")).reset_index()
    g["flapping"] = g["meses_flapping"] >= meses_min
    return g.sort_values("meses_flapping", ascending=False, ignore_index=True)


def pareto(base: pd.DataFrame, valor: str = "horas_indisp", fracoes=(0.01, 0.05, 0.10, 0.20)) -> dict:
    """Fração do total de ``valor`` explicada pelos ``f`` maiores itens (ex.: 10% dos equipamentos)."""
    v = np.sort(base[valor].fillna(0).to_numpy())[::-1]
    tot = v.sum()
    out = {}
    for f in fracoes:
        k = max(1, int(round(len(v) * f)))
        out[f"top_{int(f * 100)}pct"] = float(v[:k].sum() / tot) if tot else np.nan
    return out


# ---------------------------------------------------------------------- degradação
def contagem_semanal(intervalos: pd.DataFrame, equipamentos: pd.Series, inicio, fim) -> pd.DataFrame:
    """Matriz equipamento × semana com o número de falhas iniciadas (semanas a partir de ``inicio``)."""
    t0 = pd.Timestamp(inicio)
    n_sem = int(np.ceil((pd.Timestamp(fim) - t0).days / 7))
    sem = ((intervalos["ini"] - t0).dt.days // 7).clip(0, n_sem - 1)
    tab = (pd.crosstab(intervalos["equipamento"], sem)
           .reindex(index=equipamentos, columns=range(n_sem), fill_value=0))
    return tab


def ewma_poisson(semanal: pd.DataFrame, grupo: pd.Series | None = None, lam: float = 0.2, L: float = 3.5,
                 semanas_base: int = 12, peso_prior_semanas: float = 8, piso_taxa: float = 0.2) -> pd.DataFrame:
    """Carta EWMA para contagens de Poisson, uma por equipamento.

    A taxa-base de cada equipamento é a média das ``semanas_base`` primeiras semanas, encolhida para a
    média do seu ``grupo`` (ex.: tecnologia) com peso equivalente a ``peso_prior_semanas`` semanas.
    Limite: ``mu + L * sqrt(mu * lam / (2 - lam))``. Devolve a primeira semana em alarme depois da base.
    """
    x = semanal.to_numpy(dtype=float)
    base = x[:, :semanas_base]
    media_eq = base.mean(axis=1)
    if grupo is not None:
        g = pd.Series(media_eq, index=semanal.index).groupby(grupo.reindex(semanal.index).to_numpy()).transform("mean")
        prior = g.to_numpy()
    else:
        prior = np.full(len(x), media_eq.mean())
    mu = (media_eq * semanas_base + prior * peso_prior_semanas) / (semanas_base + peso_prior_semanas)
    mu = np.maximum(mu, piso_taxa)
    ucl = mu + L * np.sqrt(mu * lam / (2 - lam))
    z = mu.copy()
    primeira = np.full(len(x), -1)
    n_alarmes = np.zeros(len(x), dtype=int)
    for t in range(semanas_base, x.shape[1]):
        z = lam * x[:, t] + (1 - lam) * z
        alarme = z > ucl
        n_alarmes += alarme
        primeira = np.where((primeira < 0) & alarme, t, primeira)
    return pd.DataFrame({"equipamento": semanal.index, "taxa_base_semana": mu, "limite": ucl,
                         "semana_primeiro_alarme": np.where(primeira >= 0, primeira, np.nan),
                         "semanas_em_alarme": n_alarmes})


# ---------------------------------------------------------------------- fila por valor
def valor_por_pp(cfg_valor: dict, cenario: str = "medio") -> dict:
    """R$/ano e minutos de DEC/ano por 1 p.p. de indisponibilidade de UM equipamento médio."""
    n = cfg_valor["frota_referencia"]

    def pick(par):
        lo, hi = par
        return {"baixo": lo, "alto": hi, "medio": (lo + hi) / 2}[cenario] / n

    return {"rs_social": pick(cfg_valor["rs_ano_por_pp_frota_social"]),
            "rs_compensacao": pick(cfg_valor["rs_ano_por_pp_frota_compensacao"]),
            "dec_min": pick(cfg_valor["dec_min_ano_por_pp_frota"])}


def fila_manutencao(disp: pd.DataFrame, equipamentos: pd.DataFrame, cfg_valor: dict,
                    cenario: str = "medio") -> pd.DataFrame:
    """Ordena equipamentos pelo valor esperado de uma visita de manutenção de telecom por R$ gasto.

    ``disp``: ``equipamento, disponibilidade`` (período recente). ``equipamentos``: ``equipamento,
    tecnologia, clientes_jusante``. O valor por p.p. vem do automation-impact (frota de referência) e é
    ponderado pelos clientes a jusante do equipamento relativo à média da frota.
    """
    d = equipamentos.merge(disp[["equipamento", "disponibilidade"]], on="equipamento", how="left")
    d["disponibilidade"] = d["disponibilidade"].fillna(1.0)
    alvo = cfg_valor["disponibilidade_pos_reparo"]
    d["excesso_pp"] = ((alvo - d["disponibilidade"]) * 100).clip(lower=0)
    peso = d["clientes_jusante"] / d["clientes_jusante"].replace(0, np.nan).mean()
    d["peso_clientes"] = peso.fillna(0)
    v = valor_por_pp(cfg_valor, cenario)
    p = cfg_valor["prob_sucesso_reparo"]
    d["valor_rs_ano"] = d["excesso_pp"] * v["rs_social"] * d["peso_clientes"] * p
    d["valor_compensacao_rs_ano"] = d["excesso_pp"] * v["rs_compensacao"] * d["peso_clientes"] * p
    d["dec_min_ano"] = d["excesso_pp"] * v["dec_min"] * d["peso_clientes"] * p
    d["custo_visita_rs"] = d["tecnologia"].map(cfg_valor["custo_visita_rs"]).fillna(
        np.mean(list(cfg_valor["custo_visita_rs"].values())))
    d["valor_por_real"] = d["valor_rs_ano"] / d["custo_visita_rs"]
    d = d.sort_values("valor_por_real", ascending=False, ignore_index=True)
    d["prioridade"] = np.arange(1, len(d) + 1)
    return d


def comparar_estrategias(disp_obs: pd.DataFrame, disp_futura: pd.DataFrame, equipamentos: pd.DataFrame,
                         cfg_valor: dict, n_visitas=(25, 50, 100, 200), n_sorteios: int = 200,
                         seed: int = 0) -> pd.DataFrame:
    """Escolhe N equipamentos com dados do período observado e mede o ganho no período seguinte.

    Estratégias: ``valor`` (fila por R$/R$), ``pior_disponibilidade``, ``mais_antigo`` e ``aleatoria``
    (média de ``n_sorteios``). O ganho de visitar um equipamento é o excesso de indisponibilidade que ele
    teria no período futuro, convertido em R$ e DEC pelos mesmos parâmetros, vezes a probabilidade de
    sucesso do reparo.
    """
    fila = fila_manutencao(disp_obs, equipamentos, cfg_valor)
    fut = fila_manutencao(disp_futura, equipamentos, cfg_valor).set_index("equipamento")
    ganho = fut[["valor_rs_ano", "dec_min_ano"]]
    custo = fila.set_index("equipamento")["custo_visita_rs"]
    ordens = {
        "valor": fila["equipamento"].tolist(),
        "pior_disponibilidade": fila.sort_values("disponibilidade")["equipamento"].tolist(),
        "mais_antigo": fila.sort_values("ano_instalacao")["equipamento"].tolist(),
    }
    rng = np.random.default_rng(seed)
    linhas = []
    for n in n_visitas:
        for nome, ordem in ordens.items():
            sel = ordem[:n]
            linhas.append({"estrategia": nome, "visitas": n, "rs_ano": ganho.loc[sel, "valor_rs_ano"].sum(),
                           "dec_min_ano": ganho.loc[sel, "dec_min_ano"].sum(), "custo_rs": custo.loc[sel].sum()})
        eqs = fila["equipamento"].to_numpy()
        sims = [rng.choice(eqs, n, replace=False) for _ in range(n_sorteios)]
        linhas.append({"estrategia": "aleatoria", "visitas": n,
                       "rs_ano": np.mean([ganho.loc[s, "valor_rs_ano"].sum() for s in sims]),
                       "dec_min_ano": np.mean([ganho.loc[s, "dec_min_ano"].sum() for s in sims]),
                       "custo_rs": np.mean([custo.loc[s].sum() for s in sims])})
    out = pd.DataFrame(linhas)
    out["rs_por_real"] = out["rs_ano"] / out["custo_rs"]
    return out
