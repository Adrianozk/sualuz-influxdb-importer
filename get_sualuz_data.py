import requests
import yaml
import argparse
from datetime import date, timedelta, datetime, time
from influxdb_client import InfluxDBClient, Point, WriteOptions
from influxdb_client.client.write_api import SYNCHRONOUS
import time as timesleep # Renomear para evitar conflito com datetime.time
import pytz # Para lidar com fuso horário corretamente pip install pytz
import sys # Para sair do script

# --- Configuração do Argparse ---
# Define como o script aceitará argumentos pela linha de comando
parser = argparse.ArgumentParser(description="Busca dados do SuaLuz (históricos ou dia atual) e salva no InfluxDB.")
parser.add_argument('-s', '--start_date', required=True, help="Data inicial para busca (Formato: YYYY-MM-DD)")
parser.add_argument('-e', '--end_date', help="Data final para busca (Formato: YYYY-MM-DD) - se omitido, busca apenas a data inicial")
parser.add_argument('-c', '--config', default='sualuz_config.yaml', help="Caminho para o arquivo de configuração YAML (padrão: sualuz_config.yaml)")
args = parser.parse_args()

# --- Carregar Configuração ---
# Lê as configurações do arquivo YAML especificado
try:
    with open(args.config, 'r', encoding='utf-8') as f: # Especifica encoding
        config = yaml.safe_load(f)
except FileNotFoundError:
    print(f"Erro: Arquivo de configuração '{args.config}' não encontrado.")
    sys.exit(1) # Encerra o script se o arquivo não for encontrado
except yaml.YAMLError as e:
    print(f"Erro ao ler o arquivo de configuração YAML: {e}")
    sys.exit(1)
except Exception as e:
    print(f"Erro inesperado ao ler arquivo de configuração: {e}")
    sys.exit(1)

# --- Validar e Processar Datas ---
try:
    start_dt = date.fromisoformat(args.start_date)
    # Se end_date não foi fornecido, usa start_date
    end_dt = date.fromisoformat(args.end_date) if args.end_date else start_dt

    # Garante que a data inicial não seja posterior à final
    if start_dt > end_dt:
        raise ValueError("Data inicial não pode ser maior que a data final.")

    # REMOVIDA A VALIDAÇÃO QUE IMPEDIA BUSCAR A DATA DE HOJE
    # A validação abaixo foi removida para permitir buscar dados do dia corrente.
    # today = date.today()
    # if end_dt >= today:
    #      print(f"Aviso: Data final '{end_dt.isoformat()}' é hoje ou futura. Ajustando para ontem '{ (today - timedelta(days=1)).isoformat()}' para garantir dados completos.")
    #      end_dt = today - timedelta(days=1)
    #      # Verifica novamente se start_dt ainda é válida após o ajuste
    #      if start_dt > end_dt:
    #           print(f"Erro: Data inicial '{start_dt.isoformat()}' é inválida após ajuste da data final. Não há histórico completo para buscar.")
    #           sys.exit(1)

    # Adiciona uma verificação para não buscar datas futuras
    if start_dt > date.today():
        print(f"Erro: Data inicial '{start_dt.isoformat()}' é futura. Não é possível buscar dados futuros.")
        sys.exit(1)
    if end_dt > date.today():
        print(f"Aviso: Data final '{end_dt.isoformat()}' é futura. Ajustando para hoje '{date.today().isoformat()}'.")
        end_dt = date.today()


except ValueError as e:
    print(f"Erro no formato das datas: {e}. Use o formato YYYY-MM-DD.")
    sys.exit(1)
except Exception as e:
     print(f"Erro inesperado ao processar datas: {e}")
     sys.exit(1)


# --- Obter Configurações Específicas ---
# Extrai as configurações de 'sualuz' e 'influxdb' do arquivo carregado
sualuz_cfg = config.get('sualuz', {})
influx_cfg = config.get('influxdb', {})

# Atribui valores das configurações a variáveis, com valores padrão se não encontrados
BEARER_TOKEN = sualuz_cfg.get('bearer_token')
API_BASE_URL = sualuz_cfg.get('base_url', "https://apiapp.sualuz.com.br/telemetria/api/v1/Telemetria/atual")
MAC_MEDIDOR = sualuz_cfg.get('mac')
TARIFA = sualuz_cfg.get('tarifa', 0.90637183) # Usar a tarifa do config se disponível

