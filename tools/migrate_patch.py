#!/usr/bin/env python3
"""Remove `with patch("ac_infinity_mcp.server.aci_client", mock_client):` blocks.

Run from repo root:
  python3 tools/migrate_patch.py tests/common/test_server.py
"""
import re
import sys

PATTERN = re.compile(
    r'^( +)with patch\("ac_infinity_mcp\.server\.aci_client", mock_client\):\n',
    re.MULTILINE,
)


def migrate(path: str) -> None:
    text = open(path).read()
    lines = text.splitlines(keepends=True)
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = PATTERN.match(line)
        if m:
            indent = m.group(1)  # indentation of the `with` line
            dedent = "    "       # one level = 4 spaces
            i += 1               # skip the `with` line
            while i < len(lines):
                next_line = lines[i]
                # End of block: blank line or line whose indent is <= with-line indent
                if next_line.strip() == "" or (
                    next_line[0] != " "
                    or len(next_line) - len(next_line.lstrip()) <= len(indent)
                ):
                    break
                # Dedent by 4 spaces
                if next_line.startswith(indent + dedent):
                    out.append(next_line[len(dedent):])
                else:
                    out.append(next_line)
                i += 1
        else:
            out.append(line)
            i += 1
    open(path, "w").write("".join(out))
    print(f"Migrated {path}")


if __name__ == "__main__":
    for path in sys.argv[1:]:
        migrate(path)
