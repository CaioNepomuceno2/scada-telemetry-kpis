import pandas as pd
import pytest
from conftest import log

C = "COMUNICACAO"


def seg_mes(m, eq="A"):
    d = m.df("fato_disponibilidade_mensal")
    return d[d.equipamento == eq].set_index("ano_mes")["seg_indisp"].to_dict()


def test_par_simples(rodar):
    m = rodar(log(("2025-01-10 10:00", "A", C, "FALHA"), ("2025-01-10 11:00", "A", C, "NORMAL")))
    assert seg_mes(m)["2025-01"] == pytest.approx(3600)
    dia = m.df("fato_disponibilidade_diaria").query("equipamento == 'A' and n_falhas > 0")
    assert len(dia) == 1 and dia["n_falhas"].iloc[0] == 1


def test_falha_que_cruza_o_mes_e_dividida(rodar):
    m = rodar(log(("2025-01-31 23:00", "A", C, "FALHA"), ("2025-02-01 01:00", "A", C, "NORMAL")))
    s = seg_mes(m)
    assert s["2025-01"] == pytest.approx(3600) and s["2025-02"] == pytest.approx(3600)
    mensal = m.df("fato_disponibilidade_mensal").set_index("ano_mes")
    assert mensal.loc["2025-01", "n_falhas"] == 1 and mensal.loc["2025-02", "n_falhas"] == 0


def test_duplicados_nao_contam(rodar):
    m = rodar(log(("2025-01-10 10:00:00", "A", C, "FALHA"), ("2025-01-10 10:00:01", "A", C, "FALHA"),
                  ("2025-01-10 11:00:00", "A", C, "NORMAL"), ("2025-01-10 11:00:00.5", "A", C, "NORMAL")))
    assert seg_mes(m)["2025-01"] == pytest.approx(3600)
    assert m.df("fato_disponibilidade_mensal")["n_falhas"].sum() == 1


def test_ordem_do_arquivo_nao_importa(rodar):
    linhas = [("2025-01-10 10:00", "A", C, "FALHA"), ("2025-01-10 11:00", "A", C, "NORMAL"),
              ("2025-01-12 08:00", "A", C, "FALHA"), ("2025-01-12 08:30", "A", C, "NORMAL")]
    a = seg_mes(rodar(log(*linhas)))
    b = seg_mes(rodar(log(*reversed(linhas))))
    assert a == b and a["2025-01"] == pytest.approx(5400)


def test_normal_perdido_proximo_evento_x_mediana(rodar):
    linhas = [("2025-01-05 10:00", "A", C, "FALHA"), ("2025-01-05 10:30", "A", C, "NORMAL"),
              ("2025-01-06 10:00", "A", C, "FALHA"), ("2025-01-06 10:30", "A", C, "NORMAL"),
              ("2025-01-10 10:00", "A", C, "FALHA"),  # NORMAL perdido
              ("2025-01-10 20:00", "A", C, "FALHA"), ("2025-01-10 20:30", "A", C, "NORMAL")]
    prox = seg_mes(rodar(log(*linhas), disponibilidade={"fim_inferido": "proximo_evento"}))["2025-01"]
    med = seg_mes(rodar(log(*linhas), disponibilidade={"fim_inferido": "mediana_equipamento"}))["2025-01"]
    assert prox == pytest.approx(3 * 1800 + 10 * 3600)
    assert med == pytest.approx(4 * 1800)


def test_manter_primeiro_espera_o_normal(rodar):
    linhas = [("2025-01-10 10:00", "A", C, "FALHA"), ("2025-01-10 20:00", "A", C, "FALHA"),
              ("2025-01-10 20:30", "A", C, "NORMAL")]
    m = rodar(log(*linhas), disponibilidade={"inicio_repetido": "manter_primeiro"})
    assert seg_mes(m)["2025-01"] == pytest.approx(10.5 * 3600)


def test_falha_aberta_no_fim_do_periodo(rodar):
    linhas = [("2025-02-28 23:00", "A", C, "FALHA")]
    assert seg_mes(rodar(log(*linhas)))["2025-02"] == pytest.approx(3600)
    m = rodar(log(*linhas), disponibilidade={"aberto_no_fim": "descartar"})
    assert seg_mes(m)["2025-02"] == 0


