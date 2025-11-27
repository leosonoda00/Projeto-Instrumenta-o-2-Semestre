"""
@file dashboard.py
@brief Interface IHM para Estufa Inteligente (IoT Dashboard)
@details
    - Coleta dados via Serial (UART) do Raspberry Pi Pico.
    - Persistência de dados em banco SQLite local.
    - Interface Web Responsiva usando Dash/Plotly.
    - Integração com Google Gemini AI para consultoria agronômica.
    - Controle bidirecional (Envia setpoints para o firmware).
"""

import serial
import sqlite3
import threading
import struct
import os
import math 
import google.generativeai as genai
import time
import pandas as pd
import plotly.graph_objects as go
import dash
import dash_bootstrap_components as dbc
from dash import dcc, html, Input, Output, State
import json
from datetime import datetime
import logging

# =============================================================================
# CONFIGURAÇÕES GERAIS E CONSTANTES
# =============================================================================

# Configuração da Porta Serial (Verificar no Gerenciador de Dispositivos)
COM_PORT = 'COM12' 
BAUD_RATE = 9600
DB_FILE = 'minha_estufa.db'

# Parâmetros de Calibração dos Sensores
# NTC 10k: Beta 3950, resistor divisor de 10k
R_FIXO_NTC = 10000.0
R_NOMINAL_NTC = 10000.0
TEMP_NOMINAL_C = 25.0
BETA_NTC = 3950.0

# Calibração do Sensor de Umidade Capacitivo (Curva Exponencial/Logarítmica)
HUMID_A = 3899.7
HUMID_B = -3.484

# Parâmetros do ADC do RP2040
ADC_MAX = 4095.0
V_IN = 3.3
LDR_LIMIAR_FIXO = 2000

# =============================================================================
# INTEGRAÇÃO COM IA (Google Gemini)
# =============================================================================
try:
    # Configura a chave de API via variável de ambiente para segurança
    genai.configure(api_key=os.environ["GOOGLE_API_KEY"])
    model = genai.GenerativeModel('gemini-flash-latest')
except KeyError:
    print("Aviso: GOOGLE_API_KEY não encontrada. A IA não funcionará.")
    model = None

# =============================================================================
# FUNÇÕES DE FÍSICA E MATEMÁTICA (Conversão ADC -> Unidade Real)
# =============================================================================

def calculate_temp_ntc(adc_raw):
    """
    Converte leitura crua do ADC em Temperatura (°C)
    Utiliza a equação Beta simplificada (Steinhart-Hart).
    """
    try:
        if adc_raw > 4050: return None # Sensor desconectado ou curto
        
        v_out = (adc_raw * V_IN) / ADC_MAX
        r_ntc = (v_out * R_FIXO_NTC) / (V_IN - v_out)
        
        # Equação Beta
        ln_r = math.log(r_ntc / R_NOMINAL_NTC)
        t_kelvin = 1.0 / ((1.0/(TEMP_NOMINAL_C + 273.15)) + (1.0/BETA_NTC) * ln_r)
        t_c = t_kelvin - 273.15
        
        return None if (t_c < -10 or t_c > 80) else t_c
    except: 
        return None 

def calculate_humidity_percent(adc_raw):
    """
    Converte leitura do sensor capacitivo em Porcentagem (0-100%).
    Baseado na calibração experimental (fórmula inversa).
    """
    try:
        y = float(adc_raw)
        if y <= 0: return 100.0
        if y >= HUMID_A: return 0.0
        
        x = math.log(y / HUMID_A) / HUMID_B
        return max(0.0, min(100.0, x * 100.0))
    except: 
        return None

def calculate_humidity_setpoint_raw(perc):
    """Converte setpoint do usuário (%) para valor RAW esperado pelo MCU."""
    try: 
        return int(max(0, min(4095, HUMID_A * math.exp(HUMID_B * (perc / 100.0)))))
    except: 
        return 3000 

def calculate_temp_setpoint_raw(temp_c):
    """Converte setpoint do usuário (°C) para valor RAW esperado pelo MCU."""
    try:
        t_k = temp_c + 273.15
        t0_k = TEMP_NOMINAL_C + 273.15
        r_ntc = R_NOMINAL_NTC * math.exp(((1.0/t_k) - (1.0/t0_k)) * BETA_NTC)
        v_out = (r_ntc * V_IN) / (R_FIXO_NTC + r_ntc)
        return int(max(0, min(4095, (v_out * ADC_MAX) / V_IN)))
    except: 
        return 1600

