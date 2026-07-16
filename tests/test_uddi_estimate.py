import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("nios_grid_capacity", ROOT / "nios_grid_capacity.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_flatten_capacity_adds_uddi_estimate():
    capacity = {
        "name": "member1",
        "role": "grid_master",
        "hardware_type": "IB-1400",
        "max_capacity": 1000,
        "total_objects": 200,
        "percent_used": 20,
        "object_counts": [
            {"type_name": "Host", "count": 20},
            {"type_name": "Network", "count": 5},
            {"type_name": "Zone", "count": 10},
            {"type_name": "Fixed Address", "count": 15},
        ],
    }

    summary, object_counts = module.flatten_capacity(capacity)

    assert summary["cap_uddi_ddi_objects"] == 15
    assert summary["cap_uddi_active_ip_objects"] == 35
    assert summary["cap_uddi_total_objects"] == 50
    assert object_counts["obj_Host"] == 20


def test_select_output_columns_hides_verbose_fields_by_default():
    selected, renamed = module.select_output_columns(["host_name", "mgmt_enabled"], False)

    assert selected == ["host_name"]
    assert renamed == ["host_name"]
