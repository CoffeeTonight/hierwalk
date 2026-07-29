"""
EDA filelist environment-variable expansion.

How EDA tools (VCS / Xcelium / Questa-class command files) use env vars
-------------------------------------------------------------------------
A simulator is started from a shell that already has project roots exported::

    export PROJ=/proj/chip
    export RTL_ROOT=$PROJ/rtl
    vcs -f $PROJ/filelist.f +define+SYNTHESIS …

Inside ``filelist.f`` (and nested ``-f``/``-F`` lists) the **same process
environment** is used to expand path tokens **before** path resolution:

    -f  $PROJ/ip/uart/files.f
    -F  $PROJ/tb/tb.f
    +incdir+$RTL_ROOT/include+$RTL_ROOT/vip
    -y  $TECH_LIB/stdlib
    -v  $TECH_LIB/cells.v
    $RTL_ROOT/top.sv

Syntax (what real tools document / parse):

    $NAME       shell / VCS-class / expandvars — most common in .f
    $NAME/...   same token then path separator (not a separate form)
    ${NAME}     braced; needed for ${PROJ}_build or ${PROJ}foo
    $(NAME)     Verilator -f docs; several filelist parsers (make-like)

    {$NAME}     NOT a standard EDA env form (often a typo for ${NAME}).
                We do not treat braces-outside-dollar as env syntax.

Not supported: ``${NAME:-default}``, command substitution, Windows ``%NAME%``.

Expansion order for each token:

    1. strip quotes
    2. expand $NAME / ${NAME} from env map (JSON env overrides shell)
    3. if relative → join with content base (-f dir or -F index_cwd)
    4. resolve absolute path

Unset variables are left literally (like many shells / expandvars) and
reported so company runs fail loudly instead of inventing paths.

pyhirewalk JSON ``env`` block is how you inject those exports without a login
shell — same names the .f already references.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Mapping, Optional, Tuple

# $NAME | ${NAME} | $(NAME) — identifier only, not ${NAME:-x} / $(cmd args)
# Order: braced/paren forms first so `$` alone is not double-matched.
_ENV_TOKEN_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
    r"|\$\(([A-Za-z_][A-Za-z0-9_]*)\)"
    r"|\$([A-Za-z_][A-Za-z0-9_]*)"
)


def build_env_map(overrides: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Process environment with optional overrides (JSON env wins)."""
    env_map = dict(os.environ)
    if overrides:
        for k, v in overrides.items():
            if k is None:
                continue
            env_map[str(k)] = "" if v is None else str(v)
    return env_map


def find_env_refs(text: str) -> List[str]:
    """Return unique env names referenced as $NAME / ${NAME} / $(NAME)."""
    names: List[str] = []
    seen = set()
    for m in _ENV_TOKEN_RE.finditer(text):
        name = m.group(1) or m.group(2) or m.group(3)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def expand_eda_env(
    text: str,
    env: Optional[Mapping[str, str]] = None,
    *,
    keep_unset: bool = True,
) -> Tuple[str, List[str]]:
    """
    Expand EDA-style ``$VAR`` / ``${VAR}`` / ``$(VAR)`` in *text*.

    Returns ``(expanded_text, unresolved_names)``.

    * Unresolved names are left as the original token when ``keep_unset``.
    * Identifier boundaries: ``$PROJ`` does **not** steal prefix of ``$PROJECT``.
    * ``{$VAR}`` is **not** expanded as a unit (non-standard); inner ``$VAR``
      still matches if present as ``$VAR`` alone inside braces.
    """
    env_map = build_env_map(env)
    unresolved: List[str] = []
    seen_unresolved = set()

    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        assert name is not None
        if name in env_map:
            return env_map[name]
        if name not in seen_unresolved:
            seen_unresolved.add(name)
            unresolved.append(name)
        return m.group(0) if keep_unset else ""

    return _ENV_TOKEN_RE.sub(repl, text), unresolved


def expand_eda_env_strict(
    text: str,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Expand; raise ValueError if any referenced variable is unset."""
    out, missing = expand_eda_env(text, env, keep_unset=True)
    if missing:
        raise ValueError(
            "unset environment variable(s) in filelist path: "
            + ", ".join(missing)
            + f" (text={text!r})"
        )
    return out
