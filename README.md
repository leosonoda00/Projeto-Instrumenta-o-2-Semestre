# Estufa Inteligente — Dashboard (Interface IHM)

Projeto Dashboard para uma Estufa Inteligente (IoT) conectada a um Raspberry Pi Pico.

Este repositório contém uma aplicação Python (Dash + Plotly) que:

- Recebe pacotes binários via Serial (UART) enviados pelo firmware do Pico
- Persiste leituras no SQLite
- Exibe gráficos e indicadores em tempo real (temperatura, luminosidade, umidade, fotoperíodo)
- Permite controle remoto de setpoints (umidade, temperatura, limiar LDR, meta de luz diária)
- Integração opcional com Google Gemini para sugestões de cultivo (IA)

---

## Estrutura principal

- `app.py` — aplicação principal (dashboard, leitura serial, persistência SQLite, integração com Gemini)
- `minha_estufa.db` — banco SQLite (será criado automaticamente em primeira execução)
- `build/` — arquivos do firmware / build environment (projeto Pico C) — já gerados

---

## Requisitos

- Python 3.8+ (recomendado 3.10+) 
- Bibliotecas Python (instalar via pip):
  - dash
  - dash-bootstrap-components
  - plotly
  - pandas
  - pyserial
  - google-generativeai (opcional, para integração Gemini)

Exemplo de arquivo `requirements.txt` (opcional):

```
dash
dash-bootstrap-components
plotly
pandas
pyserial
google-generativeai
```

---

## Instalação rápida (Windows PowerShell)

1. Recomendo usar um virtualenv:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Se você não tiver um `requirements.txt`, instale as dependências diretamente:

```powershell
pip install dash dash-bootstrap-components plotly pandas pyserial google-generativeai
```

---

## Configuração

- A porta serial padrão no `app.py` é `COM12` com `BAUD_RATE = 9600`. Modifique `COM_PORT`/`BAUD_RATE` conforme o dispositivo.
- Banco de dados: `DB_FILE = 'minha_estufa.db'` (arquivo criado automaticamente, se não existir).

### API Key Google Gemini (opcional)

Para ativar a função de consultoria com Gemini (IA), configure a variável de ambiente `GOOGLE_API_KEY`:

```powershell
# para a sessão atual
$env:GOOGLE_API_KEY = 'sua_api_key_aqui'

# para persistir no Windows (reinicie a janela para aplicar)
setx GOOGLE_API_KEY "sua_api_key_aqui"
```

Se a variável não estiver definida o dashboard continuará funcionando — a função IA ficará desativada.

---

## Como rodar

No terminal (com o venv ativado):

```powershell
python app.py
```

Isso iniciará um servidor local em `http://127.0.0.1:5000` (porta 5000 por padrão). Em modo sem conexão Serial, a interface roda em modo visualização (apenas leitura salvo por envio de comandos que falharão se a Serial estiver desconectada).

---

## Protocolo Serial (resumo)

O firmware do Pico envia pacotes binários de 13 bytes com o formato usado em `app.py`:

- bytes 0-1: LDR (uint16) — luminosidade ADC
- bytes 2-3: NTC/ADC (uint16) — valor ADC do NTC
- bytes 4-5: Umidade (uint16) — leitura ADC do sensor capacitivo
- byte 6: LED status (0/1)
- bytes 7-10: Luz acumulada (uint32) — segundos do fotoperíodo acumulado
- byte 11: checksum (somatório dos bytes 0..10 & 0xFF)
- byte 12: terminador 0xAA

Além disso, o painel envia comandos textuais (para o MCU) no formato `SET,TIPO,VALOR\n` — por exemplo:

- `SET,HUMID,<raw>`
- `SET,TEMP,<raw>`
- `SET,LDR,<raw>`
- `SET,META_LUZ,<seconds>`
- `RESET,TIMER_LUZ` (reseta contador de luz)
- `SET,FOTO,1` ou `SET,FOTO,0` (habilita/desabilita fotoperíodo)

---

## Banco de dados (SQLite)

Tabela `readings` (criada automaticamente):

- id (INTEGER PRIMARY KEY)
- timestamp (INTEGER, ms)
- ldr_raw (INTEGER)
- temperature_c (REAL)
- umidade_raw (INTEGER)
- umidade_percent (REAL)
- led_status (INTEGER)
- luz_acumulada_s (INTEGER)

---

## Observações e troubleshooting

- Se a Serial estiver desconectada a aplicação inicia em modo de visualização (não grava leituras).
- Se você receber `Checksum Inválido`, verifique a consistência do protocolo no firmware C e a ordem de bytes (big-endian).
- Se tiver problemas com permissões na porta serial no Windows, verifique drivers e o Gerenciador de Dispositivos.

---
