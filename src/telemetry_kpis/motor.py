"""Motor de cálculo dos indicadores de telemetria (DuckDB).

Fluxo:
    m = Motor(cfg)
    m.carregar_log("dados/log")                      # Parquet (particionado ou não) ou DataFrame
    m.carregar_equipamentos(dim)                     # opcional: grade completa e agregações
    m.carregar_manutencao(janelas)                   # opcional: expurgo de manutenção programada
    m.intervalos("2025-01-01", "2026-01-01")         # falhas de comunicação pareadas
    m.diario(); m.mensal()                           # disponibilidade
    m.comandos()                                     # assertividade

As tabelas ficam no DuckDB (``m.con``) e podem ser lidas com ``m.df(nome)`` ou exportadas com
``m.exportar(nome, caminho)``. As regras estão em ``config_padrao.yaml`` e em ``docs/metodologia.md``.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from telemetry_kpis import config as _config

COLUNAS_LOG = ("timestamp", "equipamento", "ponto", "estado")
RESULTADOS = ("SUCESSO", "RETORNO_INESPERADO", "ATRASADO", "SEM_RETORNO", "SEM_COMUNICACAO")


def _ts(x) -> str:
    return pd.Timestamp(x).strftime("%Y-%m-%d %H:%M:%S.%f")


class Motor:
    def __init__(self, cfg: dict | None = None, threads: int | None = None, memoria: str | None = None):
        self.cfg = cfg or _config.carregar()
        _config.validar(self.cfg)
        self.con = duckdb.connect()
        if threads:
            self.con.execute(f"SET threads = {int(threads)}")
        if memoria:
            self.con.execute(f"SET memory_limit = '{memoria}'")
        self.periodo: tuple[str, str] | None = None
        self.stats: dict = {}
        self._tem_equip = False
        self._tem_manut = False

    # ------------------------------------------------------------------ entrada
    def _fonte(self, fonte, nome: str, colunas: dict | None = None):
        """Registra ``fonte`` como view ``nome`` (DataFrame, arquivo, glob ou diretório Parquet)."""
        if isinstance(fonte, pd.DataFrame):
            df = fonte.rename(columns=colunas or {})
            self.con.register(f"{nome}_df", df)
            sql = f"SELECT * FROM {nome}_df"
        else:
            p = Path(fonte)
            if p.is_dir():
                padrao = f"{p.as_posix()}/**/*.parquet"
                sql = f"SELECT * FROM read_parquet('{padrao}', hive_partitioning = true, union_by_name = true)"
            else:
                sql = f"SELECT * FROM read_parquet('{p.as_posix()}')"
            if colunas:
                sel = ", ".join(f'"{orig}" AS {novo}' for orig, novo in colunas.items())
                sql = f"SELECT *, {sel} FROM ({sql})"
        self.con.execute(f"CREATE OR REPLACE VIEW {nome} AS {sql}")

    def carregar_log(self, fonte, colunas: dict | None = None) -> int:
        """Lê o log bruto (timestamp, equipamento, ponto, estado) e normaliza pelo ``mapeamento``.

        ``colunas`` renomeia colunas do arquivo de origem, ex.: ``{"DataHora": "timestamp"}``.
        Linhas cujo (ponto, estado) não está no mapeamento são descartadas e contadas.
        """
        self._fonte(fonte, "log_bruto", colunas)
        campos = ("ponto", "estado", "evento", "familia", "valor", "esperado")
        linhas = [tuple(None if m.get(c) is None else str(m[c]) for c in campos) for m in self.cfg["mapeamento"]]
        self.con.execute("CREATE OR REPLACE TABLE mapa (" + ", ".join(f"{c} VARCHAR" for c in campos) + ")")
        self.con.executemany("INSERT INTO mapa VALUES (?, ?, ?, ?, ?, ?)", linhas)
        ordem_arquivo = not self.cfg["disponibilidade"]["ordenar_por_timestamp"]
        seq = "row_number() OVER ()" if ordem_arquivo else "0"
        self.con.execute(f"""
            CREATE OR REPLACE TABLE eventos AS
            SELECT l.seq, l.ts, l.equipamento, m.evento, m.familia, m.valor, m.esperado
            FROM (SELECT {seq} AS seq, CAST(timestamp AS TIMESTAMP) AS ts,
                         CAST(equipamento AS VARCHAR) AS equipamento,
                         CAST(ponto AS VARCHAR) AS ponto, CAST(estado AS VARCHAR) AS estado
                  FROM log_bruto) l
            JOIN mapa m ON l.ponto = m.ponto AND l.estado = m.estado
        """)
        n_log = self.con.execute("SELECT count(*) FROM log_bruto").fetchall()[0][0]
        n_ev = self.con.execute("SELECT count(*) FROM eventos").fetchall()[0][0]
        self.stats.update(linhas_log=n_log, eventos_mapeados=n_ev, linhas_nao_mapeadas=n_log - n_ev)
        return n_ev

    def carregar_equipamentos(self, fonte) -> None:
        """Dimensão de equipamentos: ``equipamento`` + atributos (subestacao, regiao, tipo, ...)."""
        self._fonte(fonte, "dim_equipamento_v")
        self.con.execute("""CREATE OR REPLACE TABLE dim_equipamento AS
                            SELECT * REPLACE (CAST(equipamento AS VARCHAR) AS equipamento)
                            FROM dim_equipamento_v""")
        self._tem_equip = True

    def carregar_manutencao(self, fonte) -> None:
        """Janelas de manutenção programada: ``equipamento, ini, fim`` (expurgadas se configurado)."""
        self._fonte(fonte, "manutencao_v")
        self.con.execute("""CREATE OR REPLACE TABLE manutencao AS
                            SELECT CAST(equipamento AS VARCHAR) AS equipamento,
                                   CAST(ini AS TIMESTAMP) AS ini, CAST(fim AS TIMESTAMP) AS fim
                            FROM manutencao_v WHERE fim > ini""")
        self._tem_manut = True

    # ------------------------------------------------------------------ disponibilidade
    def intervalos(self, inicio, fim) -> pd.DataFrame:
        """Pareia FALHA/NORMAL de comunicação em intervalos de indisponibilidade dentro de [inicio, fim)."""
        r = self.cfg["disponibilidade"]
        p0, p1 = _ts(inicio), _ts(fim)
        self.periodo = (p0, p1)
        ordem = "ts, evento" if r["ordenar_por_timestamp"] else "seq"
        jan_us = float(r["janela_duplicado_s"]) * 1e6
        manter_primeiro = r["inicio_repetido"] == "manter_primeiro"
        filtro_rep = "ev_ant IS NULL OR evento <> ev_ant" if manter_primeiro else "TRUE"
        self.con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _comm AS
            WITH c AS (
                SELECT equipamento, ts, evento, seq FROM eventos
                WHERE evento IN ('COMM_FALHA', 'COMM_NORMAL')
            ),
            d AS (
                SELECT *, lag(evento) OVER w AS ev_ant, lag(ts) OVER w AS ts_ant
                FROM c WINDOW w AS (PARTITION BY equipamento ORDER BY {ordem})
            ),
            e AS (  -- remove duplicados: mesmo evento repetido dentro da janela
                SELECT equipamento, ts, evento, seq FROM d
                WHERE NOT (evento = ev_ant AND abs(epoch_us(ts) - epoch_us(ts_ant)) <= {jan_us})
                   OR ev_ant IS NULL
            ),
            f AS (
                SELECT *, lag(evento) OVER w AS ev_ant
                FROM e WINDOW w AS (PARTITION BY equipamento ORDER BY {ordem})
            )
            SELECT equipamento, ts, evento, seq FROM f WHERE {filtro_rep}
        """)
        n_dup = (self.con.execute("SELECT count(*) FROM eventos WHERE evento IN ('COMM_FALHA','COMM_NORMAL')")
                 .fetchall()[0][0] - self.con.execute("SELECT count(*) FROM _comm").fetchall()[0][0])
        extra_inicio = ""
        if r["fim_sem_inicio"] == "abrir_no_inicio_periodo":
            extra_inicio = f"""
                UNION ALL
                SELECT equipamento, TIMESTAMP '{p0}' AS ini, ini AS fim, 'inicio_periodo' AS fim_tipo
                FROM h WHERE rn = 1 AND evento = 'COMM_NORMAL'"""
        self.con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _int_bruto AS
            WITH h AS (
                SELECT equipamento, ts AS ini, evento,
                       lead(ts) OVER w AS prox_ts, lead(evento) OVER w AS prox_ev,
                       row_number() OVER w AS rn
                FROM _comm WINDOW w AS (PARTITION BY equipamento ORDER BY {ordem})
            )
            SELECT equipamento, ini, coalesce(prox_ts, TIMESTAMP '{p1}') AS fim,
                   CASE WHEN prox_ts IS NULL THEN 'aberto'
                        WHEN prox_ev = 'COMM_FALHA' THEN 'novo_inicio'
                        ELSE 'normal' END AS fim_tipo
            FROM h WHERE evento = 'COMM_FALHA'
            {extra_inicio}
        """)
        if r.get("fim_inferido", "proximo_evento") == "mediana_equipamento":
            # FALHA seguida de outra FALHA: o NORMAL se perdeu. Em vez de estender a falha até o próximo
            # evento (podem ser semanas), usa a duração mediana das falhas bem pareadas do equipamento.
            self.con.execute("""
                CREATE OR REPLACE TEMP TABLE _med AS
                SELECT equipamento, median(epoch_us(fim) - epoch_us(ini)) AS med_us
                FROM _int_bruto WHERE fim_tipo = 'normal' GROUP BY equipamento""")
            glob = self.con.execute("SELECT median(med_us) FROM _med").fetchall()[0][0] or 0
            self.con.execute(f"""
                UPDATE _int_bruto AS b
                   SET fim = least(b.fim, make_timestamp(CAST(epoch_us(b.ini) + coalesce(m.med_us, {glob}) AS BIGINT)))
                  FROM (SELECT DISTINCT equipamento FROM _int_bruto) e
                  LEFT JOIN _med m USING (equipamento)
                 WHERE b.equipamento = e.equipamento AND b.fim_tipo = 'novo_inicio'""")
        conds = ["fim > ini"]
        if r["aberto_no_fim"] == "descartar":
            conds.append("fim_tipo <> 'aberto'")
        fim_expr = "fim"
        if r.get("duracao_maxima_sem_fim_h"):
            cap_us = float(r["duracao_maxima_sem_fim_h"]) * 3600e6
            fim_expr = (f"CASE WHEN fim_tipo IN ('aberto','novo_inicio') AND epoch_us(fim) - epoch_us(ini) > {cap_us}"
                        f" THEN make_timestamp(CAST(epoch_us(ini) + {cap_us} AS BIGINT)) ELSE fim END")
        dmin_us = float(r["duracao_minima_s"]) * 1e6
        self.con.execute(f"""
            CREATE OR REPLACE TABLE intervalos AS
            SELECT equipamento,
                   greatest(ini, TIMESTAMP '{p0}') AS ini,
                   least({fim_expr}, TIMESTAMP '{p1}') AS fim,
                   fim_tipo,
                   ini < TIMESTAMP '{p0}' AS iniciou_antes
            FROM _int_bruto
            WHERE {' AND '.join(conds)}
              AND epoch_us({fim_expr}) - epoch_us(ini) >= {dmin_us}
              AND {fim_expr} > TIMESTAMP '{p0}' AND ini < TIMESTAMP '{p1}'
        """)
        self.con.execute("DELETE FROM intervalos WHERE fim <= ini")
        q = self.con.execute("""SELECT count(*), count(*) FILTER (fim_tipo = 'aberto'),
                                       count(*) FILTER (fim_tipo = 'novo_inicio') FROM intervalos""").fetchall()[0]
        self.stats.update(intervalos=q[0], intervalos_abertos=q[1], intervalos_fim_inferido=q[2],
                          eventos_comm_descartados=n_dup)
        return self.df("intervalos") if q[0] < 2_000_000 else None

    def _por_dia(self, tabela: str, saida: str) -> None:
        """Divide intervalos (equipamento, ini, fim) em segundos por dia."""
        self.con.execute(f"""
            CREATE OR REPLACE TEMP TABLE {saida} AS
            SELECT equipamento, CAST(d AS DATE) AS dia,
                   (epoch_us(least(fim, d + INTERVAL 1 DAY)) - epoch_us(greatest(ini, d))) / 1e6 AS seg
            FROM (SELECT equipamento, ini, fim,
                         unnest(generate_series(CAST(date_trunc('day', ini) AS TIMESTAMP),
                                                CAST(date_trunc('day', fim - INTERVAL 1 MICROSECOND) AS TIMESTAMP),
                                                INTERVAL 1 DAY)) AS d
                  FROM {tabela} WHERE fim > ini)
        """)

    def diario(self) -> None:
        """Tabela ``fato_disponibilidade_diaria``: grade completa equipamento × dia."""
        if self.periodo is None:
            raise RuntimeError("chame intervalos(inicio, fim) antes")
        p0, p1 = self.periodo
        expurgar = self.cfg["disponibilidade"]["expurgar_manutencao"] and self._tem_manut
        self._por_dia("intervalos", "_seg_int")
        if expurgar:
            self.con.execute(f"""
                CREATE OR REPLACE TEMP TABLE _manut_p AS
                SELECT equipamento, greatest(ini, TIMESTAMP '{p0}') AS ini, least(fim, TIMESTAMP '{p1}') AS fim
                FROM manutencao WHERE fim > TIMESTAMP '{p0}' AND ini < TIMESTAMP '{p1}'""")
            self.con.execute("""
                CREATE OR REPLACE TEMP TABLE _ovl AS
                SELECT i.equipamento, greatest(i.ini, m.ini) AS ini, least(i.fim, m.fim) AS fim
                FROM intervalos i JOIN _manut_p m
                  ON i.equipamento = m.equipamento AND i.ini < m.fim AND m.ini < i.fim""")
            self._por_dia("_manut_p", "_seg_manut")
            self._por_dia("_ovl", "_seg_ovl")
        else:
            for t in ("_seg_manut", "_seg_ovl"):
                self.con.execute(f"CREATE OR REPLACE TEMP TABLE {t} (equipamento VARCHAR, dia DATE, seg DOUBLE)")
        equip = ("SELECT equipamento FROM dim_equipamento" if self._tem_equip
                 else "SELECT DISTINCT equipamento FROM eventos")
        self.con.execute(f"""
            CREATE OR REPLACE TABLE fato_disponibilidade_diaria AS
            WITH dias AS (
                SELECT CAST(d AS DATE) AS dia,
                       (epoch_us(least(TIMESTAMP '{p1}', d + INTERVAL 1 DAY))
                        - epoch_us(greatest(TIMESTAMP '{p0}', d))) / 1e6 AS seg_periodo
                FROM (SELECT unnest(generate_series(CAST(date_trunc('day', TIMESTAMP '{p0}') AS TIMESTAMP),
                                                    CAST(date_trunc('day', TIMESTAMP '{p1}' - INTERVAL 1 MICROSECOND)
                                                         AS TIMESTAMP),
                                                    INTERVAL 1 DAY)) AS d)
            ),
            grade AS (SELECT e.equipamento, d.dia, d.seg_periodo FROM ({equip}) e CROSS JOIN dias d),
            si AS (SELECT equipamento, dia, sum(seg) AS seg FROM _seg_int GROUP BY ALL),
            sm AS (SELECT equipamento, dia, sum(seg) AS seg FROM _seg_manut GROUP BY ALL),
            so AS (SELECT equipamento, dia, sum(seg) AS seg FROM _seg_ovl GROUP BY ALL),
            nf AS (SELECT equipamento, CAST(ini AS DATE) AS dia, count(*) AS n
                   FROM intervalos WHERE fim_tipo <> 'inicio_periodo' AND NOT iniciou_antes GROUP BY ALL)
            SELECT g.equipamento, g.dia, g.seg_periodo,
                   coalesce(sm.seg, 0) AS seg_manutencao,
                   coalesce(si.seg, 0) AS seg_indisp_bruto,
                   greatest(coalesce(si.seg, 0) - coalesce(so.seg, 0), 0) AS seg_indisp,
                   CAST(coalesce(nf.n, 0) AS INTEGER) AS n_falhas
            FROM grade g
            LEFT JOIN si USING (equipamento, dia)
            LEFT JOIN sm USING (equipamento, dia)
            LEFT JOIN so USING (equipamento, dia)
            LEFT JOIN nf USING (equipamento, dia)
        """)
        self.con.execute("""
            ALTER TABLE fato_disponibilidade_diaria ADD COLUMN disponibilidade DOUBLE;
            UPDATE fato_disponibilidade_diaria
               SET disponibilidade = CASE WHEN seg_periodo - seg_manutencao > 0
                                          THEN 1 - seg_indisp / (seg_periodo - seg_manutencao) END;
        """)

    def mensal(self) -> pd.DataFrame:
        """Tabela ``fato_disponibilidade_mensal`` (equipamento × mês), a partir da diária."""
        self.con.execute("""
            CREATE OR REPLACE TABLE fato_disponibilidade_mensal AS
            WITH dur AS (
                SELECT equipamento, strftime(ini, '%Y-%m') AS ano_mes,
                       median((epoch_us(fim) - epoch_us(ini)) / 6e7) AS duracao_mediana_min,
                       count(*) FILTER (fim_tipo = 'aberto') AS n_abertos,
                       count(*) FILTER (fim_tipo = 'novo_inicio') AS n_fim_inferido
                FROM intervalos GROUP BY ALL
            )
            SELECT d.equipamento, strftime(d.dia, '%Y-%m') AS ano_mes,
                   sum(seg_periodo) AS seg_periodo, sum(seg_manutencao) AS seg_manutencao,
                   sum(seg_indisp) AS seg_indisp, sum(n_falhas) AS n_falhas,
                   1 - sum(seg_indisp) / nullif(sum(seg_periodo) - sum(seg_manutencao), 0) AS disponibilidade,
                   any_value(dur.duracao_mediana_min) AS duracao_mediana_min,
                   coalesce(any_value(dur.n_abertos), 0) AS n_abertos,
                   coalesce(any_value(dur.n_fim_inferido), 0) AS n_fim_inferido
            FROM fato_disponibilidade_diaria d
            LEFT JOIN dur ON d.equipamento = dur.equipamento AND strftime(d.dia, '%Y-%m') = dur.ano_mes
            GROUP BY d.equipamento, strftime(d.dia, '%Y-%m')
        """)
        return self.df("fato_disponibilidade_mensal")

    def disponibilidade_agregada(self, por: str | list[str] = "equipamento", mensal: bool = False) -> pd.DataFrame:
        """Disponibilidade ponderada pelo tempo, agregada por colunas da dimensão (ex.: 'subestacao')."""
        por = [por] if isinstance(por, str) else list(por)
        if mensal:
            por = por + ["ano_mes"]
        cols = ", ".join(por)
        join = "JOIN dim_equipamento USING (equipamento)" if self._tem_equip and por != ["equipamento"] else ""
        base = ("SELECT *, strftime(dia, '%Y-%m') AS ano_mes FROM fato_disponibilidade_diaria")
        return self.con.execute(f"""
            SELECT {cols}, count(DISTINCT equipamento) AS n_equipamentos,
                   sum(seg_indisp) / 3600 AS horas_indisp, sum(n_falhas) AS n_falhas,
                   1 - sum(seg_indisp) / nullif(sum(seg_periodo) - sum(seg_manutencao), 0) AS disponibilidade
            FROM ({base}) f {join}
            GROUP BY {cols} ORDER BY {cols}
        """).df()

    # ------------------------------------------------------------------ assertividade
    def comandos(self) -> pd.DataFrame:
        """Tabela ``fato_comando``: uma linha por tentativa de comando, com resultado e tempo de resposta."""
        a = self.cfg["assertividade"]
        lim, busca = float(a["tempo_limite_s"]), float(a["janela_busca_s"])
        jan_us = float(a["janela_repeticao_s"]) * 1e6
        tol_us = float(a.get("tolerancia_relogio_s", 2.0)) * 1e6
        if a["repetidos"] == "agrupar":
            novo = f"pv IS NULL OR valor <> pv OR epoch_us(ts) - epoch_us(pt) > {jan_us}"
        else:
            novo = "TRUE"
        self.con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _tent AS
            WITH c AS (
                SELECT equipamento, familia, ts, valor, esperado,
                       lag(valor) OVER w AS pv, lag(ts) OVER w AS pt
                FROM eventos WHERE evento = 'COMANDO'
                WINDOW w AS (PARTITION BY equipamento, familia ORDER BY ts)
            ),
            g AS (
                SELECT *, sum(CASE WHEN {novo} THEN 1 ELSE 0 END)
                          OVER (PARTITION BY equipamento, familia ORDER BY ts ROWS UNBOUNDED PRECEDING) AS gid
                FROM c
            )
            SELECT equipamento, familia, gid, min(ts) AS ts, arg_min(valor, ts) AS valor,
                   arg_min(esperado, ts) AS esperado, count(*) AS n_tentativas,
                   make_timestamp(CAST(epoch_us(min(ts)) - {tol_us} AS BIGINT)) AS ts_busca
            FROM g GROUP BY equipamento, familia, gid
        """)
        self.con.execute("""
            CREATE OR REPLACE TEMP TABLE _st AS
            SELECT equipamento, familia, ts, valor FROM eventos WHERE evento = 'ESTADO'
        """)
        self.con.execute("""
            CREATE OR REPLACE TEMP TABLE _a1 AS
            SELECT t.*, s.ts AS ts_prim, s.valor AS valor_prim
            FROM _tent t ASOF LEFT JOIN _st s
              ON t.equipamento = s.equipamento AND t.familia = s.familia AND s.ts >= t.ts_busca
        """)
        self.con.execute("""
            CREATE OR REPLACE TEMP TABLE _a2 AS
            SELECT a.*, s.ts AS ts_esp
            FROM _a1 a ASOF LEFT JOIN _st s
              ON a.equipamento = s.equipamento AND a.familia = s.familia AND a.esperado = s.valor
             AND s.ts >= a.ts_busca
        """)
        tem_int = self.con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'intervalos'").fetchall()[0][0] > 0
        em_falha = ("EXISTS (SELECT 1 FROM intervalos i WHERE i.equipamento = a.equipamento "
                    "AND a.ts >= i.ini AND a.ts < i.fim)") if tem_int else "FALSE"
        self.con.execute(f"""
            CREATE OR REPLACE TABLE fato_comando AS
            WITH b AS (
                SELECT a.*, (epoch_us(ts_esp) - epoch_us(ts)) / 1e6 AS t_esp,
                       (epoch_us(ts_prim) - epoch_us(ts)) / 1e6 AS t_prim,
                       {em_falha} AS em_falha_comunicacao
                FROM _a2 a
            ),
            c AS (
                SELECT *, CASE WHEN t_esp <= {lim} THEN 'SUCESSO'
                               WHEN t_prim <= {lim} THEN 'RETORNO_INESPERADO'
                               WHEN t_esp <= {busca} THEN 'ATRASADO'
                               ELSE 'SEM_RETORNO' END AS r0
                FROM b
            )
            SELECT equipamento, familia, ts, valor, esperado, n_tentativas,
                   CASE WHEN r0 <> 'SUCESSO' AND em_falha_comunicacao THEN 'SEM_COMUNICACAO' ELSE r0 END
                       AS resultado,
                   CASE WHEN r0 IN ('SUCESSO', 'ATRASADO') THEN t_esp END AS tempo_resposta_s,
                   em_falha_comunicacao
            FROM c
        """)
        q = self.con.execute("SELECT count(*), sum(n_tentativas) FROM fato_comando").fetchall()[0]
        self.stats.update(tentativas_comando=q[0], comandos_brutos=int(q[1] or 0))
        return self.df("fato_comando") if q[0] < 2_000_000 else None

    def assertividade_agregada(self, por: str | list[str] = "equipamento") -> pd.DataFrame:
        """Taxas por grupo. ``assertividade`` = SUCESSO / tentativas; ``assertividade_com_comunicacao``
        exclui as tentativas feitas com o canal em falha (atribuíveis à telecomunicação, não ao equipamento)."""
        por = [por] if isinstance(por, str) else list(por)
        cols = ", ".join(por)
        dim_cols = set(por) - {"equipamento", "familia", "valor", "resultado"}
        join = "JOIN dim_equipamento USING (equipamento)" if self._tem_equip and dim_cols else ""
        return self.con.execute(f"""
            SELECT {cols}, count(*) AS tentativas,
                   avg((resultado = 'SUCESSO')::INT) AS assertividade,
                   sum((resultado = 'SUCESSO')::INT) / nullif(sum((resultado <> 'SEM_COMUNICACAO')::INT), 0)
                       AS assertividade_com_comunicacao,
                   avg((resultado = 'SEM_COMUNICACAO')::INT) AS frac_sem_comunicacao,
                   quantile_cont(tempo_resposta_s, 0.5) FILTER (resultado = 'SUCESSO') AS tempo_p50_s,
                   quantile_cont(tempo_resposta_s, 0.95) FILTER (resultado = 'SUCESSO') AS tempo_p95_s
            FROM fato_comando f {join}
            GROUP BY {cols} ORDER BY {cols}
        """).df()

    # ------------------------------------------------------------------ utilitários
    def df(self, tabela: str) -> pd.DataFrame:
        return self.con.execute(f"SELECT * FROM {tabela}").df()

    def exportar(self, tabela: str, caminho: str | Path) -> Path:
        caminho = Path(caminho)
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self.con.execute(f"COPY {tabela} TO '{caminho.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
        return caminho