def test_normal_sem_falha_anterior(rodar):
    linhas = [("2025-01-01 02:00", "A", C, "NORMAL")]
    assert seg_mes(rodar(log(*linhas)))["2025-01"] == 0
    m = rodar(log(*linhas), disponibilidade={"fim_sem_inicio": "abrir_no_inicio_periodo"})
    assert seg_mes(m)["2025-01"] == pytest.approx(7200)


def test_falha_iniciada_antes_do_periodo_e_cortada(rodar):
    linhas = [("2024-12-31 22:00", "A", C, "FALHA"), ("2025-01-01 01:00", "A", C, "NORMAL")]
    m = rodar(log(*linhas))
    assert seg_mes(m)["2025-01"] == pytest.approx(3600)
    assert m.df("fato_disponibilidade_mensal")["n_falhas"].sum() == 0  # começou antes: não é falha nova


def test_expurgo_de_manutencao(rodar):
    linhas = [("2025-01-10 10:00", "A", C, "FALHA"), ("2025-01-10 14:00", "A", C, "NORMAL")]
    man = [("A", "2025-01-10 12:00", "2025-01-10 13:00")]
    m = rodar(log(*linhas), manutencao=man)
    dia = pd.Timestamp("2025-01-10")
    t = m.df("fato_disponibilidade_diaria")
    d = t[(t.equipamento == "A") & (t.dia == dia)].iloc[0]
    assert d["seg_indisp"] == pytest.approx(3 * 3600)
    assert d["seg_manutencao"] == pytest.approx(3600)
    assert d["disponibilidade"] == pytest.approx(1 - 3 / 23)
    sem = rodar(log(*linhas), manutencao=man, disponibilidade={"expurgar_manutencao": False})
    t2 = sem.df("fato_disponibilidade_diaria")
    d2 = t2[t2.dia == dia].iloc[0]
    assert d2["seg_indisp"] == pytest.approx(4 * 3600) and d2["seg_manutencao"] == 0


def test_grade_completa_com_equipamento_sem_falha(rodar):
    m = rodar(log(("2025-01-10 10:00", "A", C, "FALHA"), ("2025-01-10 11:00", "A", C, "NORMAL")),
              equipamentos=["A", "B"])
    d = m.df("fato_disponibilidade_mensal")
    assert set(d.equipamento) == {"A", "B"}
    assert (d[d.equipamento == "B"]["disponibilidade"] == 1).all()
    assert len(m.df("fato_disponibilidade_diaria")) == 2 * 59


def test_disponibilidade_ponderada_pelo_tempo(rodar):
    m = rodar(log(("2025-01-01 00:00", "A", C, "FALHA"), ("2025-01-02 00:00", "A", C, "NORMAL")),
              equipamentos=["A", "B"])
    total = m.disponibilidade_agregada(["equipamento"]).set_index("equipamento")["disponibilidade"]
    assert total["A"] == pytest.approx(1 - 1 / 59)
    frota = 1 - (86400) / (2 * 59 * 86400)
    agg = m.con.execute("""SELECT 1 - sum(seg_indisp) / sum(seg_periodo) FROM fato_disponibilidade_diaria""")
    assert agg.fetchall()[0][0] == pytest.approx(frota)


def test_linhas_nao_mapeadas_sao_contadas(rodar):
    m = rodar(log(("2025-01-10 10:00", "A", "50/51", "ATUOU"), ("2025-01-10 10:00", "A", C, "FALHA"),
                  ("2025-01-10 10:10", "A", C, "NORMAL")))
    assert m.stats["linhas_nao_mapeadas"] == 1


def test_config_invalida():
    from telemetry_kpis import config
    with pytest.raises(ValueError):
        config.carregar(disponibilidade={"inicio_repetido": "qualquer"})


def test_periodo_parcial(rodar):
    m = rodar(log(("2025-01-10 10:00", "A", C, "FALHA"), ("2025-01-10 11:00", "A", C, "NORMAL")),
              ini="2025-01-10 06:00", fim="2025-01-11 00:00")
    d = m.df("fato_disponibilidade_diaria")
    assert d["seg_periodo"].tolist() == [18 * 3600]
    assert d["disponibilidade"].iloc[0] == pytest.approx(1 - 1 / 18)
    assert pd.Timestamp(m.periodo[0]) == pd.Timestamp("2025-01-10 06:00")