# =============================================================================
# CAMADA DE DADOS (SQLite)
# =============================================================================

def init_db():
    """Inicializa o esquema do banco de dados se não existir."""
    try:
        con = sqlite3.connect(DB_FILE)
        con.execute('''
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER, 
                ldr_raw INTEGER, 
                temperature_c REAL,
                umidade_raw INTEGER, 
                umidade_percent REAL,
                led_status INTEGER, 
                luz_acumulada_s INTEGER
            )
        ''')
        con.commit()
        con.close()
    except Exception as e: 
        print(f"Erro BD: {e}")

# =============================================================================
# THREAD DE COMUNICAÇÃO SERIAL (Backend)
# =============================================================================

def read_from_pico(ser): 
    """
    Worker Thread: Monitora a porta serial continuamente.
    Lê pacotes binários de 13 bytes, valida checksum e salva no SQLite.
    Isso roda em paralelo para não travar a interface Dash.
    """
    # SQLite precisa de conexão própria por thread
    db_con = sqlite3.connect(DB_FILE, check_same_thread=False)
    print(">>> Thread de Leitura Serial Iniciada")
    
    while True:
        try:
            # Protocolo: Aguarda byte 0xAA (Final de pacote)
            packet = ser.read_until(b'\xAA')
            
            # Validação do Tamanho do Pacote (definido no firmware C)
            if len(packet) == 13: 
                # Cálculo de Checksum (Soma dos primeiros 11 bytes)
                chk = (sum(packet[0:11])) & 0xFF
                
                # Validação de Integridade
                if chk == packet[11]: 
                    # Decodificação Big Endian (MSB primeiro)
                    ldr = (packet[0]<<8) | packet[1]
                    ntc = (packet[2]<<8) | packet[3]
                    hum = (packet[4]<<8) | packet[5]
                    led = packet[6] 
                    # Luz acumulada é um uint32 (4 bytes)
                    acc_luz = (packet[7]<<24) | (packet[8]<<16) | (packet[9]<<8) | packet[10] 
                    
                    # Conversão física
                    temp_c = calculate_temp_ntc(ntc)
                    hum_p = calculate_humidity_percent(hum)
                    
                    if temp_c is not None and hum_p is not None:
                        # Persistência
                        db_con.execute(
                            "INSERT INTO readings (timestamp, ldr_raw, temperature_c, umidade_raw, umidade_percent, led_status, luz_acumulada_s) VALUES (?,?,?,?,?,?,?)", 
                            (int(time.time()*1000), ldr, temp_c, hum, hum_p, led, acc_luz)
                        )
                        db_con.commit()
                        print(f"[RX] LDR:{ldr} | T:{temp_c:.1f}°C | H:{hum_p:.1f}% | LED:{led} | Luz:{acc_luz}s")
                else:
                    print(f"[ERRO] Checksum Inválido: Calc {chk} != Rec {packet[11]}")
        except Exception as e: 
            print(f"[ERRO CRÍTICO] Falha na Serial: {e}")
            time.sleep(5) # Espera antes de tentar reconectar

# =============================================================================
# FRONTEND DASHBOARD (Dash + Plotly)
# =============================================================================

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.CYBORG, 'https://fonts.googleapis.com/css2?family=Roboto:wght@400;700&display=swap'])
app.title = "Estufa Inteligente"
CARD_STYLE = {"backgroundColor": "#2a2a2a", "color": "white"}

