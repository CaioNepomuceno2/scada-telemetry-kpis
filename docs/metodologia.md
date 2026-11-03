# Metodologia

Este documento descreve as regras e fórmulas do `telemetry-kpis`, o desenho da validação com dados sintéticos e
as premissas de cada número do README. As regras configuráveis estão em
[`src/telemetry_kpis/config_padrao.yaml`](../src/telemetry_kpis/config_padrao.yaml).

## 1. Entrada: o log de eventos

O motor lê um log no formato longo, com uma linha por evento:

| coluna | conteúdo |
|---|---|
| `timestamp` | instante do evento |
| `equipamento` | identificador do equipamento (religador, disjuntor, chave, RTU) |
| `ponto` | ponto do SCADA (ex.: `COMUNICACAO`, `CMD_52`, `52`, `79`) |
| `estado` | estado ou valor do ponto (ex.: `FALHA`, `NORMAL`, `ABRIR`, `ABERTO`) |

O `mapeamento` do YAML traduz cada par `(ponto, estado)` num evento canônico:

| evento canônico | significado | campos extras |
|---|---|---|
| `COMM_FALHA` | início de falha de comunicação | — |
| `COMM_NORMAL` | fim de falha de comunicação | — |
| `COMANDO` | comando enviado pelo centro de operação | `familia`, `valor`, `esperado` |
| `ESTADO` | estado reportado pelo equipamento | `familia`, `valor` |

A `familia` agrupa o comando e o retorno do mesmo dispositivo lógico. O exemplo usa a numeração da
IEEE C37.2 / ANSI: `52` (disjuntor/posição) e `79` (religamento). Linhas sem mapeamento são descartadas e
contadas em `linhas_nao_mapeadas`, como as atuações de proteção `50/51` no exemplo.

Para usar com um SCADA real, basta reescrever o `mapeamento`. O código não depende de nomes de pontos.

## 2. Disponibilidade de comunicação

### 2.1 Pareamento das falhas

Por equipamento, os eventos `COMM_FALHA` e `COMM_NORMAL` são ordenados pelo `timestamp`. Ordenar pela ordem do
arquivo é uma opção que existe só para mostrar o viés. Depois:

1. **Duplicados.** Um evento igual ao anterior a menos de `janela_duplicado_s` (padrão 2 s) é descartado.
2. **Intervalo.** Cada `COMM_FALHA` abre um intervalo que termina no evento seguinte do mesmo equipamento.
   O `fim_tipo` registra como ele terminou:
   * `normal`: terminou num `COMM_NORMAL`;
   * `novo_inicio`: terminou noutra `COMM_FALHA`, ou seja, o `COMM_NORMAL` se perdeu;
   * `aberto`: não há evento seguinte no log.
3. **NORMAL perdido** (`fim_inferido`):
   * `proximo_evento`: a falha vai até a próxima `COMM_FALHA`. Pode inflar a indisponibilidade em dias ou semanas.
   * `mediana_equipamento` (padrão): a falha dura a mediana das falhas bem pareadas do próprio equipamento,
     limitada ao próximo evento. Se o equipamento não tem falhas bem pareadas, usa a mediana da frota.
   * Com `inicio_repetido: manter_primeiro`, a segunda `COMM_FALHA` é ignorada e a primeira só fecha no próximo
     `COMM_NORMAL`.
4. **Falha aberta no fim do período** (`aberto_no_fim`): `fechar_no_fim_periodo` (padrão) ou `descartar`.
5. **NORMAL sem FALHA anterior** (`fim_sem_inicio`): `ignorar` (padrão) ou `abrir_no_inicio_periodo`.
6. **Limites opcionais:** `duracao_minima_s` filtra falhas curtas. `duracao_maxima_sem_fim_h` limita as falhas
   com fim inferido.
7. **Corte no período.** Os intervalos são cortados em `[inicio, fim)`. Uma falha que começou antes do período
   conta horas, mas não conta como falha nova (`iniciou_antes`).

### 2.2 Divisão por dia e expurgo de manutenção

Cada intervalo é dividido em segundos por dia (`generate_series` no DuckDB). Uma falha que cruza a meia-noite ou
a virada do mês fica com cada parte no seu dia. As janelas de manutenção programada (`equipamento, ini, fim`)
recebem o mesmo tratamento.

Com `expurgar_manutencao: true`, para cada equipamento e dia:

```
seg_indisp       = segundos em falha − segundos em falha que caem dentro de manutenção
seg_disponivel   = seg_periodo − seg_manutencao
disponibilidade  = 1 − seg_indisp / seg_disponivel
```

O expurgo supõe que as janelas de manutenção de um mesmo equipamento não se sobrepõem.

### 2.3 Agregação

Toda agregação (mês, equipamento, subestação, região, tecnologia, frota) é **ponderada pelo tempo**: soma os
segundos e só depois divide. Nunca é a média das disponibilidades:

