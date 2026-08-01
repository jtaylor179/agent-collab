import os
import pytest
from app import app


@pytest.fixture(autouse=True)
def clean_state():
    """Fixture to isolate and clean in-memory state between tests."""
    app.config["TESTING"] = True
    
    # Try to import app.py as a module to clean any in-memory list or dictionary
    try:
        import app as app_module
        # Clean common names for in-memory todo collections
        for attr in ["todos", "todo_list", "TASKS", "tasks", "TODO_LIST"]:
            if hasattr(app_module, attr):
                val = getattr(app_module, attr)
                if isinstance(val, list):
                    val.clear()
                elif isinstance(val, dict):
                    val.clear()
    except ImportError:
        pass


@pytest.fixture
def client():
    with app.test_client() as c:
        yield c


def test_index_get(client):
    """GET / should load successfully and contain Todo List markers."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Todo" in resp.data or b"Task" in resp.data


def test_add_todo(client):
    """POST /add with a valid title adds a todo and redirects to index."""
    resp = client.post("/add", data={"title": "Buy milk"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Buy milk" in resp.data
    # Verify initial task state is incomplete/pending
    assert "○".encode("utf-8") in resp.data
    assert "✓".encode("utf-8") not in resp.data


def test_add_todo_blank_title(client):
    """POST /add with a blank or empty title should return 400 Bad Request."""
    resp = client.post("/add", data={"title": ""})
    assert resp.status_code == 400

    resp = client.post("/add", data={"title": "   "})
    assert resp.status_code == 400


def test_add_todo_missing_title(client):
    """POST /add with missing title field should return 400 Bad Request."""
    resp = client.post("/add", data={})
    assert resp.status_code == 400


def test_toggle_todo(client):
    """POST /toggle/<task_id> toggles the task state between completed and incomplete."""
    # Add a task (should get ID 1)
    client.post("/add", data={"title": "Exercise"}, follow_redirects=True)

    # Verify task starts as incomplete/pending
    resp = client.get("/")
    assert b"Exercise" in resp.data
    assert "○".encode("utf-8") in resp.data
    assert "✓".encode("utf-8") not in resp.data

    # Toggle to completed
    resp = client.post("/toggle/1", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Exercise" in resp.data
    # Should now show as completed (either in styling, class, or text)
    assert "✓".encode("utf-8") in resp.data
    assert "○".encode("utf-8") not in resp.data

    # Toggle back to incomplete
    resp = client.post("/toggle/1", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Exercise" in resp.data
    assert "○".encode("utf-8") in resp.data
    assert "✓".encode("utf-8") not in resp.data


def test_delete_todo(client):
    """POST /delete/<task_id> deletes the task."""
    # Add a task (should get ID 1)
    client.post("/add", data={"title": "Read book"}, follow_redirects=True)

    # Verify task exists
    resp = client.get("/")
    assert b"Read book" in resp.data

    # Delete the task
    resp = client.post("/delete/1", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Read book" not in resp.data


def test_toggle_nonexistent_todo(client):
    """POST /toggle/<task_id> for a nonexistent task should return 404 Not Found."""
    resp = client.post("/toggle/999")
    assert resp.status_code == 404


def test_delete_nonexistent_todo(client):
    """POST /delete/<task_id> for a nonexistent task should return 404 Not Found."""
    resp = client.post("/delete/999")
    assert resp.status_code == 404


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