# --- Layout da Página (Grid System Bootstrap) ---
app.layout = dbc.Container(fluid=True, style={'backgroundColor': '#111111', 'color': 'white', 'padding': '20px', 'fontFamily': 'Roboto'}, children=[
    
    # Cabeçalho
    dbc.Row(dbc.Col(html.H1("MONITORAMENTO ESTUFA IOT", className="text-center text-primary mb-4"))),
    
    # Gráfico Principal (Histórico)
    dbc.Row(dbc.Col(dcc.Graph(id='main-graph')), className="mb-4"),
    
    # Cards de Indicadores Atuais (Gauges)
    dbc.Row([
        dbc.Col(md=3, children=[dbc.Card(style=CARD_STYLE, children=[dbc.CardHeader("Temperatura"), dbc.CardBody([dcc.Graph(id='g-temp', style={'height':'200px'}), html.Div(id='s-temp', className="text-center")])])]),
        dbc.Col(md=3, children=[dbc.Card(style=CARD_STYLE, children=[dbc.CardHeader("Luminosidade"), dbc.CardBody([dcc.Graph(id='g-ldr', style={'height':'200px'}), html.Div(id='s-ldr', className="text-center")])])]),
        dbc.Col(md=3, children=[dbc.Card(style=CARD_STYLE, children=[dbc.CardHeader("Umidade Solo"), dbc.CardBody([dcc.Graph(id='g-hum', style={'height':'200px'}), html.Div(id='s-hum', className="text-center")])])]),
        
        # Card Especial: Status da Iluminação Artificial
        dbc.Col(md=3, children=[dbc.Card(style=CARD_STYLE, children=[
            dbc.CardHeader("Fotoperíodo (Sol + LED)"),
            dbc.CardBody([
                html.Div(id='led-indicator', style={'width':'50px', 'height':'50px', 'borderRadius':'50%', 'margin':'0 auto 20px auto', 'backgroundColor':'gray'}),
                html.H5("Tempo de Luz Hoje:", className="text-center"),
                html.H3(id='light-counter', className="text-center text-warning"),
                dbc.Progress(id='light-progress', value=0, striped=True, animated=True, color="warning", style={"height": "20px"})
            ])
        ])]),
    ], className="mb-4"),

    # Área de Controle e IA
    dbc.Row([
        # Controle Manual de Setpoints
        dbc.Col(md=6, children=[dbc.Card(style=CARD_STYLE, children=[
            dbc.CardHeader("Painel de Controle Remoto"),
            dbc.CardBody([
                dbc.Row([dbc.Col(html.Label("Setpoint Umidade (%)"),width=6), dbc.Col(dcc.Input(id='in-hum', type='number', value=50, style={'width':'100%'}))]), html.Br(),
                dbc.Row([dbc.Col(html.Label("Setpoint Temp (°C)"),width=6), dbc.Col(dcc.Input(id='in-temp', type='number', value=25, style={'width':'100%'}))]), html.Br(),
                dbc.Row([dbc.Col(html.Label("Meta Luz (Horas/Dia)"),width=6), dbc.Col(dcc.Input(id='in-meta', type='number', value=14, style={'width':'100%'}))]), html.Br(),
                dbc.Button('ENVIAR PARA ESTUFA', id='btn-apply', n_clicks=0, color="success", className="w-100"),
                html.Div(id='out-apply', className="text-center mt-2 text-muted", children=f"Limiar LDR fixo em {LDR_LIMIAR_FIXO}")
            ])
        ])]),
        
        # Consultoria IA
        dbc.Col(md=6, children=[dbc.Card(style=CARD_STYLE, children=[
            dbc.CardHeader("Agrônomo Virtual (Gemini AI)"),
            dbc.CardBody([
                dcc.Input(id='in-plant', type='text', placeholder='Digite o nome da planta (ex: Orquídea)...', className="dbc", style={'width':'70%', 'marginRight':'10px'}),
                dbc.Button('Consultar IA', id='btn-api', n_clicks=0, color="primary"),
                html.Div(id='out-api', style={'marginTop':'15px', 'padding':'10px', 'backgroundColor':'#333', 'minHeight':'200px', 'borderRadius':'5px'})
            ])
        ])])
    ]),
    
    # Timers para atualização automática
    dcc.Interval(id='tick', interval=2000), # Atualiza gráficos a cada 2s
    dcc.Interval(id='clock', interval=60000) # Eventos de relógio (minuto a minuto)
])

# =============================================================================
# CALLBACKS (Lógica Reativa)
# =============================================================================

