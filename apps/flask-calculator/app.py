from flask import Flask, request, render_template_string

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head><title>Calculator</title></head>
<body>
<h1>Calculator</h1>
<form method="post">
  <input name="a" type="number" step="any" value="{{ a }}" required>
  <select name="op">
    <option value="add" {{ 'selected' if op=='add' }}>+</option>
    <option value="sub" {{ 'selected' if op=='sub' }}>−</option>
    <option value="mul" {{ 'selected' if op=='mul' }}>×</option>
    <option value="div" {{ 'selected' if op=='div' }}>÷</option>
  </select>
  <input name="b" type="number" step="any" value="{{ b }}" required>
  <button type="submit">=</button>
</form>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
{% if result is not none and not error %}<p>Result: {{ result }}</p>{% endif %}
</body>
</html>
"""


def calculate(a, b, op):
    if op == "add":
        return a + b
    elif op == "sub":
        return a - b
    elif op == "mul":
        return a * b
    elif op == "div":
        if b == 0:
            raise ZeroDivisionError("Cannot divide by zero")
        return a / b
    raise ValueError(f"Unknown operation: {op}")


@app.route("/", methods=["GET", "POST"])
def index():
    a, b, op, result, error = "", "", "add", None, None
    if request.method == "POST":
        try:
            a = float(request.form["a"])
            b = float(request.form["b"])
            op = request.form["op"]
            result = calculate(a, b, op)
        except ZeroDivisionError as e:
            error = str(e)
        except ValueError as e:
            error = str(e)
        except KeyError as e:
            error = f"Missing field: {e.args[0]}"
    return render_template_string(HTML, a=a, b=b, op=op, result=result, error=error)


def main():
    import os
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)


if __name__ == "__main__":
    main()
