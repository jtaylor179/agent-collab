import os
import pytest
from app import app, calculate


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_add():
    assert calculate(2, 3, "add") == 5


def test_add_negative():
    assert calculate(-1, -2, "add") == -3


def test_divide_by_zero():
    with pytest.raises(ZeroDivisionError):
        calculate(10, 0, "div")


def test_index_get(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Calculator" in resp.data


def test_index_post_add(client):
    resp = client.post("/", data={"a": "2", "b": "3", "op": "add"})
    assert b"5.0" in resp.data


def test_index_post_div_zero(client):
    resp = client.post("/", data={"a": "1", "b": "0", "op": "div"})
    assert b"Cannot divide by zero" in resp.data


def test_sub():
    assert calculate(10, 3, "sub") == 7


def test_mul():
    assert calculate(4, 5, "mul") == 20


def test_unknown_op():
    with pytest.raises(ValueError, match="Unknown operation"):
        calculate(1, 2, "mod")


def test_index_post_malformed_input(client):
    resp = client.post("/", data={"a": "abc", "b": "3", "op": "add"})
    assert resp.status_code == 200
    assert b"could not convert" in resp.data.lower() or b"invalid" in resp.data.lower()


def test_index_post_missing_field(client):
    resp = client.post("/", data={"a": "1", "op": "add"})
    assert resp.status_code == 200
    body = resp.data.decode()
    # The error message should mention the missing field by name
    assert "Missing field: b" in body
    # No result should be shown
    assert "Result:" not in body


def test_flask_debug_default():
    """FLASK_DEBUG defaults to off when the env var is unset."""
    from unittest.mock import patch

    env = os.environ.copy()
    env.pop("FLASK_DEBUG", None)
    with patch.dict(os.environ, env, clear=True), \
         patch.object(app, "run") as mock_run:
        from app import main
        main()
        mock_run.assert_called_once_with(debug=False)


def test_flask_debug_override():
    """FLASK_DEBUG=1 enables debug mode."""
    from unittest.mock import patch

    env = os.environ.copy()
    env["FLASK_DEBUG"] = "1"
    with patch.dict(os.environ, env, clear=True), \
         patch.object(app, "run") as mock_run:
        from app import main
        main()
        mock_run.assert_called_once_with(debug=True)
