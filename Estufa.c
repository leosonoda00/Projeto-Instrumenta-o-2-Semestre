/**
 * @file Estufa_Final.c
 * @brief Controlador de Estufa Automatizada com RP2040 (Raspberry Pi Pico)
 * * Funcionalidades:
 * - Leitura de sensores (LDR, NTC, Umidade) com filtro de média móvel.
 * - Controle de atuadores (Bomba, Ventilador, LED de Crescimento).
 * - Lógica de fotoperíodo (meta diária de luz considerando Sol + LED).
 * - Comunicação UART bidirecional (Recebimento de comandos e Telemetria).
 * - Arquitetura não-bloqueante usando interrupções e temporizadores.
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "pico/stdlib.h"
#include "hardware/adc.h"
#include "hardware/uart.h"
#include "pico/time.h"
#include "hardware/sync.h" 
#include "hardware/irq.h"
#include "hardware/watchdog.h"

// --- Definição de Hardware ---
const uint FAN_PIN = 6;           // Controle do Ventilador
const uint PUMP_PIN = 2;          // Controle da Bomba de Água
const uint LED_PIN = 9;           // Painel de LED (Grow light)
const uint PIN_ADC_0_LDR = 26;    // ADC0: Sensor de Luz
const uint PIN_ADC_1_NTC = 27;    // ADC1: Sensor de Temperatura
const uint PIN_ADC_2_UMIDADE = 28;// ADC2: Sensor de Umidade de Solo

// --- Configuração da UART ---
#define UART_ID uart0
#define BAUD_RATE 9600
#define UART_TX_PIN 0
#define UART_RX_PIN 1

// --- Parâmetros do Filtro de Média Móvel ---
// Utiliza deslocamento de bits para divisão rápida (2^5 = 32 amostras)
#define AVG_SHIFT_BITS 5
#define AVG_SAMPLES (1 << AVG_SHIFT_BITS)
#define TIMER_ISR_INTERVAL_MS 100 // Frequência de amostragem

// Buffers circulares para armazenar histórico de leituras
static uint16_t ldr_buffer[AVG_SAMPLES], ntc_buffer[AVG_SAMPLES], umidade_buffer[AVG_SAMPLES];
static int avg_idx = 0; // Índice atual do buffer circular
static uint32_t ldr_sum = 0, ntc_sum = 0, umidade_sum = 0; // Somas acumuladas para cálculo eficiente

// --- Variáveis Globais (Voláteis pois são alteradas em ISR) ---
volatile uint16_t g_ldr_filtrado = 0;
volatile uint16_t g_ntc_filtrado = 0;
volatile uint16_t g_umidade_filtrada = 0;

// Setpoints de Controle (Valores padrão, alteráveis via UART)
volatile uint16_t g_umidade_setpoint_raw = 3000;
volatile uint16_t g_temp_setpoint_raw = 1600;
volatile uint16_t g_ldr_limiar_raw = 2000; 
volatile bool g_fotoperiodo_ativo = false; 

// Controle de Fotoperíodo (Cota Diária de Luz)
volatile uint32_t g_meta_luz_segundos = 14 * 3600; // Ex: 14 horas de luz
volatile uint32_t g_segundos_de_luz_hoje = 0;
static uint32_t g_contador_1s = 0; // Auxiliar para contar segundos dentro do timer de 100ms

// --- Buffer de Comunicação UART ---
#define RX_BUFFER_SIZE 100
volatile char g_rx_buffer[RX_BUFFER_SIZE];
volatile int g_rx_idx = 0;
volatile bool g_comando_pronto = false; // Flag para indicar que um comando completo chegou

/**
 * @brief Interrupção de RX da UART
 * Processa byte a byte. Detecta fim de comando por '\n' ou '\r'.
 * Garante que o loop principal não trave esperando dados.
 */
void on_uart_rx() {
    while (uart_is_readable(UART_ID)) {
        char c = uart_getc(UART_ID);
        // Verifica terminadores de linha para finalizar o comando
        if (c == '\n' || c == '\r') {
            if (g_rx_idx > 0) { 
                g_rx_buffer[g_rx_idx] = '\0'; // Finaliza string C
                g_comando_pronto = true;      // Sinaliza para o main processar
                g_rx_idx = 0;                 // Reseta índice para próximo comando
            }
        } else if (g_rx_idx < (RX_BUFFER_SIZE - 1)) { 
            g_rx_buffer[g_rx_idx++] = c;      // Armazena caractere se houver espaço
        }
    }
}

