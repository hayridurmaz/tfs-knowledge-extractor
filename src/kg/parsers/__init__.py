"""Parsers turn one file's bytes into a JSON-serializable dict.

Parser output must depend only on file content (never on its path), because results are cached by blob hash.
Bump PARSER_VERSION whenever any parser's output shape changes.
"""

PARSER_VERSION = 1