```
disponibilidade_grupo = 1 − Σ seg_indisp / (Σ seg_periodo − Σ seg_manutencao)
```

A grade é completa (equipamento × dia). Por isso, equipamentos sem nenhuma falha entram com 100% e não somem das
médias.

## 3. Assertividade de comandos

1. **Tentativas.** Comandos iguais do mesmo equipamento e da mesma família, a menos de `janela_repeticao_s`
   (padrão 60 s) do anterior, viram uma tentativa (`repetidos: agrupar`) com `n_tentativas`. Com `contar_todos`,
   cada linha é uma tentativa.
2. **Busca do retorno.** Para cada tentativa, um ASOF join encontra o primeiro `ESTADO` da mesma família e do
   mesmo equipamento a partir de `ts − tolerancia_relogio_s`. O padrão é 2 s, porque o estado vem com o relógio
   do equipamento e o comando com o relógio do centro. Um segundo ASOF join procura o primeiro estado igual ao
   `esperado`.
3. **Classificação** (`limite = tempo_limite_s`, `busca = janela_busca_s`):

| resultado | regra |
|---|---|
| `SUCESSO` | estado esperado em até `limite` |
| `RETORNO_INESPERADO` | outro estado da família em até `limite`, sem o esperado |
| `ATRASADO` | estado esperado depois do `limite`, mas em até `busca` |
| `SEM_RETORNO` | nenhum estado esperado em até `busca` |
| `SEM_COMUNICACAO` | não foi `SUCESSO` e o comando caiu dentro de uma falha de comunicação do equipamento |

4. **Taxas.** `assertividade = SUCESSO / tentativas`. `assertividade_com_comunicacao` exclui as tentativas
   `SEM_COMUNICACAO`, que são falhas do canal de telecom e não do equipamento. O tempo de resposta (p50 e p95)
   usa só os `SUCESSO`.

## 4. Analítica de gestão de ativos

| análise | regra padrão |
|---|---|
| **Crônico** | disponibilidade mensal abaixo da meta (98%) em pelo menos 3 dos últimos 4 meses |
| **Flapping** | em 2 meses ou mais, 30 falhas ou mais no mês com duração mediana de até 5 min |
| **Pareto** | fração das horas indisponíveis explicada pelos 1%, 5%, 10% e 20% piores equipamentos (ou subestações) |
| **Degradação** | carta EWMA de Poisson sobre a contagem semanal de falhas de cada equipamento |

**EWMA de Poisson.** A carta usa a contagem semanal de falhas `x_t`:
* `z_t = λ·x_t + (1 − λ)·z_{t−1}`, com `λ = 0,2`.
* A taxa-base `μ` é a média das 12 primeiras semanas, encolhida para a média da tecnologia com peso de 8 semanas,
  com piso de 0,2 falha/semana.
* Limite: `μ + L·√(μ·λ/(2 − λ))`, com `L = 4`.
* Alarme na primeira semana com `z_t` acima do limite.

O piso e o `L` equilibram detecção e alarme falso. Com contagens baixas, a aproximação normal subestima a cauda
de Poisson. Os testes de calibração estão em `tests/test_analitica.py` e na seção 6.

**Fila de manutenção por valor.** Para cada equipamento, no período recente (padrão: últimos 3 meses):

```
excesso_pp      = max(0, (disponibilidade_pos_reparo − disponibilidade) × 100)
peso            = clientes_jusante / média de clientes_jusante da frota
valor_rs_ano    = excesso_pp × (R$/ano por p.p. da frota ÷ frota_referencia) × peso × prob_sucesso_reparo
dec_min_ano     = idem com minutos de DEC por p.p.
prioridade      = ordem decrescente de valor_rs_ano / custo_visita_rs
```