@app.callback(
    [Output('main-graph','figure'), Output('g-temp','figure'), Output('s-temp','children'),
     Output('g-ldr','figure'), Output('s-ldr','children'), Output('g-hum','figure'), Output('s-hum','children'),
     Output('led-indicator','style'), Output('light-counter','children'), Output('light-progress','value')],
    Input('tick','n_intervals'), State('in-meta', 'value')
)
def update_graphs(n, meta_horas):
    """
    Callback principal: Busca dados no DB e atualiza todos os gráficos.
    Gera gauges de tempo real e gráfico de linha histórico.
    """
    try:
        # Busca últimos 10 minutos de dados
        con = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query("SELECT * FROM readings WHERE timestamp > ?", con, params=(int(time.time()*1000)-600000,))
        con.close()
        
        df = df.dropna(subset=['temperature_c', 'umidade_percent'])
        
        # Definição de estilos visuais
        empty_fig = go.Figure().update_layout(template="plotly_dark", paper_bgcolor='#2a2a2a', plot_bgcolor='#2a2a2a')
        led_off = {'width':'50px', 'height':'50px', 'borderRadius':'50%', 'margin':'0 auto 20px auto', 'backgroundColor':'gray', 'boxShadow': 'none'}
        led_on = {'width':'50px', 'height':'50px', 'borderRadius':'50%', 'margin':'0 auto 20px auto', 'backgroundColor':'#00FF00', 'boxShadow': '0 0 20px #00FF00'}
        
        if df.empty: return [empty_fig]*2 + ["N/A"] + [empty_fig] + ["N/A"] + [empty_fig] + ["N/A"] + [led_off] + ["0s"] + [0]
        
        df['time'] = pd.to_datetime(df['timestamp'], unit='ms')
        last = df.iloc[-1]
        
        # Gráfico Multieixo (Temp, LDR, Umid)
        fig = go.Figure(layout=go.Layout(template="plotly_dark", paper_bgcolor='#2a2a2a', plot_bgcolor='#2a2a2a'))
        fig.add_trace(go.Scatter(x=df['time'], y=df['temperature_c'], name='Temp', line=dict(color='red')))
        fig.add_trace(go.Scatter(x=df['time'], y=df['ldr_raw'], name='LDR', line=dict(color='gold'), yaxis='y2'))
        fig.add_trace(go.Scatter(x=df['time'], y=df['umidade_percent'], name='Umid', line=dict(color='deepskyblue'), yaxis='y3'))
        
        fig.update_layout(
            yaxis=dict(title=dict(text='Temp (°C)', font=dict(color='red'))),
            yaxis2=dict(title=dict(text='LDR', font=dict(color='gold')), overlaying='y', side='right'),
            yaxis3=dict(title=dict(text='Umid (%)', font=dict(color='deepskyblue')), overlaying='y', side='right', position=0.95)
        )

        # Helper para criar Gauges
        def mk_gauge(val, min_v, max_v, col):
            return go.Figure(go.Indicator(
                mode="gauge+number", value=val, 
                domain={'x':[0,1], 'y':[0,1]}, 
                gauge={'axis':{'range':[min_v,max_v]}, 'bar':{'color':'white'}, 'steps':[{'range':[min_v, max_v], 'color':col}]}
            )).update_layout(template="plotly_dark", height=200, margin=dict(l=20, r=20, t=20, b=20), paper_bgcolor='#2a2a2a')

        # Cálculo de Progresso de Luz
        led_val = last['led_status'] if 'led_status' in last else 0
        acc_luz = last['luz_acumulada_s'] if 'luz_acumulada_s' in last else 0
        led_style = led_on if led_val == 1 else led_off
        
        meta_segundos = float(meta_horas) * 3600 if meta_horas else 1
        progresso = (acc_luz / meta_segundos) * 100
        
        return fig, \
               mk_gauge(last['temperature_c'], 10, 40, 'red'), f"{last['temperature_c']:.1f}°C", \
               mk_gauge(last['ldr_raw'], 0, 4095, 'gold'), f"{last['ldr_raw']}", \
               mk_gauge(last['umidade_percent'], 0, 100, 'deepskyblue'), f"{last['umidade_percent']:.1f}%", \
               led_style, f"{acc_luz}s / {int(meta_segundos)}s", progresso

    except Exception as e: 
        print(f"Erro no Update de Gráficos: {e}")
        return [go.Figure()]*2 + ["Err"] + [go.Figure()] + ["Err"] + [go.Figure()] + ["Err"] + [{'backgroundColor':'red'}] + ["Err"] + [0]

