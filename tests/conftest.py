import pandas as pd
import pytest

from telemetry_kpis import config
from telemetry_kpis.motor import Motor

INI, FIM = "2025-01-01", "2025-03-01"


def log(*linhas):
    """Monta um log bruto a partir de tuplas (timestamp, equipamento, ponto, estado)."""
    return pd.DataFrame(linhas, columns=["timestamp", "equipamento", "ponto", "estado"]).assign(
        timestamp=lambda d: pd.to_datetime(d["timestamp"], format="ISO8601"))


@pytest.fixture
def rodar():
    """Roda disponibilidade (e opcionalmente comandos) e devolve o motor."""

    def _rodar(df, equipamentos=None, manutencao=None, comandos=False, ini=INI, fim=FIM, **cfg):
        m = Motor(config.carregar(**cfg), threads=1)
        m.carregar_log(df)
        if equipamentos is not None:
            m.carregar_equipamentos(pd.DataFrame({"equipamento": equipamentos}))
        if manutencao is not None:
            m.carregar_manutencao(pd.DataFrame(manutencao, columns=["equipamento", "ini", "fim"]).assign(
                ini=lambda d: pd.to_datetime(d["ini"], format="ISO8601"),
                fim=lambda d: pd.to_datetime(d["fim"], format="ISO8601")))
        m.intervalos(ini, fim)
        m.diario()
        m.mensal()
        if comandos:
            m.comandos()
        return m

    return _rodar