/**
 * @brief Callback do Temporizador (100ms)
 * Responsável por:
 * 1. Leitura dos ADCs
 * 2. Atualização da Média Móvel (Filtro Digital)
 * 3. Contabilização do tempo de exposição à luz
 */
bool timer_callback(repeating_timer_t *t) {
    // Leitura crua dos sensores
    adc_select_input(0); uint16_t ldr_raw = adc_read();
    adc_select_input(1); uint16_t ntc_raw = adc_read();
    adc_select_input(2); uint16_t umidade_raw = adc_read();

    // Atualização da soma móvel (subtrai o mais antigo, soma o novo)
    ldr_sum = ldr_sum - ldr_buffer[avg_idx] + ldr_raw;
    ntc_sum = ntc_sum - ntc_buffer[avg_idx] + ntc_raw;
    umidade_sum = umidade_sum - umidade_buffer[avg_idx] + umidade_raw;

    // Atualiza buffer circular
    ldr_buffer[avg_idx] = ldr_raw;
    ntc_buffer[avg_idx] = ntc_raw;
    umidade_buffer[avg_idx] = umidade_raw;

    // Calcula média (divisão por bit shift para eficiência)
    g_ldr_filtrado = (uint16_t)(ldr_sum >> AVG_SHIFT_BITS);
    g_ntc_filtrado = (uint16_t)(ntc_sum >> AVG_SHIFT_BITS);
    g_umidade_filtrada = (uint16_t)(umidade_sum >> AVG_SHIFT_BITS);

    avg_idx = (avg_idx + 1) % AVG_SAMPLES;
    
    // Lógica de Contagem de Luz (Sol + LED)
    // O timer roda a cada 100ms -> 10 ticks = 1 segundo
    g_contador_1s++;
    if (g_contador_1s >= 10) { 
        g_contador_1s = 0;
        
        bool led_ligado = (gpio_get(LED_PIN) == 1);
        bool tem_sol = (g_ldr_filtrado <= g_ldr_limiar_raw); // Lógica inversa do LDR (Menor valor = Mais luz)

        // Se houver luz artificial ou natural, incrementa contador diário
        if (led_ligado || tem_sol) {
            g_segundos_de_luz_hoje++;
        }
    }
    return true; // Mantém o timer repetindo
}

/**
 * @brief Interpretador de Comandos
 * Formato esperado: "COMANDO,TIPO,VALOR"
 */
void processa_comando() {
    g_comando_pronto = false; 
    char* valor_str;
    
    // Parseia string recebida e atualiza variáveis de controle
    if (strstr((const char*)g_rx_buffer, "SET,HUMID,") != NULL) {
        valor_str = strrchr((const char*)g_rx_buffer, ',');
        if (valor_str) g_umidade_setpoint_raw = (uint16_t)atoi(valor_str + 1);
    }
    else if (strstr((const char*)g_rx_buffer, "SET,TEMP,") != NULL) {
        valor_str = strrchr((const char*)g_rx_buffer, ',');
        if (valor_str) g_temp_setpoint_raw = (uint16_t)atoi(valor_str + 1);
    }
    else if (strstr((const char*)g_rx_buffer, "SET,LDR,") != NULL) {
        valor_str = strrchr((const char*)g_rx_buffer, ',');
        if (valor_str) g_ldr_limiar_raw = (uint16_t)atoi(valor_str + 1);
    }
    else if (strstr((const char*)g_rx_buffer, "SET,FOTO,") != NULL) {
        valor_str = strrchr((const char*)g_rx_buffer, ',');
        if (valor_str) g_fotoperiodo_ativo = ((uint16_t)atoi(valor_str + 1) == 1);
    }
    else if (strstr((const char*)g_rx_buffer, "SET,META_LUZ,") != NULL) {
        valor_str = strrchr((const char*)g_rx_buffer, ',');
        if (valor_str) g_meta_luz_segundos = (uint32_t)atol(valor_str + 1);
    }
    else if (strstr((const char*)g_rx_buffer, "RESET,TIMER_LUZ") != NULL) {
        g_segundos_de_luz_hoje = 0;
    }
    // Limpa buffer para evitar lixo de memória
    memset((void*)g_rx_buffer, 0, RX_BUFFER_SIZE);
}

