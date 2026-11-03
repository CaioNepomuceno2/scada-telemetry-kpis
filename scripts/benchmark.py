"""Benchmark de escala: gera um log sintético grande em Parquet particionado e mede o motor.

Uso: python scripts/benchmark.py --equipamentos 40000 --threads 4 [--pasta /tmp/bench]
Grava reports/benchmark.json. Os dados gerados são apagados no fim (use --manter para guardar).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

from telemetry_kpis import gerador

RAIZ = Path(__file__).resolve().parents[1]


def pico_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equipamentos", type=int, default=40000)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--memoria", default=None, help="limite de memória do DuckDB, ex.: 4GB")
    ap.add_argument("--pasta", default=str(RAIZ / "data" / "benchmark"))
    ap.add_argument("--manter", action="store_true")
    a = ap.parse_args()
    pasta = Path(a.pasta)
    shutil.rmtree(pasta, ignore_errors=True)
    t = time.perf_counter()
    s = gerador.gerar(gerador.Parametros(n_equipamentos=a.equipamentos, seed=1))
    t_ger = time.perf_counter() - t
    n_ev = len(s.log)
    t = time.perf_counter()
    gerador.salvar_log_particionado(s.log, pasta / "log")
    s.equipamentos.to_parquet(pasta / "dim_equipamento.parquet", index=False)
    s.manutencao.to_parquet(pasta / "manutencao.parquet", index=False)
    t_grava = time.perf_counter() - t
    tam_mb = sum(f.stat().st_size for f in (pasta / "log").rglob("*.parquet")) / 1e6
    rss_gerador = pico_rss_mb()
    del s

    # o motor roda num processo novo para medir a memória dele, não a do gerador
    codigo = f"""
import json, time, resource
from telemetry_kpis import config
from telemetry_kpis.motor import Motor
t = {{}}
m = Motor(config.carregar(), threads={a.threads}, memoria={a.memoria!r})
x = time.perf_counter(); m.carregar_log({str(pasta / 'log')!r}); t['carregar_e_normalizar'] = time.perf_counter() - x
m.carregar_equipamentos({str(pasta / 'dim_equipamento.parquet')!r})
m.carregar_manutencao({str(pasta / 'manutencao.parquet')!r})
x = time.perf_counter(); m.intervalos('2025-01-01', '2026-01-01'); t['parear_falhas'] = time.perf_counter() - x
x = time.perf_counter(); m.diario(); t['disponibilidade_diaria'] = time.perf_counter() - x
x = time.perf_counter(); m.mensal(); t['disponibilidade_mensal'] = time.perf_counter() - x
x = time.perf_counter(); m.comandos(); t['assertividade'] = time.perf_counter() - x
t['total_motor'] = sum(t.values())
linhas = m.con.execute('SELECT count(*) FROM fato_disponibilidade_diaria').fetchall()[0][0]
print(json.dumps({{'tempos_s': t, 'stats': m.stats, 'linhas_fato_diario': linhas,
                  'pico_memoria_motor_mb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024}}))
"""
    env = dict(os.environ, OMP_NUM_THREADS=str(a.threads))
    out = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, env=env, check=True)
    r = json.loads(out.stdout.strip().splitlines()[-1])
    res = {
        "equipamentos": a.equipamentos, "eventos_log": n_ev, "parquet_mb": round(tam_mb, 1),
        "segundos_geracao": round(t_ger, 1), "segundos_gravacao_parquet": round(t_grava, 1),
        "pico_memoria_gerador_mb": round(rss_gerador), "threads_duckdb": a.threads,
        "maquina": {"cpus": os.cpu_count(), "sistema": platform.platform(), "python": platform.python_version()},
        **r,
    }
    res["eventos_por_segundo_motor"] = round(n_ev / r["tempos_s"]["total_motor"])
    (RAIZ / "reports" / "benchmark.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    if not a.manter:
        shutil.rmtree(pasta, ignore_errors=True)


if __name__ == "__main__":
    main()
