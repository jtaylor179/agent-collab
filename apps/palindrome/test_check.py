import unittest
from unittest.mock import patch
import io
import sys

try:
    from apps.palindrome.check import is_palindrome, main
except ModuleNotFoundError:
    from check import is_palindrome, main

class TestPalindrome(unittest.TestCase):
    def test_palindrome_cases(self):
        # Case 1: Palindrome with spaces and mixed case
        self.assertTrue(is_palindrome("Race Car"))
        # Case 2: Non-palindrome
        self.assertFalse(is_palindrome("Hello"))
        # Case 3: Canonical palindrome with punctuation and mixed case
        self.assertTrue(is_palindrome("A man, a plan, a canal: Panama"))
        self.assertTrue(is_palindrome("Madam, I'm Adam"))
        # Case 4: Single character
        self.assertTrue(is_palindrome("a"))
        self.assertTrue(is_palindrome("Z"))
        # Case 5: Empty input
        self.assertTrue(is_palindrome(""))
        # Case 6: Punctuation only
        self.assertTrue(is_palindrome("!!!"))
        self.assertTrue(is_palindrome("  .,;?  "))
        # Case 7: Non-ASCII accented characters (stripped/unsupported behavior pin)
        self.assertFalse(is_palindrome("café"))  # Stripped to "caf" -> not palindrome
        self.assertTrue(is_palindrome("caféfac"))  # Stripped to "caffac" -> palindrome

class TestPalindromeCLI(unittest.TestCase):
    @patch('sys.argv', ['check.py'])
    @patch('sys.stderr', new_callable=io.StringIO)
    def test_main_no_args(self, mock_stderr):
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Usage:", mock_stderr.getvalue())

    @patch('sys.argv', ['check.py', ''])
    @patch('sys.stdout', new_callable=io.StringIO)
    def test_main_empty_string_arg(self, mock_stdout):
        # empty string is a palindrome -> YES
        main()
        self.assertEqual(mock_stdout.getvalue().strip(), "YES")

    @patch('sys.argv', ['check.py', 'Race', 'Car'])
    @patch('sys.stdout', new_callable=io.StringIO)
    def test_main_palindrome(self, mock_stdout):
        main()
        self.assertEqual(mock_stdout.getvalue().strip(), "YES")

    @patch('sys.argv', ['check.py', 'Hello'])
    @patch('sys.stdout', new_callable=io.StringIO)
    def test_main_non_palindrome(self, mock_stdout):
        main()
        self.assertEqual(mock_stdout.getvalue().strip(), "NO")

if __name__ == "__main__":
    unittest.main()
