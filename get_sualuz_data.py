#!/usr/bin/env python3
"""Importa telemetria da SuaLuz para o InfluxDB.

O cliente web atual da SuaLuz usa o endpoint Wisebyte ``item-pt``. O parser
mantém compatibilidade com a resposta legada para facilitar migrações e
diagnóstico de instalações antigas.
"""

from __future__ import annotations

import argparse
import sys
import time as time_sleep
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Mapping, Sequence

import pytz
import requests
import yaml
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS


DEFAULT_API_URL = (
    "https://apiapp.sualuz.com.br/wisebyte-site/prod/api/v0.1.0/item-pt"
)
LEGACY_API_URL = (
    "https://apiapp.sualuz.com.br/telemetria/api/v1/Telemetria/atual"
)
MEASUREMENT_NAME = "consumo_energia"
FIELD_NAME = "potencia_W"


class ConfigurationError(ValueError):
    """Configuração ausente ou inválida."""


class ApiContractError(ValueError):
    """A resposta da API não segue nenhum contrato conhecido."""


@dataclass(frozen=True)
class Measurement:
    timestamp_utc: datetime
    power_w: float


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Busca telemetria histórica ou do dia atual na SuaLuz e grava "
            "os pontos no InfluxDB."
        )
    )
    parser.add_argument(
        "-s",
        "--start_date",
        required=True,
        help="Data inicial no formato YYYY-MM-DD.",
    )
    parser.add_argument(
        "-e",
        "--end_date",
        help="Data final no formato YYYY-MM-DD. O padrão é a data inicial.",
    )
    parser.add_argument(
        "-c",
        "--config",
        default="sualuz_config.yaml",
        help="Arquivo YAML de configuração (padrão: sualuz_config.yaml).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Consulta e valida a API sem conectar nem gravar no InfluxDB. "
            "Útil para conferir um token ou uma mudança de contrato."
        ),
    )
    return parser