**De onde vem o valor por p.p.** Do projeto
[automation-impact](https://github.com/CaioNepomuceno2/automation-impact), arquivo `reports/valor.json`. Ali,
1 p.p. de indisponibilidade da telemetria dos 4.000 religadores da distribuidora estudada no automation-impact custa 0,70 a 1,41 min de DEC/ano,
ou R$ 134 mil a R$ 268 mil/ano na visão social (R$ 35 mil a R$ 70 mil/ano só em compensações). A faixa vem da
premissa sobre quanto do benefício do religador depende de comunicação. A fila usa o ponto médio, cerca de
**R$ 50 por p.p. por equipamento médio por ano**.

Os custos de visita (R$ 900 a R$ 2.500 por tecnologia), a probabilidade de sucesso do reparo (80%) e a meta
pós-reparo (99%) são **premissas ilustrativas**. O peso por clientes a jusante é um proxy. O ideal é usar o CHI
dependente de cada religador (BDGD + interrupções), como no automation-impact.

## 5. Gerador sintético e "verdade"

O gerador (`gerador.py`) cria uma frota fictícia: `EQ00001...`, subestações `SE-001...`, regiões `R1...R5`. A
**verdade** fica guardada à parte do log.

| item | premissa do gerador |
|---|---|
| Tipos | religador 70%, disjuntor 15%, chave telecomandada 10%, RTU 5% |
| Tecnologia | fibra 15%, rádio 40%, celular 45%; metade dos disjuntores e RTUs em fibra |
| Falhas de comunicação | Poisson por equipamento: 0,02 (fibra), 0,08 (rádio) e 0,15 (celular) por dia, +2% por ano de idade |
| Duração das falhas | lognormal; mediana de 30 min (fibra), 20 min (rádio) e 10 min (celular); σ = 1,3; até 20 dias |
| Perfis | normal 89%; **problemático** 5% (6× mais falhas, 3× mais longas); **flapping** 2% (4 quedas curtas por dia, mediana de 45 s); **degradando** 4% (taxa sobe linearmente até 10× por 30 a 90 dias, até uma visita que resolve) |
| Manutenção programada | 2% dos equipamentos por mês; janela de 2 a 8 h em dia útil (o canal cai durante a janela) |
| Comandos | Poisson de 3/mês (religador e chave) e 1,5/mês (disjuntor); 70% família 52 e 30% família 79 |
| Resultado real dos comandos (com comunicação) | fibra 96/1/1/2%, rádio 92/2/2/4%, celular 88/2/3/7% (sucesso/inesperado/atrasado/sem retorno); problemáticos 75/3/7/15% |
| Tempo de resposta | lognormal com mediana de 4 s; atrasados de 35 a 300 s |
| Repetição pelo operador | 40% dos comandos sem retorno são reenviados após 10 a 25 s |
| Atuações espontâneas | 2/mês por religador ou disjuntor (50/51, 52 abre e religa; 20% vão a bloqueio) |
| **Ruído do log** | 2% de linhas duplicadas (0 a 1,5 s depois), 0,3% de linhas perdidas, chegada fora de ordem (até 120 s), relógio do equipamento com desvio padrão de 1,5 s (só nos eventos de estado) |

A disponibilidade verdadeira de cada equipamento sai dos intervalos reais, sem passar pelo motor:
`1 − (Σ falhas − Σ manutenção) / (T − Σ manutenção)`. A manutenção está sempre contida na união dos intervalos.

## 6. Desenho da validação

1. **Sem ruído.** O motor precisa reproduzir a verdade exatamente: erro de disponibilidade < 1e-9 p.p. e contagem
   de resultados de comandos idêntica (`tests/test_recuperacao.py`).
2. **Com ruído.** Erro por equipamento e da frota em p.p., e acerto da classificação de comandos contra a
   verdade (casamento por equipamento, família e timestamp ±2 s).
3. **Robustez.** O mesmo log ruidoso, com cada regra alterada isoladamente (`reports/robustez_*.csv`).
4. **Analítica.** Precisão e recall de crônicos e flapping contra o perfil verdadeiro. Detecção de degradação
   entre o início real da degradação e a visita. Alarme falso nos equipamentos normais.
5. **Fila (backtest).** A fila é montada com a disponibilidade **calculada** de janeiro a setembro. O ganho é
   medido com a disponibilidade **verdadeira** de outubro a dezembro: o excesso que cada equipamento visitado
   teria, convertido em R$ e DEC. Isso compara estratégias (valor, pior disponibilidade, mais antigo,
   aleatória) sem que a fila "veja" o período em que é avaliada.

**Interpretação.** A validação mostra que o **método** recupera o que de fato aconteceu e que as regras
importam. As comparações entre estratégias e tecnologias refletem as premissas do gerador: são ilustrativas, não
um retrato de alguma frota real.

## 7. Modelo de dados (Power BI)

| tabela | grão | chaves |
|---|---|---|
| `dim_equipamento` | equipamento | `equipamento` |
| `dim_subestacao` | subestação | `subestacao` |
| `dim_tempo` | dia | `dia` (com `ano`, `mes`, `ano_mes`, `semana_iso`) |
| `fato_disponibilidade_diaria` | equipamento × dia | `equipamento`, `dia` |
| `fato_disponibilidade_mensal` | equipamento × mês | `equipamento`, `ano_mes` |
| `fato_falha_comunicacao` | falha pareada | `equipamento` (com `ini`, `fim`, `duracao_min`, `fim_tipo`) |
| `fato_comando` | tentativa de comando | `equipamento` (com `ts`, `familia`, `valor`, `resultado`, `tempo_resposta_s`) |

Relações: as dimensões 1 → * nos fatos por `equipamento` e `dia`, e `dim_subestacao` 1 → * em `dim_equipamento`.
Medida de disponibilidade ponderada pelo tempo em DAX:

```
Disponibilidade =
1 - DIVIDE(
    SUM(fato_disponibilidade_diaria[seg_indisp]),
    SUM(fato_disponibilidade_diaria[seg_periodo]) - SUM(fato_disponibilidade_diaria[seg_manutencao])
)
```
