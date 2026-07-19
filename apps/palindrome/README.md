# Palindrome checker command-line app.

## Running the application
You can run the application by passing a string as an argument:
```bash
python3 apps/palindrome/check.py "A man, a plan, a canal: Panama"
```

## Running tests
You can run the tests from the repository root:
```bash
python3 -m unittest apps/palindrome/test_check.py
```
Or from within the `apps/palindrome` directory:
```bash
cd apps/palindrome && python3 -m unittest test_check
```