def load_config(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            loaded = yaml.safe_load(file) or {}
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Arquivo de configuração não encontrado: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"YAML inválido em {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigurationError("A raiz do arquivo YAML precisa ser um objeto.")
    return loaded


def parse_date_range(start_value: str, end_value: str | None) -> tuple[date, date]:
    try:
        start_date = date.fromisoformat(start_value)
        end_date = date.fromisoformat(end_value) if end_value else start_date
    except ValueError as exc:
        raise ConfigurationError("Use datas no formato YYYY-MM-DD.") from exc

    if start_date > end_date:
        raise ConfigurationError("A data inicial não pode ser posterior à data final.")
    if start_date > date.today():
        raise ConfigurationError("A data inicial não pode estar no futuro.")
    if end_date > date.today():
        print(
            f"[!] Data final futura; ajustando {end_date.isoformat()} "
            f"para {date.today().isoformat()}."
        )
        end_date = date.today()
    return start_date, end_date


def get_timezone(config: Mapping[str, Any]) -> pytz.BaseTzInfo:
    timezone_name = str(config.get("timezone", "America/Sao_Paulo"))
    try:
        return pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError as exc:
        raise ConfigurationError(f"Fuso horário inválido: {timezone_name}") from exc


def validate_config(config: Mapping[str, Any], *, dry_run: bool) -> None:
    sualuz = config.get("sualuz") or {}
    if not isinstance(sualuz, Mapping):
        raise ConfigurationError("A seção 'sualuz' precisa ser um objeto.")

    missing = [name for name in ("bearer_token", "mac") if not sualuz.get(name)]
    if not dry_run:
        influx = config.get("influxdb") or {}
        if not isinstance(influx, Mapping):
            raise ConfigurationError("A seção 'influxdb' precisa ser um objeto.")
        missing.extend(
            f"influxdb.{name}"
            for name in ("url", "token", "org", "bucket")
            if not influx.get(name)
        )

    if missing:
        raise ConfigurationError(
            "Configurações obrigatórias ausentes: " + ", ".join(missing)
        )


def build_wisebyte_payload(mac: str, target_date: date) -> dict[str, Any]:
    """Monta o corpo usado pelo cliente web atual da SuaLuz."""
    luz_id = mac.removeprefix("luz-")
    day = target_date.isoformat()
    return {
        "items": [
            {
                "luz_id": luz_id,
                "initial_date": f"{day} 00:00:00",
                "final_date": f"{day} 23:59:59",
                "period": "minute",
                "time_wanted": 1,
            }
        ]
    }


def _power_value(raw_value: Any) -> float:
    if isinstance(raw_value, Mapping):
        for key in ("value", "pt", "power", "potencia", "potencia_W"):
            if key in raw_value:
                raw_value = raw_value[key]
                break
        else:
            raise ApiContractError(
                "Ponto de telemetria sem um campo de potência reconhecido."
            )
    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ApiContractError(f"Valor de potência inválido: {raw_value!r}") from exc


def _timestamp_to_utc(raw_timestamp: Any, local_tz: pytz.BaseTzInfo) -> datetime:
    if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
        raise ApiContractError(f"Timestamp inválido: {raw_timestamp!r}")

    normalized = raw_timestamp.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ApiContractError(f"Timestamp não reconhecido: {raw_timestamp!r}") from exc

    if parsed.tzinfo is None:
        parsed = local_tz.localize(parsed)
    return parsed.astimezone(pytz.utc)


def _legacy_measurements(
    items: Sequence[Any], target_date: date, local_tz: pytz.BaseTzInfo
) -> list[Measurement]:
    measurements: list[Measurement] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ApiContractError("Item legado não é um objeto JSON.")
        minute = item.get("minuto")
        power = item.get("pt")
        if minute is None or power is None:
            raise ApiContractError("Item legado sem os campos 'minuto' e 'pt'.")
        try:
            local_time = time.fromisoformat(str(minute))
        except ValueError as exc:
            raise ApiContractError(f"Horário legado inválido: {minute!r}") from exc
        local_datetime = local_tz.localize(datetime.combine(target_date, local_time))
        measurements.append(
            Measurement(local_datetime.astimezone(pytz.utc), _power_value(power))
        )
    return measurements


def extract_measurements(
    payload: Any, target_date: date, local_tz: pytz.BaseTzInfo
) -> list[Measurement]:
    """Normaliza respostas Wisebyte e legadas em uma lista de medições."""
    if isinstance(payload, list):
        return _legacy_measurements(payload, target_date, local_tz)

    if not isinstance(payload, Mapping):
        raise ApiContractError("A resposta da API não é um objeto nem uma lista.")

    response = payload.get("response", payload)
    if not isinstance(response, Mapping):
        raise ApiContractError("O campo 'response' da API não é um objeto.")

    if "result_array" not in response:
        raise ApiContractError("Resposta sem o campo 'response.result_array'.")
    result_array = response.get("result_array")
    if result_array is None:
        return []
    if not isinstance(result_array, Mapping):
        raise ApiContractError("'response.result_array' não é um objeto indexado por data.")

    measurements = [
        Measurement(
            timestamp_utc=_timestamp_to_utc(timestamp, local_tz),
            power_w=_power_value(power),
        )
        for timestamp, power in result_array.items()
    ]
    measurements.sort(key=lambda item: item.timestamp_utc)
    return measurements


def request_day(
    session: requests.Session,
    sualuz_config: Mapping[str, Any],
    target_date: date,
) -> Any:
    mode = str(sualuz_config.get("api_mode", "wisebyte")).lower()
    mac = str(sualuz_config["mac"])
    timeout = float(sualuz_config.get("timeout_seconds", 60))
    configured_url = sualuz_config.get("base_url")

    if mode == "legacy":
        url = str(configured_url or LEGACY_API_URL)
        response = session.get(
            url,
            params={
                "Mac": mac,
                "DataInicio": target_date.isoformat(),
                "Tarifa": sualuz_config.get("tarifa", 0.90637183),
            },
            timeout=timeout,
        )
    elif mode == "wisebyte":
        url = str(configured_url or DEFAULT_API_URL)
        response = session.post(
            url,
            json=build_wisebyte_payload(mac, target_date),
            timeout=timeout,
        )
    else:
        raise ConfigurationError("sualuz.api_mode deve ser 'wisebyte' ou 'legacy'.")

    if response.status_code == 401:
        raise PermissionError(
            "Token SuaLuz inválido ou expirado. Renove sualuz.bearer_token."
        )
    response.raise_for_status()
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as exc:
        preview = response.text[:160].replace("\n", " ")
        raise ApiContractError(f"A API não retornou JSON: {preview!r}") from exc


def make_session(bearer_token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "accept": "application/json, text/plain, */*",
            "authorization": f"Bearer {bearer_token}",
            "content-type": "application/json",
            "origin": "https://luz.sualuz.com.br",
            "referer": "https://luz.sualuz.com.br/",
            "user-agent": "sualuz-influxdb-importer/2",
        }
    )
    return session


