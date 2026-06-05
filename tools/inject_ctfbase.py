#!/usr/bin/env python3
"""
Inject CTFBase knowledge base instructions into agent .md files.

This script adds CTFBase MCP tool usage instructions to your agent files,
enabling them to search a knowledge base of 1300+ CTF writeups during solving.

Usage:
  python3 tools/inject_ctfbase.py agents/custom/

The script is idempotent — running it multiple times won't duplicate content.
"""

import sys
from pathlib import Path

CTFBASE_BLOCK = """
## Knowledge Base (CTFBase)

Before attacking, search the CTFBase knowledge base for similar challenges:

```
mcp_ctfbase_search(query="[technique or pattern, 4-7 words]", category="[category]")
```

If you find a relevant result, read the full writeup:

```
mcp_ctfbase_get_writeup(id="...")
```

The full writeup contains working scripts, payloads, and step-by-step solutions.
Search is semantic — it understands synonyms and related concepts.

Good queries (by technique):
  - "RSA small exponent Coppersmith"
  - "Flask SSTI Jinja2 sandbox escape"
  - "PNG LSB steganography hidden data"

Bad queries:
  - "mycoolctf challenge3" (task name, not technique)
  - "web exploit" (too generic)
""".strip()

MARKER = "## Knowledge Base (CTFBase)"


def inject_ctfbase(agents_dir: Path) -> int:
    """Inject CTFBase instructions into all .md files in agents_dir.

    Returns number of files modified.
    """
    modified = 0
    for md_file in sorted(agents_dir.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")

        # Skip if already injected
        if MARKER in content:
            print(f"  skip: {md_file.name} (already has CTFBase block)")
            continue

        # Inject before the last line (usually a motivational closer)
        content = content.rstrip() + "\n\n" + CTFBASE_BLOCK + "\n"

        md_file.write_text(content, encoding="utf-8")
        print(f"  added: {md_file.name}")
        modified += 1

    return modified


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 tools/inject_ctfbase.py <agents-dir>")
        print("Example: python3 tools/inject_ctfbase.py agents/custom/")
        sys.exit(1)

    agents_dir = Path(sys.argv[1])
    if not agents_dir.exists():
        print(f"ERROR: Directory not found: {agents_dir}")
        sys.exit(1)

    md_files = list(agents_dir.glob("*.md"))
    if not md_files:
        print(f"ERROR: No .md files found in {agents_dir}")
        sys.exit(1)

    print(f"Injecting CTFBase instructions into {len(md_files)} agent files...")
    modified = inject_ctfbase(agents_dir)
    print(f"\nDone. {modified} files modified.")


if __name__ == "__main__":
    main()
