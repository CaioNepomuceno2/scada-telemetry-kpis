from conftest import log


def resultado(m):
    return m.df("fato_comando").sort_values("ts")["resultado"].tolist()


def test_sucesso_e_tempo_de_resposta(rodar):
    m = rodar(log(("2025-01-10 10:00:00", "A", "CMD_52", "ABRIR"), ("2025-01-10 10:00:04", "A", "52", "ABERTO")),
              comandos=True)
    f = m.df("fato_comando").iloc[0]
    assert f["resultado"] == "SUCESSO" and f["tempo_resposta_s"] == 4


def test_atrasado_inesperado_sem_retorno(rodar):
    m = rodar(log(
        ("2025-01-10 10:00:00", "A", "CMD_52", "ABRIR"), ("2025-01-10 10:01:00", "A", "52", "ABERTO"),
        ("2025-01-11 10:00:00", "A", "CMD_52", "FECHAR"), ("2025-01-11 10:00:03", "A", "52", "ABERTO"),
        ("2025-01-12 10:00:00", "A", "CMD_79", "BLOQUEAR"),
    ), comandos=True)
    assert resultado(m) == ["ATRASADO", "RETORNO_INESPERADO", "SEM_RETORNO"]


def test_retorno_de_outra_familia_nao_conta(rodar):
    m = rodar(log(("2025-01-10 10:00:00", "A", "CMD_79", "BLOQUEAR"), ("2025-01-10 10:00:02", "A", "52", "ABERTO")),
              comandos=True)
    assert resultado(m) == ["SEM_RETORNO"]


def test_retorno_de_outro_equipamento_nao_conta(rodar):
    m = rodar(log(("2025-01-10 10:00:00", "A", "CMD_52", "ABRIR"), ("2025-01-10 10:00:02", "B", "52", "ABERTO")),
              comandos=True)
    assert resultado(m) == ["SEM_RETORNO"]


def test_sem_comunicacao(rodar):
    m = rodar(log(("2025-01-10 09:50", "A", "COMUNICACAO", "FALHA"), ("2025-01-10 10:00", "A", "CMD_52", "ABRIR"),
                  ("2025-01-10 10:30", "A", "COMUNICACAO", "NORMAL")), comandos=True)
    assert resultado(m) == ["SEM_COMUNICACAO"]
    agg = m.assertividade_agregada("equipamento").iloc[0]
    assert agg["assertividade"] == 0 and agg["frac_sem_comunicacao"] == 1


def test_repetidos_agrupados_ou_contados(rodar):
    linhas = (("2025-01-10 10:00:00", "A", "CMD_52", "ABRIR"), ("2025-01-10 10:00:15", "A", "CMD_52", "ABRIR"),
              ("2025-01-10 10:00:20", "A", "52", "ABERTO"))
    m = rodar(log(*linhas), comandos=True)
    f = m.df("fato_comando")
    assert len(f) == 1 and f["n_tentativas"].iloc[0] == 2 and f["resultado"].iloc[0] == "SUCESSO"
    assert f["tempo_resposta_s"].iloc[0] == 20
    m2 = rodar(log(*linhas), comandos=True, assertividade={"repetidos": "contar_todos"})
    assert len(m2.df("fato_comando")) == 2


def test_tolerancia_de_relogio(rodar):
    # o relógio do equipamento está 1 s atrasado: o retorno chega "antes" do comando
    linhas = (("2025-01-10 10:00:00", "A", "CMD_52", "ABRIR"), ("2025-01-10 09:59:59", "A", "52", "ABERTO"))
    assert resultado(rodar(log(*linhas), comandos=True)) == ["SUCESSO"]
    m = rodar(log(*linhas), comandos=True, assertividade={"tolerancia_relogio_s": 0})
    assert resultado(m) == ["SEM_RETORNO"]


def test_agregado_exclui_sem_comunicacao(rodar):
    m = rodar(log(
        ("2025-01-10 10:00:00", "A", "CMD_52", "ABRIR"), ("2025-01-10 10:00:03", "A", "52", "ABERTO"),
        ("2025-01-11 09:00:00", "A", "COMUNICACAO", "FALHA"), ("2025-01-11 10:00:00", "A", "CMD_52", "FECHAR"),
        ("2025-01-11 11:00:00", "A", "COMUNICACAO", "NORMAL"),
    ), comandos=True)
    agg = m.assertividade_agregada("equipamento").iloc[0]
    assert agg["assertividade"] == 0.5 and agg["assertividade_com_comunicacao"] == 1.0
