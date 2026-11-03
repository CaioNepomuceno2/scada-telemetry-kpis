"""Estudo completo sobre uma frota sintética: validação, robustez, analítica e fila por valor.

Gera ``reports/*.csv``, ``reports/resultados.json`` e as figuras de ``reports/figures``.
Uso: python scripts/estudo.py [--equipamentos 4000] [--seed 42]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from telemetry_kpis import analitica, config, gerador, powerbi  # noqa: E402
from telemetry_kpis.motor import Motor  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
REP = RAIZ / "reports"
FIG = REP / "figures"
INI, FIM = "2025-01-01", "2026-01-01"
COR = {"azul": "#2a6fdb", "laranja": "#e8743b", "verde": "#19a979", "cinza": "#8a8f98", "vermelho": "#d64545"}


def verdade_indisp(s: gerador.Sintetico, ini, fim) -> pd.DataFrame:
    """Segundos indisponíveis (sem manutenção) e de manutenção por equipamento em [ini, fim)."""
    a, b = pd.Timestamp(ini), pd.Timestamp(fim)

    def clip(d):
        x = d.assign(i=d["ini"].clip(a, b), f=d["fim"].clip(a, b))
        return x.assign(seg=(x["f"] - x["i"]).dt.total_seconds()).groupby("equipamento")["seg"].sum()

    eq = s.equipamentos["equipamento"]
    v = clip(s.verdade_intervalos).reindex(eq).fillna(0)
    mt = clip(s.manutencao).reindex(eq).fillna(0)
    T = (b - a).total_seconds()
    out = pd.DataFrame({"equipamento": eq.to_numpy(), "seg_indisp": (v - mt).to_numpy(), "seg_manut": mt.to_numpy()})
    out["disponibilidade"] = 1 - out["seg_indisp"] / (T - out["seg_manut"])
    return out


def _razao(a: pd.Series, b: pd.Series) -> dict:
    """a / b por linha; None quando b = 0 (a estratégia comparada não recupera nada)."""
    return {int(k): (round(float(a[k] / b[k]), 2) if b[k] > 0 else None) for k in a.index}


def rodar(s, cfg, comandos=True, ini=INI, fim=FIM):
    m = Motor(cfg, threads=2)
    m.carregar_log(s.log)
    m.carregar_equipamentos(s.equipamentos)
    m.carregar_manutencao(s.manutencao)
    m.intervalos(ini, fim)
    m.diario()
    m.mensal()
    if comandos:
        m.comandos()
    return m


def erro_disp(m, verd, T, n_eq):
    calc = m.disponibilidade_agregada("equipamento").set_index("equipamento")["disponibilidade"]
    v = verd.set_index("equipamento")
    e = (calc.reindex(v.index) - v["disponibilidade"]) * 100
    frota_v = 1 - v["seg_indisp"].sum() / (T * n_eq - v["seg_manut"].sum())
    d = m.con.execute("""SELECT 1 - sum(seg_indisp) / (sum(seg_periodo) - sum(seg_manutencao)),
                                sum(n_falhas) FROM fato_disponibilidade_diaria""").fetchall()[0]
    return {"erro_frota_pp": (d[0] - frota_v) * 100, "mae_equipamento_pp": e.abs().mean(),
            "p95_equipamento_pp": e.abs().quantile(0.95), "max_equipamento_pp": e.abs().max(),
            "n_falhas_calc": int(d[1])}, e


def comparar_comandos(m, s):
    calc = m.df("fato_comando").sort_values("ts")
    verd = s.verdade_comandos.sort_values("ts")
    calc["ts"] = calc["ts"].astype("datetime64[us]")
    verd["ts"] = verd["ts"].astype("datetime64[us]")
    mm = pd.merge_asof(calc, verd[["equipamento", "familia", "ts", "resultado"]].rename(
        columns={"resultado": "verdade"}), on="ts", by=["equipamento", "familia"],
        tolerance=pd.Timedelta("2s"), direction="nearest")
    return mm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equipamentos", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    REP.mkdir(exist_ok=True)
    FIG.mkdir(exist_ok=True)
    cfg = config.carregar()
    T = (pd.Timestamp(FIM) - pd.Timestamp(INI)).total_seconds()
    res: dict = {"frota_sintetica": {"equipamentos": a.equipamentos, "periodo": [INI, FIM], "seed": a.seed}}

    # ------------------------------------------------------------------ dados
    t = time.time()
    par = dict(n_equipamentos=a.equipamentos, inicio=INI, fim=FIM, seed=a.seed)
    s = gerador.gerar(gerador.Parametros(**par))
    limpo = gerador.gerar(gerador.Parametros(**par, ruido=gerador.Ruido.nenhum()))
    verd = verdade_indisp(s, INI, FIM)
    res["frota_sintetica"].update(eventos_log=len(s.log), eventos_log_sem_ruido=len(limpo.log),
                                  tentativas_comando_verdade=len(s.verdade_comandos),
                                  perfis=s.equipamentos["perfil"].value_counts().to_dict(),
                                  tipos=s.equipamentos["tipo"].value_counts().to_dict(),
                                  segundos_geracao=round(time.time() - t, 1))
    print("gerado", res["frota_sintetica"])

    # ------------------------------------------------------------------ validação
    m0 = rodar(limpo, cfg)
    v0, _ = erro_disp(m0, verd, T, a.equipamentos)
    cmd0 = comparar_comandos(m0, limpo)
    m = rodar(s, cfg)
    v1, err_eq = erro_disp(m, verd, T, a.equipamentos)
    cmd1 = comparar_comandos(m, s)
    frota_v = 1 - verd["seg_indisp"].sum() / (T * a.equipamentos - verd["seg_manut"].sum())
    res["validacao"] = {
        "disponibilidade_frota_verdade": frota_v,
        "sem_ruido": {**v0, "acerto_comandos": float((cmd0["verdade"] == cmd0["resultado"]).mean())},
        "com_ruido": {**v1, "acerto_comandos": float((cmd1["verdade"] == cmd1["resultado"]).mean())},
        "falhas_verdade": int(len(s.verdade_intervalos)),
        "estatisticas_motor": m.stats,
    }
    conf = pd.crosstab(cmd1["verdade"].fillna("SEM_PAR"), cmd1["resultado"])
    conf.to_csv(REP / "confusao_comandos.csv")
    taxa_v = (s.verdade_comandos["resultado"] == "SUCESSO").mean()
    taxa_c = (m.df("fato_comando")["resultado"] == "SUCESSO").mean()
    res["validacao"]["assertividade_frota_verdade"] = float(taxa_v)
    res["validacao"]["assertividade_frota_calculada"] = float(taxa_c)

    # ------------------------------------------------------------------ robustez das regras
    variantes = {
        "Regras padrão": {},
        "NORMAL perdido: falha vai até o próximo evento": {"disponibilidade": {"fim_inferido": "proximo_evento"}},
        "NORMAL perdido: espera o próximo NORMAL": {"disponibilidade": {"inicio_repetido": "manter_primeiro"}},
        "Sem remover duplicados": {"disponibilidade": {"janela_duplicado_s": 0}},
        "Descarta falhas abertas no fim do período": {"disponibilidade": {"aberto_no_fim": "descartar"}},
        "Sem expurgo de manutenção": {"disponibilidade": {"expurgar_manutencao": False}},
        "Pareia na ordem do arquivo (sem ordenar)": {"disponibilidade": {"ordenar_por_timestamp": False,
                                                                         "fim_inferido": "proximo_evento"}},
    }
    linhas = []
    for nome, kw in variantes.items():
        mv = rodar(s, config.carregar(**kw), comandos=False)
        r, _ = erro_disp(mv, verd, T, a.equipamentos)
        r["erro_n_falhas_pct"] = (r["n_falhas_calc"] / len(s.verdade_intervalos) - 1) * 100
        linhas.append({"variante": nome, **r})
    rob = pd.DataFrame(linhas)
    rob.to_csv(REP / "robustez_disponibilidade.csv", index=False)
    linhas = []
    for nome, kw in {"Regras padrão": {},
                     "Conta cada repetição como um comando": {"assertividade": {"repetidos": "contar_todos"}},
                     "Sem tolerância de relógio": {"assertividade": {"tolerancia_relogio_s": 0}},
                     "Tempo limite de 10 s": {"assertividade": {"tempo_limite_s": 10}}}.items():
        mv = rodar(s, config.carregar(**kw))
        fc = mv.df("fato_comando")
        linhas.append({"variante": nome, "tentativas": len(fc),
                       "assertividade_calculada": (fc["resultado"] == "SUCESSO").mean(),
                       "assertividade_verdade": taxa_v})
    rob_cmd = pd.DataFrame(linhas)
    rob_cmd["erro_pp"] = (rob_cmd["assertividade_calculada"] - rob_cmd["assertividade_verdade"]) * 100
    rob_cmd.to_csv(REP / "robustez_assertividade.csv", index=False)

    # ------------------------------------------------------------------ indicadores
    por_tec = m.disponibilidade_agregada("tecnologia")
    por_tipo = m.disponibilidade_agregada("tipo")
    ass_tec = m.assertividade_agregada("tecnologia")
    por_tec.to_csv(REP / "disponibilidade_por_tecnologia.csv", index=False)
    por_tipo.to_csv(REP / "disponibilidade_por_tipo.csv", index=False)
    ass_tec.to_csv(REP / "assertividade_por_tecnologia.csv", index=False)
    fc = m.df("fato_comando")
    res["indicadores"] = {
        "disponibilidade_frota": float(1 - m.con.execute(
            "SELECT sum(seg_indisp) / (sum(seg_periodo) - sum(seg_manutencao)) FROM fato_disponibilidade_diaria"
        ).fetchall()[0][0]),
        "assertividade_frota": float((fc["resultado"] == "SUCESSO").mean()),
        "assertividade_com_comunicacao": float((fc["resultado"] == "SUCESSO").sum()
                                               / (fc["resultado"] != "SEM_COMUNICACAO").sum()),
        "resultados_comando": fc["resultado"].value_counts(normalize=True).round(4).to_dict(),
        "tempo_resposta_p50_s": float(fc.loc[fc.resultado == "SUCESSO", "tempo_resposta_s"].median()),
        "tempo_resposta_p95_s": float(fc.loc[fc.resultado == "SUCESSO", "tempo_resposta_s"].quantile(0.95)),
    }

    # ------------------------------------------------------------------ analítica
    acfg = cfg["analitica"]
    mensal = m.df("fato_disponibilidade_mensal")
    falhas = m.df("intervalos")
    perfil = s.equipamentos.set_index("equipamento")["perfil"]

    def pr(flag: pd.Series, alvo: str):
        sel = set(flag[flag].index)
        verdade = set(perfil[perfil == alvo].index)
        tp = len(sel & verdade)
        return {"sinalizados": len(sel), "verdadeiros": len(verdade),
                "precisao": tp / len(sel) if sel else np.nan, "recall": tp / len(verdade) if verdade else np.nan}

    cron = analitica.cronicos(mensal, acfg["meta_disponibilidade"], **acfg["cronico"])
    flap = analitica.flapping(falhas, **acfg["flapping"])
    por_eq = mensal.groupby("equipamento")["seg_indisp"].sum().div(3600).rename("horas_indisp").reset_index()
    por_se = (por_eq.merge(s.equipamentos[["equipamento", "subestacao"]]).groupby("subestacao")["horas_indisp"]
              .sum().reset_index())
    res["analitica"] = {
        "cronicos": pr(cron.set_index("equipamento")["cronico"], "problematico"),
        "flapping": pr(flap.set_index("equipamento")["flapping"], "flapping"),
        "pareto_equipamentos": analitica.pareto(por_eq),
        "pareto_subestacoes": analitica.pareto(por_se, fracoes=(0.05, 0.10, 0.20)),
    }
    cron.to_csv(REP / "cronicos.csv", index=False)

    # degradação: EWMA semanal x regra mensal
    eqs = s.equipamentos["equipamento"]
    semanal = analitica.contagem_semanal(falhas, eqs, INI, FIM)
    grupo = s.equipamentos.set_index("equipamento")["tecnologia"]
    ew = analitica.ewma_poisson(semanal, grupo, lam=acfg["ewma"]["lambda"], L=acfg["ewma"]["L"],
                                semanas_base=acfg["ewma"]["semanas_base"],
                                peso_prior_semanas=acfg["ewma"]["peso_prior_semanas"],
                                piso_taxa=acfg["ewma"]["piso_taxa"]).set_index("equipamento")
    t0 = pd.Timestamp(INI)
    dg = s.equipamentos[s.equipamentos.perfil == "degradando"].set_index("equipamento")
    alarme_dia = t0 + pd.to_timedelta((ew["semana_primeiro_alarme"] + 1) * 7, unit="D")
    # regra mensal: primeiro mês (após o início da degradação) com disponibilidade abaixo da meta,
    # conhecida no fim do mês
    mm_ = mensal.assign(fim_mes=pd.to_datetime(mensal["ano_mes"] + "-01") + pd.offsets.MonthBegin(1))
    abaixo = mm_[mm_["disponibilidade"] < acfg["meta_disponibilidade"]]
    linhas = []
    for e, row in dg.iterrows():
        ad = alarme_dia.get(e)
        ad = ad if pd.notna(ad) and row.degradacao_ini <= ad <= row.degradacao_fim else pd.NaT
        mes = abaixo[(abaixo.equipamento == e) & (abaixo.fim_mes > row.degradacao_ini)
                     & (abaixo.fim_mes <= row.degradacao_fim)]["fim_mes"].min()
        linhas.append({"equipamento": e, "ini": row.degradacao_ini, "fim": row.degradacao_fim,
                       "alarme_ewma": ad, "alarme_mensal": mes})
    det = pd.DataFrame(linhas)
    det = det[det["fim"] <= pd.Timestamp(FIM)]
    for k in ("ewma", "mensal"):
        det[f"antecedencia_{k}_dias"] = (det["fim"] - det[f"alarme_{k}"]).dt.days
    det.to_csv(REP / "degradacao_deteccao.csv", index=False)
    normais = s.equipamentos[s.equipamentos.perfil == "normal"]["equipamento"]
    semanas_monit = semanal.shape[1] - acfg["ewma"]["semanas_base"]
    res["analitica"]["degradacao"] = {
        "equipamentos_degradando": int(len(det)),
        "detectados_ewma": float(det["alarme_ewma"].notna().mean()),
        "detectados_regra_mensal": float(det["alarme_mensal"].notna().mean()),
        "antecedencia_mediana_ewma_dias": float(det["antecedencia_ewma_dias"].median()),
        "antecedencia_mediana_mensal_dias": float(det["antecedencia_mensal_dias"].median()),
        "falso_alarme_normais": float(ew.loc[normais, "semana_primeiro_alarme"].notna().mean()),
        "semanas_monitoradas": int(semanas_monit),
    }

    # ------------------------------------------------------------------ fila por valor (backtest)
    vcfg = cfg["valor"]
    corte = "2025-10-01"
    obs = mensal[mensal["ano_mes"] < corte[:7]].groupby("equipamento")[["seg_indisp", "seg_periodo",
                                                                         "seg_manutencao"]].sum()
    disp_obs = (1 - obs["seg_indisp"] / (obs["seg_periodo"] - obs["seg_manutencao"])).rename(
        "disponibilidade").reset_index()
    disp_fut = verdade_indisp(s, corte, FIM)[["equipamento", "disponibilidade"]]
    comp = analitica.comparar_estrategias(disp_obs, disp_fut, s.equipamentos, vcfg)
    comp.to_csv(REP / "fila_estrategias.csv", index=False)
    fila = analitica.fila_manutencao(disp_obs, s.equipamentos, vcfg)
    cols = ["prioridade", "equipamento", "subestacao", "tipo", "tecnologia", "disponibilidade", "excesso_pp",
            "clientes_jusante", "valor_rs_ano", "dec_min_ano", "custo_visita_rs", "valor_por_real"]
    fila[cols].head(200).to_csv(REP / "fila_manutencao_top200.csv", index=False)
    piv = comp.pivot(index="visitas", columns="estrategia", values="rs_ano")
    payback = fila["valor_rs_ano"] / fila["custo_visita_rs"]
    res["fila"] = {
        "corte_observacao": corte, "ganho_medido_em": [corte, FIM],
        "rs_ano_por_estrategia": piv.round(0).to_dict(),
        "valor_vs_aleatoria": _razao(piv["valor"], piv["aleatoria"]),
        "valor_vs_pior_disponibilidade": _razao(piv["valor"], piv["pior_disponibilidade"]),
        "valor_vs_mais_antigo": _razao(piv["valor"], piv["mais_antigo"]),
        "equipamentos_payback_ate_1_ano": int((payback >= 1).sum()),
        "equipamentos_payback_ate_3_anos": int((payback >= 1 / 3).sum()),
        "valor_por_pp_equipamento_medio": analitica.valor_por_pp(vcfg),
    }

    # ------------------------------------------------------------------ amostra Power BI (pequena)
    am = gerador.gerar(gerador.Parametros(n_equipamentos=150, inicio=INI, fim="2025-04-01", seed=a.seed))
    mp = rodar(am, cfg, ini=INI, fim="2025-04-01")
    powerbi.exportar(mp, RAIZ / "exemplos" / "powerbi")

    # ------------------------------------------------------------------ figuras
    figuras(s, m, verd, err_eq, rob, comp, semanal, ew, dg, ass_tec, por_eq)
    (REP / "resultados.json").write_text(json.dumps(res, indent=1, default=lambda o: o.item()
                                                     if hasattr(o, "item") else str(o), ensure_ascii=False))
    print(json.dumps(res, indent=1, default=str, ensure_ascii=False)[:4000])


def _estilo(ax):
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    ax.grid(axis="y", color="#e6e6e6", lw=0.8)
    ax.set_axisbelow(True)


def figuras(s, m, verd, err_eq, rob, comp, semanal, ew, dg, ass_tec, por_eq):
    plt.rcParams.update({"font.size": 10, "axes.titleweight": "bold", "axes.titlesize": 11})
    # 1. validação
    calc = m.disponibilidade_agregada("equipamento").set_index("equipamento")["disponibilidade"]
    v = verd.set_index("equipamento")["disponibilidade"]
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.scatter(v * 100, calc.reindex(v.index) * 100, s=6, alpha=0.5, color=COR["azul"], lw=0)
    lo = min(v.min(), calc.min()) * 100
    ax.plot([lo, 100], [lo, 100], color=COR["cinza"], lw=1, ls="--")
    ax.set_xlabel("Disponibilidade verdadeira (%)")
    ax.set_ylabel("Disponibilidade calculada do log com ruído (%)")
    ax.set_title("O motor recupera a disponibilidade real de cada equipamento")
    ax.text(0.03, 0.95, f"erro médio: {err_eq.abs().mean():.3f} p.p.\np95: {err_eq.abs().quantile(.95):.3f} p.p.",
            transform=ax.transAxes, va="top")
    _estilo(ax)
    fig.tight_layout()
    fig.savefig(FIG / "01_validacao.png", dpi=150)
    plt.close(fig)
    # 2. robustez
    fig, ax = plt.subplots(figsize=(8, 4))
    r = rob.iloc[::-1]
    cores = [COR["verde"] if v == "Regras padrão" else COR["laranja"] for v in r["variante"]]
    ax.barh(r["variante"], r["mae_equipamento_pp"], color=cores)
    for y, val in enumerate(r["mae_equipamento_pp"]):
        ax.text(val, y, f" {val:.3f}", va="center")
    ax.set_xlabel("Erro médio por equipamento (p.p. de disponibilidade)")
    ax.set_title("Erro do indicador conforme a regra de tratamento do log")
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    fig.tight_layout()
    fig.savefig(FIG / "02_robustez_regras.png", dpi=150)
    plt.close(fig)
    # 3. Pareto
    vals = np.sort(por_eq["horas_indisp"].to_numpy())[::-1]
    cum = np.cumsum(vals) / vals.sum()
    x = np.arange(1, len(vals) + 1) / len(vals) * 100
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(x, cum * 100, color=COR["azul"], lw=2)
    k = int(round(len(vals) * 0.1))
    ax.axvline(10, color=COR["cinza"], ls="--", lw=1)
    ax.annotate(f"10% dos equipamentos = {cum[k - 1] * 100:.0f}% das horas sem comunicação", (10, cum[k - 1] * 100),
                xytext=(18, cum[k - 1] * 100 - 20), arrowprops={"arrowstyle": "->", "color": COR["cinza"]})
    ax.set_xlabel("% dos equipamentos (do pior para o melhor)")
    ax.set_ylabel("% acumulado das horas indisponíveis")
    ax.set_title("A indisponibilidade está concentrada")
    _estilo(ax)
    fig.tight_layout()
    fig.savefig(FIG / "03_pareto.png", dpi=150)
    plt.close(fig)
    # 4. EWMA exemplo
    det = dg.join(ew, how="left").dropna(subset=["semana_primeiro_alarme"])
    if len(det):
        e = det.index[0]
        y = semanal.loc[e].to_numpy(dtype=float)
        lam = 0.2
        z = [ew.loc[e, "taxa_base_semana"]]
        for xi in y:
            z.append(lam * xi + (1 - lam) * z[-1])
        fig, ax = plt.subplots(figsize=(8, 4))
        sem = np.arange(len(y))
        ax.bar(sem, y, color="#c9d6ee", label="falhas na semana")
        ax.plot(sem, z[1:], color=COR["azul"], lw=2, label="EWMA")
        ax.axhline(ew.loc[e, "limite"], color=COR["vermelho"], ls="--", lw=1, label="limite de controle")
        w0 = (dg.loc[e, "degradacao_ini"] - pd.Timestamp(INI)).days / 7
        w1 = (dg.loc[e, "degradacao_fim"] - pd.Timestamp(INI)).days / 7
        ax.axvspan(w0, w1, color="#fbe3d6", alpha=0.6, label="degradação (verdade)")
        ax.axvline(det.loc[e, "semana_primeiro_alarme"], color=COR["vermelho"], lw=2, label="alarme")
        ax.set_xlabel("Semana de 2025")
        ax.set_ylabel("Falhas de comunicação")
        ax.set_title(f"Alarme de degradação antes da visita ({e}, frota sintética)")
        ax.legend(frameon=False, fontsize=8, loc="upper left")
        _estilo(ax)
        fig.tight_layout()
        fig.savefig(FIG / "04_ewma_degradacao.png", dpi=150)
        plt.close(fig)
    # 5. fila
    fig, ax = plt.subplots(figsize=(7, 4))
    nomes = {"valor": r"Fila por valor (R\$ por R\$)", "pior_disponibilidade": "Pior disponibilidade primeiro",
             "mais_antigo": "Mais antigo primeiro", "aleatoria": "Aleatória"}
    cores = {"valor": COR["verde"], "pior_disponibilidade": COR["azul"], "mais_antigo": COR["laranja"],
             "aleatoria": COR["cinza"]}
    for est, g in comp.groupby("estrategia"):
        ax.plot(g["visitas"], g["rs_ano"] / 1000, marker="o", label=nomes[est], color=cores[est], lw=2)
    ax.set_xlabel("Visitas de manutenção de telecom")
    ax.set_ylabel("Valor recuperado no trimestre seguinte\n" + r"(R\$ mil/ano, visão social)")
    ax.set_title("Mesmas visitas, mais valor: priorizar pelo preço da indisponibilidade")
    ax.legend(frameon=False)
    _estilo(ax)
    fig.tight_layout()
    fig.savefig(FIG / "05_fila_estrategias.png", dpi=150)
    plt.close(fig)
    # 6. assertividade por tecnologia
    fc = m.df("fato_comando").merge(s.equipamentos[["equipamento", "tecnologia"]])
    tab = pd.crosstab(fc["tecnologia"], fc["resultado"], normalize="index") * 100
    ordem = [c for c in ["SUCESSO", "ATRASADO", "RETORNO_INESPERADO", "SEM_RETORNO", "SEM_COMUNICACAO"]
             if c in tab]
    cores_r = [COR["verde"], "#f2c14e", COR["laranja"], COR["vermelho"], COR["cinza"]]
    fig, ax = plt.subplots(figsize=(7, 3.5))
    esq = np.zeros(len(tab))
    for c, cor in zip(ordem, cores_r, strict=False):
        ax.barh(tab.index, tab[c], left=esq, color=cor, label=c.replace("_", " ").lower())
        esq += tab[c].to_numpy()
    ax.set_xlabel("% das tentativas de comando")
    ax.set_xlim(0, 100)
    ax.set_title("Resultado dos telecomandos por tecnologia de comunicação")
    ax.legend(frameon=False, fontsize=8, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.25))
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    fig.tight_layout()
    fig.savefig(FIG / "06_assertividade_tecnologia.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()
