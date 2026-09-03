import json
import re
from pathlib import Path


DASHBOARD_PATH = Path(__file__).parents[1] / "grafana" / "dashboard.json"


def test_dashboard_is_portable_and_matches_the_importer_schema():
    dashboard_text = DASHBOARD_PATH.read_text(encoding="utf-8")
    dashboard = json.loads(dashboard_text)

    assert dashboard["metadata"] == {"name": "sualuz-energy-monitoring"}
    assert "${datasource}" in dashboard_text
    assert "consumo_energia" in dashboard_text
    assert "potencia_W" in dashboard_text
    assert "mac_medidor" in dashboard_text

    # Exported dashboards must not carry a local Grafana data-source UID or
    # a concrete SuaLuz meter identifier.
    assert "dfwths7llpxq8e" not in dashboard_text
    assert re.search(r'"luz-[0-9a-f]+"', dashboard_text, re.IGNORECASE) is None

    panel_titles = {
        panel["spec"]["title"] for panel in dashboard["spec"]["elements"].values()
    }
    assert {
        "Consumo Semanal (kWh)",
        "Custo Semanal (R$)",
        "Consumo Mensal (kWh)",
        "Custo Mensal (R$)",
    } <= panel_titles

    panels_by_title = {
        panel["spec"]["title"]: panel for panel in dashboard["spec"]["elements"].values()
    }

    def thresholds(title):
        return [
            step["value"]
            for step in panels_by_title[title]["spec"]["vizConfig"]["spec"]
            ["fieldConfig"]["defaults"]["thresholds"]["steps"]
        ]

    assert thresholds("Consumo Semanal (kWh)") == [0, 77, 105]
    assert thresholds("Custo Semanal (R$)") == [0, 70, 91]
    assert thresholds("Consumo Mensal (kWh)") == [0, 330, 450]
    assert thresholds("Custo Mensal (R$)") == [0, 300, 390]
