import pytest

from scripts.byoc_pr.validation import is_empty_form, parse_int, validate

VALID = {
    "username": "alice-hpc",
    "password": "AlicePuls4rPassw0rd",
    "max_accepted_cores": "16",
    "max_accepted_mem": 64,
    "min_accepted_gpus": 0.0,
    "max_accepted_gpus": "1",
}


def test_valid_form_becomes_request():
    request, errors = validate("id1", "alice", VALID)
    assert errors == []
    assert request.username == "alice-hpc"
    assert (request.max_accepted_cores, request.max_accepted_mem) == (16, 64)
    assert (request.min_accepted_gpus, request.max_accepted_gpus) == (0, 1)
    assert "AlicePuls4rPassw0rd" not in repr(request)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("username", "Al", "username: must match"),
        ("username", "UPPER", "username: must match"),
        ("username", "a" * 33, "username: must match"),
        ("username", None, "username: must match"),
        ("password", "short1", "password: must be alphanumeric"),
        ("password", "has space 12345", "password: must be alphanumeric"),
        ("password", None, "password: must be alphanumeric"),
        ("max_accepted_cores", "0", "max_accepted_cores: must be a whole number from 1 to 1024"),
        ("max_accepted_mem", "lots", "max_accepted_mem: must be a whole number"),
        ("max_accepted_gpus", 65, "max_accepted_gpus: must be a whole number from 0 to 64"),
        ("min_accepted_gpus", 2, "min_accepted_gpus: must not be larger than max_accepted_gpus"),
    ],
)
def test_invalid_field_is_reported_without_its_value(field, value, message):
    request, errors = validate("id1", "alice", {**VALID, field: value})
    assert request is None
    assert any(error.startswith(message) for error in errors)
    if value is not None and field == "password":
        assert not any(str(value) in error for error in errors)


def test_all_errors_are_collected():
    _, errors = validate("id1", "carol", {})
    assert len(errors) == 6


@pytest.mark.parametrize(
    ("value", "expected"),
    [(4, 4), ("4", 4), (" 8 ", 8), (4.0, 4), (4.5, None), ("4.0", None), (True, None), ([], None)],
)
def test_parse_int(value, expected):
    assert parse_int(value) == expected


def test_empty_form():
    assert is_empty_form({"username": " ", "password": None})
    assert not is_empty_form({"max_accepted_cores": 0})
