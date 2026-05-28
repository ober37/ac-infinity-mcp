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
    lines = open(path).readlines()
    out = []
    i = 0
    removed = 0
    while i < len(lines):
        line = lines[i]
        m = PATTERN.match(line)
        if m:
            removed += 1
            indent = m.group(1)   # indentation of the `with` line
            dedent = "    "        # one level = 4 spaces
            i += 1                 # skip the `with` line
            # Collect pending blank lines — only emit them if the block continues
            pending_blanks: list[str] = []
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip() == "":
                    # Blank line inside block — buffer it; decide later
                    pending_blanks.append(next_line)
                    i += 1
                    continue
                # Non-blank: check if still inside the block
                stripped = next_line.lstrip()
                current_indent = len(next_line) - len(stripped)
                if current_indent <= len(indent):
                    # Exited the block — flush buffered blanks and stop
                    out.extend(pending_blanks)
                    pending_blanks = []
                    break
                # Still inside the block — flush buffered blanks and dedent
                out.extend(pending_blanks)
                pending_blanks = []
                if next_line.startswith(indent + dedent):
                    out.append(next_line[len(dedent):])
                else:
                    out.append(next_line)
                i += 1
            # Any trailing buffered blanks belong after the block
            out.extend(pending_blanks)
        else:
            out.append(line)
            i += 1
    open(path, "w").writelines(out)
    print(f"Removed {removed} patch wrappers from {path}")


if __name__ == "__main__":
    for path in sys.argv[1:]:
        migrate(path)