def make_points(measurements: Iterable[Measurement], mac: str) -> list[Point]:
    return [
        Point(MEASUREMENT_NAME)
        .tag("fonte", "sualuz")
        .tag("mac_medidor", mac)
        .field(FIELD_NAME, measurement.power_w)
        .time(measurement.timestamp_utc)
        for measurement in measurements
    ]


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        start_date, end_date = parse_date_range(args.start_date, args.end_date)
        validate_config(config, dry_run=args.dry_run)
        local_tz = get_timezone(config)
    except ConfigurationError as exc:
        print(f"Erro de configuração: {exc}", file=sys.stderr)
        return 2

    sualuz_config = config["sualuz"]
    mac = str(sualuz_config["mac"])
    session = make_session(str(sualuz_config["bearer_token"]))
    interval = float(sualuz_config.get("request_interval_seconds", 1.5))

    influx_client: InfluxDBClient | None = None
    write_api = None
    if not args.dry_run:
        influx = config["influxdb"]
        influx_client = InfluxDBClient(
            url=influx["url"],
            token=influx["token"],
            org=influx["org"],
            timeout=30_000,
        )
        if not influx_client.ping():
            print("Erro: não foi possível conectar ao InfluxDB.", file=sys.stderr)
            influx_client.close()
            return 1
        write_api = influx_client.write_api(write_options=SYNCHRONOUS)
        print(f"[+] Conectado ao InfluxDB em {influx['url']}.")

    total_points = 0
    failed_days = 0
    current_date = start_date
    print(f"[*] Consultando de {start_date} até {end_date}.")

    try:
        while current_date <= end_date:
            print(f"[*] {current_date.isoformat()}: consultando a SuaLuz...")
            try:
                payload = request_day(session, sualuz_config, current_date)
                measurements = extract_measurements(payload, current_date, local_tz)
                if not measurements:
                    print("    Nenhum ponto retornado.")
                elif args.dry_run:
                    first = measurements[0].timestamp_utc.isoformat()
                    last = measurements[-1].timestamp_utc.isoformat()
                    print(
                        f"    OK: {len(measurements)} pontos válidos "
                        f"({first} até {last}), sem escrita."
                    )
                    total_points += len(measurements)
                else:
                    points = make_points(measurements, mac)
                    assert write_api is not None
                    influx = config["influxdb"]
                    write_api.write(
                        bucket=influx["bucket"], org=influx["org"], record=points
                    )
                    total_points += len(points)
                    print(f"    {len(points)} pontos gravados no InfluxDB.")
            except PermissionError as exc:
                print(f"Erro fatal: {exc}", file=sys.stderr)
                return 1
            except (requests.RequestException, ApiContractError, ValueError) as exc:
                failed_days += 1
                print(f"    Erro: {exc}", file=sys.stderr)

            current_date += timedelta(days=1)
            if current_date <= end_date:
                time_sleep.sleep(interval)
    finally:
        session.close()
        if influx_client is not None:
            influx_client.close()

    action = "validados" if args.dry_run else "gravados"
    print(f"[*] Concluído: {total_points} pontos {action}; {failed_days} dia(s) com erro.")
    return 1 if failed_days else 0


if __name__ == "__main__":
    raise SystemExit(run())