@app.callback(
    [Output('out-api','children'), Output('in-hum','value'), Output('in-temp','value'), Output('in-meta','value')],
    Input('btn-api','n_clicks'), State('in-plant','value'), prevent_initial_call=True
)
def ask_api(n, plant):
    """
    Integração com LLM: Pede JSON estruturado ao Gemini com parâmetros ideais.
    Preenche automaticamente os inputs do usuário.
    """
    if not plant: return "Digite uma planta", dash.no_update, dash.no_update, dash.no_update
    
    prompt = f"""Dados para {plant}: JSON {{ "umidade_ideal_percent": float, "temperatura_ideal_celsius": float, "fotoperiodo_horas": float, "descricao": string }}"""
    
    try:
        if model:
            resp = model.generate_content(prompt)
            data = json.loads(resp.text.replace("```json","").replace("```",""))
            
            return [html.H5("Sugestão da IA:"), html.P(data['descricao']), html.Hr(), "Valores sugeridos aplicados nos campos!"], \
                   data['umidade_ideal_percent'], \
                   data['temperatura_ideal_celsius'], \
                   data['fotoperiodo_horas']
        return "Erro: API Key inválida", dash.no_update, dash.no_update, dash.no_update
    except Exception as e: 
        return f"Erro na IA: {e}", dash.no_update, dash.no_update, dash.no_update

@app.callback(
    Output('out-apply','children'), 
    Input('btn-apply','n_clicks'), 
    [State('in-hum','value'), State('in-temp','value'), State('in-meta','value')], 
    prevent_initial_call=True
)
def apply_settings(n, h, t, m):
    """
    Envia comandos via Serial para o Microcontrolador.
    Protocolo textual: SET,TIPO,VALOR
    """
    global ser
    if ser and ser.is_open:
        try:
            # Converte valores físicos para RAW antes de enviar
            cmd_hum = f"SET,HUMID,{calculate_humidity_setpoint_raw(float(h))}\n"
            cmd_temp = f"SET,TEMP,{calculate_temp_setpoint_raw(float(t))}\n"
            cmd_ldr = f"SET,LDR,{LDR_LIMIAR_FIXO}\n"
            cmd_meta = f"SET,META_LUZ,{int(float(m)*3600)}\n"
            
            ser.write(cmd_hum.encode()); time.sleep(0.1)
            ser.write(cmd_temp.encode()); time.sleep(0.1)
            ser.write(cmd_ldr.encode()); time.sleep(0.1)
            ser.write(cmd_meta.encode())
            
            return dbc.Alert("Configurações enviadas com sucesso!", color="success")
        except Exception as e:
            return dbc.Alert(f"Erro ao enviar: {e}", color="danger")
    return dbc.Alert("Erro: Serial desconectada", color="danger")

@app.callback(Output('out-apply','children', allow_duplicate=True), Input('clock','n_intervals'), prevent_initial_call=True)
def scheduled_events(n):
    """
    Eventos agendados (Relógio).
    Reseta o contador de luz do firmware à meia-noite.
    Controla ativação do fotoperíodo baseado na hora do servidor.
    """
    global ser
    if ser and ser.is_open:
        now = datetime.now()
        # Reset diário
        if now.hour == 0 and now.minute == 0: 
            ser.write(b"RESET,TIMER_LUZ\n")
        
        # Habilita fotoperíodo entre 01:00 e 23:00 (Exemplo)
        ser.write(b"SET,FOTO,1\n" if 1 <= now.hour < 23 else b"SET,FOTO,0\n")
    return dash.no_update

# =============================================================================
# INICIALIZAÇÃO E MAIN
# =============================================================================

ser = None
# Silencia logs desnecessários do servidor Flask interno
log = logging.getLogger('werkzeug'); log.setLevel(logging.ERROR)

if __name__ == '__main__':
    print(">>> Inicializando Sistema da Estufa...")
    init_db()
    
    # Tenta conexão serial
    try: 
        ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=2)
        print(f">>> Serial conectada em {COM_PORT}")
    except: 
        print(">>> AVISO: Serial Offline (Modo de visualização apenas)")
    
    # Inicia Thread de Leitura em Background
    if ser: 
        t = threading.Thread(target=read_from_pico, args=(ser,), daemon=True)
        t.start()
    
    # Inicia Servidor Web

    app.run(debug=True, port=5000, use_reloader=False)