INFLUX_URL = influx_cfg.get('url')
INFLUX_TOKEN = influx_cfg.get('token')
INFLUX_ORG = influx_cfg.get('org')
INFLUX_BUCKET = influx_cfg.get('bucket')

# Verifica se configurações essenciais foram fornecidas
if not all([BEARER_TOKEN, MAC_MEDIDOR, INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET]):
    print("Erro: Configurações essenciais (bearer_token, mac, influx_url, influx_token, influx_org, influx_bucket) não encontradas no arquivo de configuração.")
    sys.exit(1)

# Define o fuso horário local (importante para o InfluxDB)
# Ajuste 'America/Sao_Paulo' se você estiver em outro fuso
try:
    LOCAL_TZ = pytz.timezone(config.get('timezone', 'America/Sao_Paulo'))
except pytz.UnknownTimeZoneError:
    print(f"Erro: Fuso horário '{config.get('timezone', 'America/Sao_Paulo')}' inválido.")
    sys.exit(1)

# --- Conexão InfluxDB ---
# Tenta conectar ao banco de dados InfluxDB
try:
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=30_000) # Timeout de 30s
    # Usa escrita síncrona para garantir que os dados sejam escritos antes de prosseguir
    write_api = client.write_api(write_options=SYNCHRONOUS)
    # Verifica a conexão
    if not client.ping():
         raise Exception("Falha no ping ao InfluxDB. Verifique URL, Token e Org.")
    print(f"Conectado ao InfluxDB em {INFLUX_URL}.")
except Exception as e:
    print(f"Erro ao conectar ou configurar o InfluxDB: {e}")
    sys.exit(1)

# --- Loop para buscar dados históricos ---
# Itera dia a dia, do início ao fim do intervalo especificado
current_dt = start_dt
total_pontos_escritos = 0
dias_com_erro = 0

print(f"[*] Iniciando busca de dados de {start_dt.isoformat()} até {end_dt.isoformat()}...")

