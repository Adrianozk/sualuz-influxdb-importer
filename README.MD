# SuaLuz InfluxDB Importer

Este script Python busca dados de consumo de energia minuto a minuto da plataforma SuaLuz (acessada via `luz.sualuz.com.br`) e os envia para um banco de dados InfluxDB, permitindo a criação de dashboards personalizados (ex: com Grafana) ou integração com outras plataformas.

**Atenção:** Este é um projeto não oficial e depende da API interna do aplicativo web SuaLuz. A API pode mudar sem aviso prévio, quebrando a funcionalidade deste script. Use por sua conta e risco.

## Funcionalidades

* Busca dados históricos de consumo (potência em Watts) por dia ou intervalo de dias.
* Busca dados do dia atual (parcialmente, até o momento da execução).
* Envia os dados formatados para o InfluxDB (v1.8+ ou v2+).
* Configurável via arquivo YAML.
* Lida com a necessidade de atualização manual do token de autenticação.

## Pré-requisitos

* Python 3.7+ (recomendado 3.9+ para `zoneinfo` nativo, embora `pytz` seja usado para compatibilidade)
* Conta no SuaLuz e acesso ao medidor via web (`luz.sualuz.com.br`)
* Instância do InfluxDB (v1.8 ou v2+) acessível pelo script.
* Bibliotecas Python: `requests`, `PyYAML`, `influxdb-client`, `pytz`

## Configuração

1.  **Clone o repositório:**
    ```bash
    git clone [https://github.com/SEU_USUARIO/SEU_REPOSITORIO.git](https://github.com/SEU_USUARIO/SEU_REPOSITORIO.git)
    cd SEU_REPOSITORIO
    ```

2.  **Instale as dependências:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Crie e configure o `sualuz_config.yaml`:**
    * Copie o arquivo `sualuz_config.example.yaml` (ou crie um novo) para `sualuz_config.yaml`.
    * **Obtenha o Token Bearer:**
        * Acesse `luz.sualuz.com.br` no seu navegador.
        * Abra as Ferramentas de Desenvolvedor (F12).
        * Vá para a aba "Rede" (Network).
        * Navegue na interface para carregar os dados de consumo.
        * Encontre uma requisição para a URL `.../Telemetria/atual?...`.
        * Nos cabeçalhos (Headers) da requisição, encontre `Authorization: Bearer SEU_TOKEN_LONGO`.
        * Copie **apenas** o token (a parte longa depois de `Bearer `).
        * **Cole este token** no campo `bearer_token` dentro de `sualuz:` no arquivo `sualuz_config.yaml`. **Este token expira e precisará ser atualizado manualmente de tempos em tempos.**
    * **Preencha as outras informações:**
        * `sualuz -> mac`: Coloque o MAC do seu medidor (ex: `luz-xxxxxx`).
        * `influxdb -> url`: A URL completa da sua instância InfluxDB (ex: `http://192.168.1.100:8086`).
        * `influxdb -> token`: Um Token de API do InfluxDB com permissão de escrita no bucket desejado.
        * `influxdb -> org`: O nome da sua Organização no InfluxDB.
        * `influxdb -> bucket`: O nome do Bucket no InfluxDB onde os dados serão salvos (crie-o no InfluxDB se não existir).
        * `timezone` (opcional): Defina seu fuso horário local (padrão: `America/Sao_Paulo`).

## Uso

Execute o script `get_sualuz_data.py` via linha de comando, fornecendo a data inicial desejada.

* **Buscar dados de um dia específico (ex: 28 de Abril de 2025):**
    ```bash
    python get_sualuz_data.py -s 2025-04-28
    ```

* **Buscar dados de um intervalo (ex: 20 a 27 de Abril de 2025):**
    ```bash
    python get_sualuz_data.py -s 2025-04-20 -e 2025-04-27
    ```

* **Buscar dados do dia atual (até o momento):**
    ```bash
    # Substitua pela data de hoje
    python get_sualuz_data.py -s YYYY-MM-DD
    ```

* **Usar um arquivo de configuração diferente:**
    ```bash
    python get_sualuz_data.py -s 2025-04-28 -c /caminho/alternativo/config.yaml
    ```

O script buscará os dados para cada dia no intervalo e os enviará para o InfluxDB. Se o token expirar durante a execução, o script avisará e parará, indicando a partir de qual data você deve continuar após atualizar o token.

## Visualização (Grafana)

Após os dados estarem no InfluxDB, você pode usar o Grafana para visualizá-los:

1.  Configure o InfluxDB como uma fonte de dados (Data Source) no Grafana.
2.  Crie um novo dashboard.
3.  Adicione painéis usando queries Flux ou InfluxQL para mostrar:
    * Potência instantânea (`potencia_W`).
    * Consumo acumulado por hora/dia (usando `aggregateWindow` e `integral` ou `sum`).
    * Custo estimado (multiplicando o consumo em kWh pela tarifa).