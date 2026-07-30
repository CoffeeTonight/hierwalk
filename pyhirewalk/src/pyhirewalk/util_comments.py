"""
SystemVerilog / C-style comment stripping (single pass).

Do NOT strip /* */ then // (or reverse): order-dependent bugs.

  /*//*/   // inside block — must NOT become line-comment
  ///*     // line comment — must NOT open a block

Use a left-to-right state machine (normal | line | block | string).
"""

from __future__ import annotations


def strip_sv_comments(text: str) -> str:
    """
    Remove // line comments and /* */ block comments.

    - Block comments become a single space (token separation).
    - Newlines inside block comments are kept (line structure).
    - Double-quoted strings are preserved (// or /* inside strings ignored).
    - Block comments are non-nested (SV/C rules).
    """
    out: list[str] = []
    i = 0
    n = len(text)
    # 0=normal 1=line 2=block 3=string
    st = 0
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if st == 0:  # normal
            if c == "/" and nxt == "/":
                st = 1
                i += 2
                continue
            if c == "/" and nxt == "*":
                st = 2
                i += 2
                out.append(" ")
                continue
            if c == '"':
                st = 3
                out.append(c)
                i += 1
                continue
            out.append(c)
            i += 1
            continue

        if st == 1:  # // line comment
            if c == "\n":
                st = 0
                out.append("\n")
            i += 1
            continue

        if st == 2:  # /* block */
            if c == "*" and nxt == "/":
                st = 0
                i += 2
                out.append(" ")
                continue
            if c == "\n":
                out.append("\n")
            i += 1
            continue

        # string "
        out.append(c)
        if c == "\\" and i + 1 < n:
            out.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            st = 0
        i += 1

    return "".join(out)
