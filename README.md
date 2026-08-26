# bitrix24-rpa-observability

Painel de saúde dos RPAs do Bitrix24 — desenhado em torno da falha que
nenhum painel comum detecta: **o robô que simplesmente parou de rodar**.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Streamlit](https://img.shields.io/badge/streamlit-1.35%2B-red)
![Testes](https://img.shields.io/badge/testes-30%20passando-brightgreen)
![Licença](https://img.shields.io/badge/licença-MIT-lightgrey)

---

## As três falhas que produzem painel verde sobre operação quebrada

### 1. Silêncio

Robô diário que não roda hoje **não aparece em métrica nenhuma** — ele não
gera linha. Taxa de sucesso: 100%. Falhas: zero. Painel verde. E a
importação de leads está parada há uma semana.

Detectar silêncio exige comparar com a frequência esperada, e é o único
alerta desta lista que nenhum agregador pega sozinho.

### 2. Execução que nunca terminou

Robô morto por falta de memória não grava o fim. Fica `em_andamento` para
sempre. Aqui vira `PERDIDA` depois do prazo, e `PERDIDA` é alerta crítico.

### 3. Sucesso com zero registros

O importador rodou, não deu erro e importou **nada** — porque o caminho da
planilha mudou. Tecnicamente sucesso, operacionalmente falha silenciosa. A
situação `VAZIA` existe para isso, e "todas as execuções vazias" é crítico.

---

## Instrumentação

Cada robô da série usa o contexto:

```python
from observabilidade import Registro

registro = Registro(Path("state/rpa.db"))

with registro.executando("importador-leads", origem="planilha.csv") as execucao:
    execucao.processados = importar()
```

Início, fim, duração e exceção são gravados sozinhos. A exceção é
**re-levantada** depois de registrada — o robô ainda precisa falhar
visivelmente para o agendador.

E o contador parcial sobrevive à falha: saber que 300 registros entraram
antes do erro muda a decisão de reprocesso.

---

## Uso

```bash
pip install -e ".[dev]"

# para o cron, de hora em hora
painel-rpa --banco state/rpa.db --frequencias frequencias.json

# para olhar
streamlit run painel.py
```

```
Ultimas 24h — 47 execucoes

  importador-leads: 24 execucoes | 100% sucesso | mediana 31s, p95 44s | 12.480 registros
! vigia-sla: 6 execucoes | 67% sucesso | mediana 8s, p95 120s | 43 registros
  bx-elt: 1 execucoes | 100% sucesso | mediana 412s, p95 412s | 84.210 registros

2 alertas:
  [CRITICO] vigia-sla: taxa de sucesso em 67% (2 falhas em 6). Ultimo erro: BitrixAPIError [QUERY_LIMIT_EXCEEDED]
  [CRITICO] backup-crm: nao roda ha 51h; o esperado e a cada 26h
```

Sai com código **1** quando há alerta crítico. `--json` para alimentar outro
sistema.

---

## Decisões técnicas

**Percentil, não média.** Duração média de 40 s com um pico de 20 min parece
saudável. A mediana mostra o caso típico; o p95, o que se sente quando as
coisas vão mal.

Um teste documenta a propriedade que confunde na leitura do painel: **o p95
não isola um único outlier em 20 amostras**. Isso é estatística, não defeito
— 1 em 20 não é o percentil 95, e vinte execuções são poucas para esse
número dizer algo. Está no teste porque a pergunta ia surgir.

**SQLite, não Postgres.** Dezenas de execuções por dia. O arquivo vai junto
do robô e não há mais um serviço para manter de pé. `PRAGMA journal_mode=WAL`
para o painel ler enquanto um robô escreve — sem isso, abrir o dashboard
durante uma carga trava a carga.

**Robô configurado que nunca rodou também alerta.** É o silêncio mais
absoluto: não há nem linha para agregar, então a verificação precisa partir
da lista de frequências esperadas, não das execuções existentes.

**Uma execução vazia é aviso; todas vazias é crítico.** Uma planilha vazia
acontece. Cinco seguidas significa que a fonte mudou.

---

## Fecha a série

Este é o robô que vigia os outros 22. Cada um deles instrumentado com o mesmo
contexto, um `frequencias.json` declarando o esperado, e um cron rodando
`painel-rpa` de hora em hora.

Automação sem observabilidade é automação que você descobre que parou pelo
cliente ligando.

## Licença

MIT.