// --- MAIN ---
int main() {
    // 1. Inicialização de Periféricos
    stdio_init_all(); 
    adc_init();
    adc_gpio_init(PIN_ADC_0_LDR); adc_gpio_init(PIN_ADC_1_NTC); adc_gpio_init(PIN_ADC_2_UMIDADE);
    
    // Configuração dos GPIOs de atuadores
    gpio_init(FAN_PIN); gpio_set_dir(FAN_PIN, GPIO_OUT); gpio_put(FAN_PIN, 0); 
    gpio_init(PUMP_PIN); gpio_set_dir(PUMP_PIN, GPIO_OUT); gpio_put(PUMP_PIN, 0);
    gpio_init(LED_PIN); gpio_set_dir(LED_PIN, GPIO_OUT); gpio_put(LED_PIN, 0); 

    // 2. Configuração da UART e Interrupções
    uart_init(UART_ID, BAUD_RATE);
    gpio_set_function(UART_TX_PIN, GPIO_FUNC_UART);
    gpio_set_function(UART_RX_PIN, GPIO_FUNC_UART);
    
    // Habilita interrupção de RX para não travar a CPU esperando dados
    irq_set_exclusive_handler(UART0_IRQ, on_uart_rx);
    irq_set_enabled(UART0_IRQ, true);
    uart_set_irq_enables(UART_ID, true, false);
    
    // Watchdog de 2 segundos para reinício automático em caso de travamento
    watchdog_enable(2000, 1);

    // Inicialização de variáveis e buffers
    uint8_t packet[13];
    memset(ldr_buffer, 0, sizeof(ldr_buffer));
    memset(ntc_buffer, 0, sizeof(ntc_buffer));
    memset(umidade_buffer, 0, sizeof(umidade_buffer));

    // Inicializa timer recorrente (100ms) para leitura de sensores
    repeating_timer_t timer;
    add_repeating_timer_ms(-TIMER_ISR_INTERVAL_MS, timer_callback, NULL, &timer);

    uint32_t ultimo_envio = 0;

    // --- Loop Principal (Super Loop) ---
    while (1) {
        // 1. Processamento de Comandos (Prioridade)
        if (g_comando_pronto) processa_comando();
        
        // 2. Lógica de Controle (Atuadores)
        // Baseado nos valores filtrados atualizados pelo Timer
        if (g_umidade_filtrada > g_umidade_setpoint_raw) gpio_put(PUMP_PIN, 1); else gpio_put(PUMP_PIN, 0);
        if (g_ntc_filtrado < g_temp_setpoint_raw) gpio_put(FAN_PIN, 1); else gpio_put(FAN_PIN, 0);

        // Lógica Complementar de Luz:
        // Se o fotoperíodo está ativo e a meta diária não foi atingida:
        // Liga o LED apenas se a luz natural for insuficiente.
        if (g_fotoperiodo_ativo && (g_segundos_de_luz_hoje < g_meta_luz_segundos)) {
            if (g_ldr_filtrado > g_ldr_limiar_raw) gpio_put(LED_PIN, 1); 
            else gpio_put(LED_PIN, 0); 
        } else {
            gpio_put(LED_PIN, 0); // Desliga se meta atingida ou fotoperíodo desativado
        }

        // 3. Telemetria (Envio Não-Bloqueante)
        // Envia estado atual a cada 1 segundo sem usar sleep() longo
        uint32_t agora = to_ms_since_boot(get_absolute_time());
        if (agora - ultimo_envio >= 1000) {
            ultimo_envio = agora;

            // Montagem do pacote de dados (Big Endian)
            // Divide uint16_t/uint32_t em bytes individuais para transporte serial
            packet[0] = (g_ldr_filtrado >> 8) & 0xFF;
            packet[1] = g_ldr_filtrado & 0xFF;
            packet[2] = (g_ntc_filtrado >> 8) & 0xFF;
            packet[3] = g_ntc_filtrado & 0xFF;
            packet[4] = (g_umidade_filtrada >> 8) & 0xFF;
            packet[5] = g_umidade_filtrada & 0xFF;
            packet[6] = gpio_get(LED_PIN) ? 1 : 0;
            // Tempo de luz (32 bits = 4 bytes)
            packet[7] = (g_segundos_de_luz_hoje >> 24) & 0xFF;
            packet[8] = (g_segundos_de_luz_hoje >> 16) & 0xFF;
            packet[9] = (g_segundos_de_luz_hoje >> 8) & 0xFF;
            packet[10] = g_segundos_de_luz_hoje & 0xFF;

            // Checksum simples (soma de verificação) para integridade
            uint8_t soma = 0;
            for(int i=0; i<11; i++) soma += packet[i];
            packet[11] = soma;
            packet[12] = 0xAA; // Byte finalizador de pacote

            uart_write_blocking(UART_ID, packet, 13);
            
            // "Chuta" o watchdog indicando que o sistema está vivo
            watchdog_update();
        }
        
        // Pequeno delay para aliviar a CPU, mas mantendo responsividade
        sleep_ms(1); 
    }
}