"""Gerador sintético de frota e de log SCADA, com a "verdade" conhecida.

Tudo é fictício: subestações ``SE-001``..., regiões ``R1``..., equipamentos ``EQ00001``...
O gerador produz:

* ``equipamentos``: tipo (religador, disjuntor, chave telecomandada, RTU), tecnologia de comunicação
  (fibra, rádio, celular), ano de instalação, clientes a jusante e perfil (normal, problemático,
  flapping, degradando);
* ``log``: eventos brutos (timestamp, equipamento, ponto, estado) com ruído opcional (duplicados,
  perdas, chegada fora de ordem, relógio do equipamento dessincronizado);
* ``verdade_intervalos`` (o que de fato aconteceu com a comunicação), ``manutencao`` (janelas
  programadas) e ``verdade_comandos`` (o resultado real de cada tentativa de comando).

É vetorizado com NumPy para gerar dezenas de milhões de eventos.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

TECNOLOGIAS = ("fibra", "radio", "celular")
# falhas de comunicação por dia e duração mediana (s) por tecnologia
TAXA_DIA = {"fibra": 0.02, "radio": 0.08, "celular": 0.15}
DUR_MEDIANA_S = {"fibra": 1800.0, "radio": 1200.0, "celular": 600.0}
SIGMA_DUR = 1.3
DUR_MAX_S = 20 * 86400.0
# resultado dos comandos com comunicação ok: sucesso, retorno inesperado, atrasado, sem retorno
PROB_CMD = {"fibra": (0.96, 0.01, 0.01, 0.02), "radio": (0.92, 0.02, 0.02, 0.04),
            "celular": (0.88, 0.02, 0.03, 0.07)}
PROB_CMD_PROBLEMATICO = (0.75, 0.03, 0.07, 0.15)
RESULTADOS = ("SUCESSO", "RETORNO_INESPERADO", "ATRASADO", "SEM_RETORNO")


@dataclass
class Ruido:
    duplicados: float = 0.02          # fração de linhas duplicadas (atraso de 0 a 1,5 s)
    perdas: float = 0.003             # fração de linhas perdidas
    atraso_chegada_s: float = 120.0   # jitter de chegada: o arquivo não vem ordenado por timestamp
    relogio_sd_s: float = 1.5         # desvio do relógio dos equipamentos (só eventos de estado)

    @classmethod
    def nenhum(cls) -> Ruido:
        return cls(0.0, 0.0, 0.0, 0.0)


@dataclass
class Parametros:
    n_equipamentos: int = 4000
    inicio: str = "2025-01-01"
    fim: str = "2026-01-01"
    seed: int = 42
    taxa: float = 1.0                 # multiplicador das falhas de comunicação
    taxa_comandos: float = 1.0        # multiplicador dos comandos
    frac_problematico: float = 0.05
    frac_flapping: float = 0.02
    frac_degradando: float = 0.04
    manutencao_por_equip_mes: float = 0.02
    tempo_limite_s: float = 30.0      # define a verdade de SUCESSO x ATRASADO
    ruido: Ruido = field(default_factory=Ruido)


@dataclass
class Sintetico:
    equipamentos: pd.DataFrame
    log: pd.DataFrame
    verdade_intervalos: pd.DataFrame
    manutencao: pd.DataFrame
    verdade_comandos: pd.DataFrame
    parametros: Parametros


def _frota(p: Parametros, rng: np.random.Generator, T_dias: float) -> pd.DataFrame:
    n = p.n_equipamentos
    n_se = max(1, n // 25)
    se = rng.integers(0, n_se, n)
    tipo = rng.choice(["religador", "disjuntor", "chave", "rtu"], size=n, p=[0.70, 0.15, 0.10, 0.05])
    tec = rng.choice(TECNOLOGIAS, size=n, p=[0.15, 0.40, 0.45])
    na_se = np.isin(tipo, ["disjuntor", "rtu"])
    tec = np.where(na_se & (rng.random(n) < 0.5), "fibra", tec)
    ano = rng.integers(2000, 2025, n)
    base_cli = np.exp(rng.normal(6.5, 0.8, n))
    mult_cli = np.select([tipo == "disjuntor", tipo == "chave", tipo == "rtu"], [5.0, 0.6, 0.0], 1.0)
    clientes = np.round(np.where(tipo == "rtu", 3000.0, base_cli * mult_cli)).astype(int)
    perfil = rng.choice(["normal", "problematico", "flapping", "degradando"], size=n,
                        p=[1 - p.frac_problematico - p.frac_flapping - p.frac_degradando,
                           p.frac_problematico, p.frac_flapping, p.frac_degradando])
    t0 = np.where(perfil == "degradando", rng.uniform(120, max(121, T_dias - 100), n), np.nan)
    tfix = t0 + rng.uniform(30, 90, n)
    return pd.DataFrame({
        "equipamento": [f"EQ{i + 1:05d}" for i in range(n)],
        "subestacao": [f"SE-{s + 1:03d}" for s in se],
        "regiao": [f"R{s % 5 + 1}" for s in se],
        "tipo": tipo, "tecnologia": tec, "ano_instalacao": ano, "clientes_jusante": clientes,
        "perfil": perfil, "degradacao_ini_dia": t0, "degradacao_fim_dia": tfix,
    })


def _uniao(eq: np.ndarray, ini: np.ndarray, fim: np.ndarray) -> pd.DataFrame:
    """União de intervalos por equipamento (vetorizada)."""
    df = pd.DataFrame({"eq": eq, "ini": ini, "fim": fim}).sort_values(["eq", "ini"], kind="mergesort")
    cummax = df.groupby("eq", sort=False)["fim"].cummax()
    prev = cummax.groupby(df["eq"], sort=False).shift()
    novo = prev.isna() | (df["ini"] > prev)
    bloco = novo.cumsum()
    return (df.groupby(bloco, sort=False).agg(eq=("eq", "first"), ini=("ini", "min"), fim=("fim", "max"))
            .reset_index(drop=True))


def _dentro(blocos: pd.DataFrame, eq: np.ndarray, t: np.ndarray, T: float) -> np.ndarray:
    """Para cada (eq, t), True se t está dentro de algum intervalo de ``blocos`` do mesmo equipamento."""
    big = T + 10.0
    chave_b = blocos["eq"].to_numpy() * big + blocos["ini"].to_numpy()
    ordem = np.argsort(chave_b, kind="mergesort")
    chave_b = chave_b[ordem]
    eq_b, fim_b = blocos["eq"].to_numpy()[ordem], blocos["fim"].to_numpy()[ordem]
    idx = np.searchsorted(chave_b, eq * big + t, side="right") - 1
    ok = idx >= 0
    res = np.zeros(len(t), dtype=bool)
    res[ok] = (eq_b[idx[ok]] == eq[ok]) & (t[ok] < fim_b[idx[ok]])
    return res


def gerar(p: Parametros | None = None) -> Sintetico:
    p = p or Parametros()
    rng = np.random.default_rng(p.seed)
    t_ini = pd.Timestamp(p.inicio)
    T = (pd.Timestamp(p.fim) - t_ini).total_seconds()
    T_dias = T / 86400
    frota = _frota(p, rng, T_dias)
    n = len(frota)
    tec = frota["tecnologia"].to_numpy()
    perfil = frota["perfil"].to_numpy()
    idade = 2025 - frota["ano_instalacao"].to_numpy()

    # ---------------------------------------------------------------- falhas de comunicação
    taxa = np.array([TAXA_DIA[x] for x in tec]) * (1 + 0.02 * idade) * p.taxa
    taxa = np.where(perfil == "problematico", taxa * 6, taxa)
    med = np.array([DUR_MEDIANA_S[x] for x in tec])
    med = np.where(perfil == "problematico", med * 3, med)
    k = rng.poisson(taxa * T_dias)
    eq = np.repeat(np.arange(n), k)
    ini = rng.uniform(0, T, len(eq))
    dur = np.minimum(np.exp(rng.normal(np.log(med[eq]), SIGMA_DUR)), DUR_MAX_S)
    partes = [(eq, ini, ini + dur)]
    # flapping: muitas quedas curtas
    fl = np.flatnonzero(perfil == "flapping")
    kf = rng.poisson(4.0 * p.taxa * T_dias, len(fl))
    eqf = np.repeat(fl, kf)
    inf = rng.uniform(0, T, len(eqf))
    partes.append((eqf, inf, inf + np.exp(rng.normal(np.log(45.0), 0.5, len(eqf)))))
    # degradação: taxa sobe linearmente até 10x entre t0 e tfix (visita que resolve)
    dg = np.flatnonzero(perfil == "degradando")
    t0 = frota["degradacao_ini_dia"].to_numpy()[dg] * 86400
    tf = np.minimum(frota["degradacao_fim_dia"].to_numpy()[dg] * 86400, T)
    area = taxa[dg] * 9 * np.maximum(tf - t0, 0) / 86400 / 2
    kd = rng.poisson(area)
    eqd = np.repeat(dg, kd)
    D = np.repeat(np.maximum(tf - t0, 0), kd)
    ind = np.repeat(t0, kd) + D * np.sqrt(rng.random(len(eqd)))
    durd = np.minimum(np.exp(rng.normal(np.log(med[eqd]), SIGMA_DUR)), DUR_MAX_S)
    partes.append((eqd, ind, ind + durd))
    # manutenção programada: no máximo uma janela por equipamento-mês, em dia útil, 8h-16h
    meses = pd.date_range(t_ini, pd.Timestamp(p.fim), freq="MS")
    jan = []
    for m0, m1 in zip(meses[:-1], meses[1:], strict=True):
        sel = np.flatnonzero(rng.random(n) < p.manutencao_por_equip_mes)
        if not len(sel):
            continue
        dias = pd.bdate_range(m0, m1 - pd.Timedelta(days=1))
        d = dias[rng.integers(0, len(dias), len(sel))]
        hi = (d - t_ini).total_seconds().to_numpy() + rng.uniform(8, 16, len(sel)) * 3600
        jan.append((sel, hi, hi + rng.uniform(2, 8, len(sel)) * 3600))
    if jan:
        meq, mini, mfim = (np.concatenate(x) for x in zip(*jan, strict=True))
        mfim = np.minimum(mfim, T)
    else:
        meq, mini, mfim = (np.array([], dtype=int), np.array([]), np.array([]))
    partes.append((meq, mini, mfim))
    blocos = _uniao(*(np.concatenate(x) for x in zip(*partes, strict=True)))

    def ts(seg):
        return t_ini + pd.to_timedelta(np.asarray(seg), unit="s")

    nomes = frota["equipamento"].to_numpy()
    ev_comm = pd.DataFrame({
        "t": np.concatenate([blocos["ini"], blocos["fim"][blocos["fim"] < T]]),
        "eq": np.concatenate([blocos["eq"], blocos["eq"][blocos["fim"] < T]]),
        "ponto": "COMUNICACAO",
        "estado": np.r_[np.repeat("FALHA", len(blocos)), np.repeat("NORMAL", int((blocos["fim"] < T).sum()))],
        "dispositivo": False,
    })

    # ---------------------------------------------------------------- comandos
    tipo = frota["tipo"].to_numpy()
    taxa_cmd_mes = np.select([np.isin(tipo, ["religador", "chave"]), tipo == "disjuntor"], [3.0, 1.5], 0.0)
    kc = rng.poisson(taxa_cmd_mes * p.taxa_comandos * T_dias / 30.4)
    ceq = np.repeat(np.arange(n), kc)
    ct = rng.uniform(0, T - 3600, len(ceq))
    fam = np.where(rng.random(len(ceq)) < 0.7, "52", "79")
    abrir = rng.random(len(ceq)) < 0.5
    valor = np.where(fam == "52", np.where(abrir, "ABRIR", "FECHAR"), np.where(abrir, "BLOQUEAR", "DESBLOQUEAR"))
    esperado = np.select([valor == "ABRIR", valor == "FECHAR", valor == "BLOQUEAR"],
                         ["ABERTO", "FECHADO", "BLOQUEADO"], "DESBLOQUEADO")
    oposto = np.select([valor == "ABRIR", valor == "FECHAR", valor == "BLOQUEAR"],
                       ["FECHADO", "ABERTO", "DESBLOQUEADO"], "BLOQUEADO")
    sem_com = _dentro(blocos, ceq, ct, T)
    probs = np.array([PROB_CMD[x] for x in tec[ceq]])
    probs[perfil[ceq] == "problematico"] = PROB_CMD_PROBLEMATICO
    u = rng.random(len(ceq))
    cls = (u[:, None] > np.cumsum(probs, axis=1)).sum(axis=1)
    res = np.array(RESULTADOS)[np.minimum(cls, 3)]
    res = np.where(sem_com, "SEM_COMUNICACAO", res)
    atraso = np.full(len(ceq), np.nan)
    m_suc = res == "SUCESSO"
    atraso[m_suc] = np.minimum(np.exp(rng.normal(np.log(4.0), 0.6, m_suc.sum())), p.tempo_limite_s * 0.9)
    m_atr = res == "ATRASADO"
    atraso[m_atr] = rng.uniform(p.tempo_limite_s + 5, 300, m_atr.sum())
    m_ine = res == "RETORNO_INESPERADO"
    t_ine = rng.uniform(2, 10, m_ine.sum())
    cmd_rows = [pd.DataFrame({"t": ct, "eq": ceq, "ponto": "CMD_" + fam, "estado": valor, "dispositivo": False})]
    resp = m_suc | m_atr
    cmd_rows.append(pd.DataFrame({"t": ct[resp] + atraso[resp], "eq": ceq[resp], "ponto": fam[resp],
                                  "estado": esperado[resp], "dispositivo": True}))
    cmd_rows.append(pd.DataFrame({"t": ct[m_ine] + t_ine, "eq": ceq[m_ine], "ponto": fam[m_ine],
                                  "estado": oposto[m_ine], "dispositivo": True}))
    # operador repete o comando quando não vê retorno
    rep = np.isin(res, ["SEM_RETORNO", "ATRASADO", "SEM_COMUNICACAO"]) & (rng.random(len(ceq)) < 0.4)
    t_rep = ct[rep] + rng.uniform(10, 25, rep.sum())
    cmd_rows.append(pd.DataFrame({"t": t_rep, "eq": ceq[rep], "ponto": "CMD_" + fam[rep], "estado": valor[rep],
                                  "dispositivo": False}))
    verdade_cmd = pd.DataFrame({"equipamento": nomes[ceq], "ts": ts(ct), "familia": fam, "valor": valor,
                                "resultado": res, "atraso_s": atraso, "repetido": rep})

    # ---------------------------------------------------------------- atuações espontâneas da proteção
    prot = np.isin(tipo, ["religador", "disjuntor"])
    ks = rng.poisson(np.where(prot, 2.0, 0.0) * T_dias / 30.4)
    seq_ = np.repeat(np.arange(n), ks)
    st = rng.uniform(0, T - 7200, len(seq_))
    ok = ~_dentro(blocos, seq_, st, T)
    seq_, st = seq_[ok], st[ok]
    religa = rng.random(len(seq_)) < 0.8
    t_fecha = np.where(religa, st + rng.uniform(1, 5, len(seq_)), st + rng.uniform(600, 3600, len(seq_)))
    esp = [pd.DataFrame({"t": st, "eq": seq_, "ponto": "50/51", "estado": "ATUOU", "dispositivo": True}),
           pd.DataFrame({"t": st + 0.1, "eq": seq_, "ponto": "52", "estado": "ABERTO", "dispositivo": True}),
           pd.DataFrame({"t": t_fecha, "eq": seq_, "ponto": "52", "estado": "FECHADO", "dispositivo": True}),
           pd.DataFrame({"t": st[~religa] + 0.5, "eq": seq_[~religa], "ponto": "79", "estado": "BLOQUEADO",
                         "dispositivo": True}),
           pd.DataFrame({"t": t_fecha[~religa] + 1, "eq": seq_[~religa], "ponto": "79", "estado": "DESBLOQUEADO",
                         "dispositivo": True})]

    ev = pd.concat([ev_comm, *cmd_rows, *esp], ignore_index=True)
    ev = ev[ev["t"] < T]
    r = p.ruido
    if r.relogio_sd_s:
        desvio = rng.normal(0, r.relogio_sd_s, n)
        ev.loc[ev["dispositivo"], "t"] += desvio[ev.loc[ev["dispositivo"], "eq"].to_numpy()]
    if r.perdas:
        ev = ev[rng.random(len(ev)) >= r.perdas]
    if r.duplicados:
        dup = ev[rng.random(len(ev)) < r.duplicados].copy()
        dup["t"] += rng.uniform(0, 1.5, len(dup))
        ev = pd.concat([ev, dup], ignore_index=True)
    ev["t"] = ev["t"].clip(0, T - 1e-3)
    chegada = ev["t"].to_numpy() + (rng.uniform(0, r.atraso_chegada_s, len(ev)) if r.atraso_chegada_s else 0)
    ev = ev.iloc[np.argsort(chegada, kind="mergesort")]
    log = pd.DataFrame({
        "timestamp": ts(ev["t"].to_numpy()).astype("datetime64[us]"),
        "equipamento": pd.Categorical(nomes[ev["eq"].to_numpy()]),
        "ponto": pd.Categorical(ev["ponto"].to_numpy()),
        "estado": pd.Categorical(ev["estado"].to_numpy()),
    })

    vint = pd.DataFrame({"equipamento": nomes[blocos["eq"].to_numpy()],
                         "ini": ts(blocos["ini"].to_numpy()), "fim": ts(np.minimum(blocos["fim"].to_numpy(), T))})
    manut = pd.DataFrame({"equipamento": nomes[meq], "ini": ts(mini), "fim": ts(mfim)})
    frota["degradacao_ini"] = t_ini + pd.to_timedelta(frota["degradacao_ini_dia"], unit="D")
    frota["degradacao_fim"] = t_ini + pd.to_timedelta(frota["degradacao_fim_dia"], unit="D")
    frota = frota.drop(columns=["degradacao_ini_dia", "degradacao_fim_dia"])
    return Sintetico(frota, log, vint, manut, verdade_cmd, p)


def salvar_log_particionado(log: pd.DataFrame, destino) -> None:
    """Grava o log em Parquet particionado por mês (``ano_mes=YYYY-MM``)."""
    import pyarrow as pa
    import pyarrow.dataset as ds

    tab = pa.Table.from_pandas(log.assign(ano_mes=log["timestamp"].dt.strftime("%Y-%m")), preserve_index=False)
    ds.write_dataset(tab, str(destino), format="parquet", partitioning=["ano_mes"], partitioning_flavor="hive",
                     existing_data_behavior="delete_matching")
