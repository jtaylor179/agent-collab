from flask import Flask, request, render_template_string, redirect, url_for

app = Flask(__name__)

tasks = []

HTML = """
<!DOCTYPE html>
<html>
<head><title>Todo</title></head>
<body>
<h1>Todo List</h1>
<form method="post" action="/add">
  <input name="title" placeholder="New task" required>
  <button type="submit">Add</button>
</form>
{% if tasks %}
<ul>
{% for task in tasks %}
  <li>
    <form method="post" action="/toggle/{{ task.id }}" style="display:inline">
      <button type="submit">{{ '✓' if task.done else '○' }}</button>
    </form>
    <span style="{{ 'text-decoration:line-through' if task.done else '' }}">{{ task.title }}</span>
    <form method="post" action="/delete/{{ task.id }}" style="display:inline">
      <button type="submit">✕</button>
    </form>
  </li>
{% endfor %}
</ul>
{% else %}
<p>No tasks yet.</p>
{% endif %}
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML, tasks=tasks)


@app.route("/add", methods=["POST"])
def add():
    title = request.form.get("title", "").strip()
    if not title:
        return "Title cannot be empty", 400
    task_id = (max(t["id"] for t in tasks) + 1) if tasks else 1
    tasks.append({"id": task_id, "title": title, "done": False})
    return redirect(url_for("index"))


@app.route("/toggle/<int:task_id>", methods=["POST"])
def toggle(task_id):
    for task in tasks:
        if task["id"] == task_id:
            task["done"] = not task["done"]
            return redirect(url_for("index"))
    return "Task not found", 404


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    for i, task in enumerate(tasks):
        if task["id"] == task_id:
            tasks.pop(i)
            return redirect(url_for("index"))
    return "Task not found", 404


def main():
    import os
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)


if __name__ == "__main__":
    main()
