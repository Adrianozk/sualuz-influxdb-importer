from datetime import date

import pytz
import pytest

from get_sualuz_data import (
    ApiContractError,
    build_wisebyte_payload,
    extract_measurements,
)


SAO_PAULO = pytz.timezone("America/Sao_Paulo")


def test_builds_current_wisebyte_request_body():
    payload = build_wisebyte_payload("luz-abc123", date(2026, 8, 31))

    assert payload == {
        "items": [
            {
                "luz_id": "abc123",
                "initial_date": "2026-08-31 00:00:00",
                "final_date": "2026-08-31 23:59:59",
                "period": "minute",
                "time_wanted": 1,
            }
        ]
    }


def test_parses_current_response_and_sorts_timestamps():
    payload = {
        "response": {
            "result_array": {
                "2026-08-31 00:02:00": 123.4,
                "2026-08-31 00:01:00": "120.5",
            }
        }
    }

    measurements = extract_measurements(payload, date(2026, 8, 31), SAO_PAULO)

    assert [item.power_w for item in measurements] == [120.5, 123.4]
    assert measurements[0].timestamp_utc.isoformat() == "2026-08-31T03:01:00+00:00"


def test_preserves_timezone_from_iso_timestamp():
    payload = {
        "response": {
            "result_array": {"2026-08-31T03:01:00Z": {"value": 99.0}}
        }
    }

    measurement = extract_measurements(payload, date(2026, 8, 31), SAO_PAULO)[0]

    assert measurement.timestamp_utc.isoformat() == "2026-08-31T03:01:00+00:00"
    assert measurement.power_w == 99.0


def test_keeps_legacy_response_compatibility():
    payload = [{"minuto": "00:01", "pt": 87.5}]

    measurement = extract_measurements(payload, date(2026, 8, 31), SAO_PAULO)[0]

    assert measurement.timestamp_utc.isoformat() == "2026-08-31T03:01:00+00:00"
    assert measurement.power_w == 87.5


def test_rejects_unknown_contract():
    with pytest.raises(ApiContractError, match="result_array"):
        extract_measurements({"response": {}}, date(2026, 8, 31), SAO_PAULO)
