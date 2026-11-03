"""Exporta o modelo estrela (Parquet) para Power BI ou outra ferramenta de BI.

Tabelas:
* ``dim_equipamento``  (chave: equipamento)
* ``dim_subestacao``   (chave: subestacao)
* ``dim_tempo``        (chave: dia)
* ``fato_disponibilidade_diaria``  (equipamento, dia)
* ``fato_disponibilidade_mensal``  (equipamento, ano_mes)
* ``fato_falha_comunicacao``       (uma linha por falha pareada)
* ``fato_comando``                 (uma linha por tentativa de comando)
"""
from __future__ import annotations

from pathlib import Path

from telemetry_kpis.motor import Motor


def exportar(m: Motor, pasta: str | Path) -> dict[str, Path]:
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    con = m.con
    if not m._tem_equip:
        con.execute("CREATE OR REPLACE TABLE dim_equipamento AS SELECT DISTINCT equipamento FROM eventos")
    cols = {r[0] for r in con.execute("DESCRIBE dim_equipamento").fetchall()}
    if "subestacao" in cols:
        reg = "any_value(regiao) AS regiao," if "regiao" in cols else ""
        tipo = ("count(*) FILTER (tipo = 'religador') AS n_religadores," if "tipo" in cols else "")
        con.execute(f"""CREATE OR REPLACE TABLE dim_subestacao AS
                        SELECT subestacao, {reg} {tipo} count(*) AS n_equipamentos
                        FROM dim_equipamento GROUP BY subestacao""")
    con.execute("""CREATE OR REPLACE TABLE dim_tempo AS
                   SELECT DISTINCT dia, year(dia) AS ano, month(dia) AS mes, strftime(dia, '%Y-%m') AS ano_mes,
                          isodow(dia) AS dia_semana, weekofyear(dia) AS semana_iso
                   FROM fato_disponibilidade_diaria ORDER BY dia""")
    con.execute("""CREATE OR REPLACE TABLE fato_falha_comunicacao AS
                   SELECT equipamento, ini, fim, (epoch_us(fim) - epoch_us(ini)) / 6e7 AS duracao_min, fim_tipo
                   FROM intervalos""")
    tabelas = ["dim_equipamento", "dim_subestacao", "dim_tempo", "fato_disponibilidade_diaria",
               "fato_disponibilidade_mensal", "fato_falha_comunicacao", "fato_comando"]
    existentes = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    return {t: m.exportar(t, pasta / f"{t}.parquet") for t in tabelas if t in existentes}
