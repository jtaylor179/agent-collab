import sys
import re

def is_palindrome(s: str) -> bool:
    """
    Checks if a string is a palindrome, ignoring case and non-alphanumeric characters.
    Note: Non-ASCII accented characters are stripped and not supported (out of scope).
    Empty strings and all-punctuation inputs are considered palindromes by definition.
    """
    cleaned = re.sub(r"[^a-z0-9]", "", s.lower())
    return cleaned == cleaned[::-1]

def main():
    if len(sys.argv) < 2:
        print("Usage: python check.py <string>", file=sys.stderr)
        sys.exit(1)
    text = " ".join(sys.argv[1:])
    if is_palindrome(text):
        print("YES")
    else:
        print("NO")

if __name__ == "__main__":
    main()
