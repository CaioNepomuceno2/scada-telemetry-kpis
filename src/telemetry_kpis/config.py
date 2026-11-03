"""Leitura da configuração (YAML) com valores padrão."""
from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path

import yaml

OPCOES = {
    ("disponibilidade", "inicio_repetido"): {"fechar_no_novo_inicio", "manter_primeiro"},
    ("disponibilidade", "fim_sem_inicio"): {"ignorar", "abrir_no_inicio_periodo"},
    ("disponibilidade", "aberto_no_fim"): {"fechar_no_fim_periodo", "descartar"},
    ("disponibilidade", "fim_inferido"): {"proximo_evento", "mediana_equipamento"},
    ("assertividade", "repetidos"): {"agrupar", "contar_todos"},
}


def padrao() -> dict:
    texto = resources.files("telemetry_kpis").joinpath("config_padrao.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(texto)


def _mesclar(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _mesclar(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def carregar(caminho: str | Path | None = None, **sobrescrever) -> dict:
    """Configuração padrão, mesclada com um YAML opcional e com sobrescritas por seção.

    Ex.: ``carregar(disponibilidade={"inicio_repetido": "manter_primeiro"})``.
    """
    cfg = padrao()
    if caminho:
        cfg = _mesclar(cfg, yaml.safe_load(Path(caminho).read_text(encoding="utf-8")))
    cfg = _mesclar(cfg, sobrescrever)
    validar(cfg)
    return cfg


def validar(cfg: dict) -> None:
    for (secao, chave), validas in OPCOES.items():
        v = cfg[secao][chave]
        if v not in validas:
            raise ValueError(f"{secao}.{chave} = {v!r}; opções: {sorted(validas)}")
    for item in cfg["mapeamento"]:
        if item["evento"] not in {"COMM_FALHA", "COMM_NORMAL", "COMANDO", "ESTADO"}:
            raise ValueError(f"evento desconhecido no mapeamento: {item}")
        if item["evento"] == "COMANDO" and not item.get("esperado"):
            raise ValueError(f"comando sem 'esperado' no mapeamento: {item}")
