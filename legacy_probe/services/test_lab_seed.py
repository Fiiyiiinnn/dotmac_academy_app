from app.services.lab_seed import generate_seed, interpolate


def test_seed_deterministic():
    spec = {"mgmt_octet": {"type": "int", "min": 2, "max": 250}}
    a = generate_seed(spec, attempt_id=42)
    b = generate_seed(spec, attempt_id=42)
    assert a == b and 2 <= a["mgmt_octet"] <= 250


def test_seed_int_within_bounds():
    spec = {"o": {"type": "int", "min": 2, "max": 9}}
    for attempt_id in range(50):
        seed = generate_seed(spec, attempt_id=attempt_id)
        assert 2 <= seed["o"] <= 9


def test_seed_choice():
    spec = {"vlan": {"type": "choice", "options": [100, 200, 300]}}
    a = generate_seed(spec, attempt_id=7)
    b = generate_seed(spec, attempt_id=7)
    assert a == b and a["vlan"] in [100, 200, 300]


def test_seed_multi_key_deterministic():
    spec = {
        "o": {"type": "int", "min": 1, "max": 254},
        "vlan": {"type": "choice", "options": ["a", "b", "c"]},
    }
    assert generate_seed(spec, attempt_id=99) == generate_seed(spec, attempt_id=99)


def test_interpolate():
    assert interpolate("ip 10.9.0.{{mgmt_octet}}/24", {"mgmt_octet": 7}) == "ip 10.9.0.7/24"


def test_interpolate_plan_case():
    assert interpolate("10.0.0.{{o}}/24", {"o": 7}) == "10.0.0.7/24"


def test_interpolate_multiple_keys():
    text = "vlan {{vlan}} on 10.0.{{o}}.1"
    assert interpolate(text, {"vlan": 100, "o": 5}) == "vlan 100 on 10.0.5.1"