while current_dt <= end_dt:
    target_date_str = current_dt.strftime('%Y-%m-%d')
    print(f"[*] Buscando dados para: {target_date_str}")

    # Monta a URL da API para o dia corrente
    api_url = f"{API_BASE_URL}?Mac={MAC_MEDIDOR}&DataInicio={target_date_str}&Tarifa={TARIFA}"

    # Define os cabeçalhos HTTP necessários para a requisição (copiados do curl)
    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        'authorization': f'Bearer {BEARER_TOKEN}',
        'origin': 'https://luz.sualuz.com.br', # Importante para CORS
        'referer': 'https://luz.sualuz.com.br/', # Importante para CORS
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36' # Exemplo, pode ajustar
    }

    try:
        # Faz a requisição GET para a API
        response = requests.get(api_url, headers=headers, timeout=60) # Timeout de 60 segundos

        # --- Tratamento da Resposta da API ---
        if response.status_code == 401:
            # Erro comum se o token expirou
            print(f"[!] ERRO FATAL: Token inválido ou expirado ao buscar dados para {target_date_str}.")
            print(f"[!] Atualize o 'bearer_token' em '{args.config}' e execute novamente o script,")
            print(f"    começando de --start_date {target_date_str}.")
            client.close() # Fecha a conexão com InfluxDB
            sys.exit(1) # Para o script imediatamente
        elif response.status_code != 200:
            # Outros erros HTTP
            print(f"[!] ERRO: Falha ao buscar dados para {target_date_str}. Status: {response.status_code}")
            print(f"    Resposta (início): {response.text[:200]}...") # Mostra o início da resposta para debug
            dias_com_erro += 1
            current_dt += timedelta(days=1) # Pula para o próximo dia
            timesleep.sleep(2) # Pausa maior em caso de erro
            continue # Próxima iteração do loop

        # Processa a resposta JSON se a requisição foi bem-sucedida (status 200)
        try:
            dados_dia = response.json()
        except requests.exceptions.JSONDecodeError:
            print(f"[!] ERRO: Resposta da API para {target_date_str} não é um JSON válido.")
            print(f"    Resposta (início): {response.text[:200]}...")
            dias_com_erro += 1
            current_dt += timedelta(days=1)
            timesleep.sleep(2)
            continue

        # Verifica se a resposta contém dados
        if not dados_dia:
            print(f"[-] Nenhum dado retornado pela API para {target_date_str}. Pulando.")
            current_dt += timedelta(days=1)
            timesleep.sleep(1) # Pequena pausa
            continue

        # --- Processar e Enviar para InfluxDB ---
        points = [] # Lista para armazenar os pontos de dados do dia
        for item in dados_dia:
            try:
                # Extrai os dados de cada registro (minuto e potência)
                hora_minuto_str = item.get('minuto')
                potencia_w_str = item.get('pt') # Vem como float no exemplo, mas tratar como string por segurança

                # Valida se os campos existem
                if hora_minuto_str is None or potencia_w_str is None:
                    print(f"    Aviso: Item com dados ausentes em {target_date_str}: {item}. Pulando item.")
                    continue

                # Converte potência para float
                try:
                    potencia_w = float(potencia_w_str)
                except (ValueError, TypeError):
                     print(f"    Aviso: Valor de potência inválido em {target_date_str}: {item}. Pulando item.")
                     continue

                # Converte string 'HH:MM' para objeto time
                hora_minuto_obj = time.fromisoformat(hora_minuto_str)
                # Combina a data do dia atual com a hora/minuto do registro
                timestamp_naive = datetime.combine(current_dt, hora_minuto_obj)

                # Adiciona o fuso horário local definido
                timestamp_local = LOCAL_TZ.localize(timestamp_naive)
                # Converte para UTC (fuso horário padrão recomendado para InfluxDB)
                timestamp_utc = timestamp_local.astimezone(pytz.utc)

                # Cria o objeto Point do InfluxDB
                point = Point("consumo_energia") \
                    .tag("fonte", "sualuz") \
                    .tag("mac_medidor", MAC_MEDIDOR) \
                    .field("potencia_W", potencia_w) \
                    .time(timestamp_utc) # Usa o timestamp UTC
                points.append(point)

            except ValueError as e:
                 print(f"    Erro ao processar hora/minuto '{hora_minuto_str}' para {target_date_str}: {e}. Pulando item.")
            except Exception as e:
                # Captura outros erros inesperados durante o processamento do item
                print(f"    Erro inesperado processando item {item} para {target_date_str}: {e}. Pulando item.")

        # Escreve os pontos no InfluxDB se houver algum ponto válido
        if points:
            try:
                # Envia os pontos em lote para o InfluxDB
                write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
                print(f"    -> {len(points)} pontos escritos com sucesso para {target_date_str}.")
                total_pontos_escritos += len(points)
            except Exception as e:
                # Erro durante a escrita no banco de dados
                print(f"[!] ERRO ao escrever no InfluxDB para {target_date_str}: {e}")
                dias_com_erro += 1
        else:
             # Caso nenhum ponto válido tenha sido gerado para o dia
             print(f"    Nenhum ponto válido gerado para {target_date_str}.")


    except requests.exceptions.Timeout:
        print(f"[!] ERRO: Timeout ao tentar buscar dados para {target_date_str}. Tentando próximo dia.")
        dias_com_erro += 1
        timesleep.sleep(5) # Pausa maior após timeout
    except requests.exceptions.RequestException as e:
        # Captura outros erros de rede (conexão, DNS, etc.)
        print(f"[!] ERRO de rede ao buscar {target_date_str}: {e}")
        dias_com_erro += 1
        # Poderia adicionar lógica de retentativa aqui se desejado
        timesleep.sleep(5) # Pausa maior em caso de erro de rede
    except Exception as e:
        # Captura qualquer outro erro inesperado no loop principal do dia
        print(f"[!] ERRO inesperado no processamento do dia {target_date_str}: {e}")
        dias_com_erro += 1
        timesleep.sleep(3)


    # Incrementa a data para buscar o próximo dia
    current_dt += timedelta(days=1)
    # Pausa entre as chamadas de API para dias diferentes (evita sobrecarregar a API)
    timesleep.sleep(1.5)

# --- Finalização ---
print("\n[*] Processo de busca concluído.")
print(f"    Total de pontos escritos no InfluxDB: {total_pontos_escritos}")
if dias_com_erro > 0:
    print(f"    Atenção: Ocorreram erros em {dias_com_erro} dia(s). Verifique os logs acima.")
# Fecha a conexão com o InfluxDB
client.close()
print("Conexão com InfluxDB fechada.")