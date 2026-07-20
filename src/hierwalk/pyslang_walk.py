"""Pyslang-backed hierarchy walk: open only module-index RTL files on the path.

Uses the existing ``module → file(s)`` map (grep_hie / HierarchyGrepSession)
and parses each needed file with pyslang.SyntaxTree — no whole-design compile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.hierarchy_grep import abs_rtl_path, inst_base_name
from hierwalk.inst_scan import coarse_hierarchy_path

LogFn = Optional[Callable[[str], None]]


def _require_pyslang():
    try:
        import pyslang  # noqa: F401
        return pyslang
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "pyslangwalk requires the 'pyslang' package "
            "(pip install pyslang)"
        ) from exc


def _token_text(tok: Any) -> str:
    if tok is None:
        return ""
    val = getattr(tok, "value", None)
    if val is not None and str(val).strip():
        return str(val).strip()
    vt = getattr(tok, "valueText", None)
    if vt is not None and str(vt).strip():
        return str(vt).strip()
    raw = getattr(tok, "rawText", None)
    if raw is not None:
        return str(raw).strip()
    return str(tok).strip()


@dataclass
class PyslangWalkNode:
    path: str
    segment: str
    role: str  # root | inst | leaf
    module: str
    file: str
    kind: Optional[str] = None  # inst | port | signal
    child_module: Optional[str] = None


@dataclass
class PyslangWalkResult:
    ok: bool
    hierarchy: str
    top: str
    error: str = ""
    nodes: Tuple[PyslangWalkNode, ...] = ()
    scoped_files: Tuple[str, ...] = ()
    fail_segment: str = ""


@dataclass
class PyslangWalkSession:
    """Lazy SyntaxTree cache keyed by absolute RTL path."""

    module_index: Dict[str, List[str]]
    defines: Dict[str, str] = field(default_factory=dict)
    _tree_cache: Dict[str, Any] = field(default_factory=dict, repr=False)
    _module_insts: Dict[Tuple[str, str], List[Tuple[str, str]]] = field(
        default_factory=dict, repr=False
    )
    _module_ports: Dict[Tuple[str, str], set] = field(default_factory=dict, repr=False)
    on_log: LogFn = None

    @classmethod
    def from_module_index(
        cls,
        module_index: Mapping[str, Sequence[str]],
        *,
        defines: Optional[Mapping[str, str]] = None,
        on_log: LogFn = None,
    ) -> "PyslangWalkSession":
        idx = {
            str(m): [abs_rtl_path(f) for f in files if f]
            for m, files in module_index.items()
        }
        return cls(module_index=idx, defines=dict(defines or {}), on_log=on_log)

    @classmethod
    def from_grep_session(
        cls,
        grep_session: Any,
        *,
        on_log: LogFn = None,
    ) -> "PyslangWalkSession":
        defines = dict(getattr(grep_session, "defines", None) or {})
        return cls.from_module_index(
            grep_session.module_index,
            defines=defines,
            on_log=on_log,
        )

    def _log(self, msg: str) -> None:
        if self.on_log is not None:
            self.on_log(msg)

    def _syntax_tree(self, fpath: str) -> Any:
        # Cache key includes define fingerprint so ifdef-visible AST is stable.
        defs_key = ",".join(f"{k}={v}" for k, v in sorted(self.defines.items()))
        key = f"{abs_rtl_path(fpath)}|{defs_key}"
        hit = self._tree_cache.get(key)
        if hit is not None:
            return hit
        pyslang = _require_pyslang()
        path = Path(key.split("|", 1)[0])
        if not path.is_file():
            raise FileNotFoundError(str(path))
        self._log(f"pyslangwalk open file={path.name} defines={len(self.defines)}")
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise FileNotFoundError(str(path)) from exc
        # Apply compile defines so `` `ifdef ZZ_REAL_IFDEF `` instances are visible.
        text = raw
        if self.defines:
            from hierwalk.preprocess import (
                apply_ifdef_filter,
                strip_comments_for_instance_scan,
            )

            text = apply_ifdef_filter(
                strip_comments_for_instance_scan(raw),
                self.defines,
            )
        tree = pyslang.syntax.SyntaxTree.fromText(text, path=str(path))
        self._tree_cache[key] = tree
        return tree

    def _module_decls_in_file(self, fpath: str) -> List[Tuple[str, Any]]:
        tree = self._syntax_tree(fpath)
        out: List[Tuple[str, Any]] = []
        root = tree.root
        root_kind = str(getattr(root, "kind", ""))
        # fromText on a single-module unit may root at ModuleDeclaration.
        if "ModuleDeclaration" in root_kind:
            header = getattr(root, "header", None)
            name = _token_text(getattr(header, "name", None)) if header else ""
            if name:
                out.append((name, root))
            return out
        members = getattr(root, "members", None) or ()
        for mem in members:
            kind = str(getattr(mem, "kind", ""))
            if "ModuleDeclaration" not in kind:
                continue
            header = getattr(mem, "header", None)
            name = _token_text(getattr(header, "name", None)) if header else ""
            if name:
                out.append((name, mem))
        return out

    def _find_module_decl(
        self,
        module: str,
        fpath: str,
    ) -> Optional[Any]:
        want = inst_base_name(module)
        for name, decl in self._module_decls_in_file(fpath):
            if inst_base_name(name) == want:
                return decl
        return None

    def _instances_in_module(
        self,
        module: str,
        fpath: str,
    ) -> List[Tuple[str, str]]:
        """Return ``(inst_name, child_module)`` pairs from pyslang syntax."""
        cache_key = (inst_base_name(module), abs_rtl_path(fpath))
        cached = self._module_insts.get(cache_key)
        if cached is not None:
            return list(cached)

        decl = self._find_module_decl(module, fpath)
        pairs: List[Tuple[str, str]] = []
        if decl is None:
            self._module_insts[cache_key] = pairs
            return pairs

        for mem in getattr(decl, "members", None) or ():
            kind = str(getattr(mem, "kind", ""))
            if "HierarchyInstantiation" not in kind:
                continue
            cell = _token_text(getattr(mem, "type", None))
            if not cell:
                continue
            for inst in getattr(mem, "instances", None) or ():
                d = getattr(inst, "decl", None)
                iname = _token_text(getattr(d, "name", None)) if d is not None else ""
                if not iname:
                    continue
                base = inst_base_name(iname)
                pairs.append((base, cell))
        self._module_insts[cache_key] = pairs
        return list(pairs)

    def _ports_in_module(self, module: str, fpath: str) -> set:
        cache_key = (inst_base_name(module), abs_rtl_path(fpath))
        cached = self._module_ports.get(cache_key)
        if cached is not None:
            return set(cached)

        names: set = set()
        decl = self._find_module_decl(module, fpath)
        if decl is None:
            self._module_ports[cache_key] = names
            return names

        header = getattr(decl, "header", None)
        ports = getattr(header, "ports", None) if header is not None else None
        # ANSI port list: walk tokens/nodes for identifiers heuristically
        if ports is not None:
            text = str(ports)
            # Prefer structured visit
            try:
                for node in getattr(ports, "ports", None) or getattr(ports, "members", None) or ():
                    n = getattr(node, "name", None) or getattr(node, "declarator", None)
                    t = _token_text(n) if n is not None else ""
                    if t:
                        names.add(inst_base_name(t))
            except Exception:
                pass
        # Also scan body PortDeclaration
        for mem in getattr(decl, "members", None) or ():
            kind = str(getattr(mem, "kind", ""))
            if "PortDeclaration" in kind or "Port" in kind:
                for a in ("declarators", "names", "ports"):
                    if not hasattr(mem, a):
                        continue
                    try:
                        for d in getattr(mem, a) or ():
                            n = getattr(d, "name", None) or d
                            t = _token_text(n)
                            if t:
                                names.add(inst_base_name(t))
                    except TypeError:
                        n = getattr(mem, "name", None)
                        t = _token_text(n)
                        if t:
                            names.add(inst_base_name(t))
        self._module_ports[cache_key] = names
        return set(names)

    def lookup_module_files(self, module: str) -> List[str]:
        """All candidate RTL files for *module*, decoy/stub last."""
        files = self.module_index.get(module) or self.module_index.get(
            inst_base_name(module)
        )
        if not files:
            want = inst_base_name(module).lower()
            for k, v in self.module_index.items():
                if k.lower() == want and v:
                    files = list(v)
                    break
        if not files:
            return []
        return _order_module_files([abs_rtl_path(f) for f in files if f])

    def lookup_module_file(self, module: str) -> Optional[str]:
        files = self.lookup_module_files(module)
        return files[0] if files else None

    def _find_instance(
        self,
        module: str,
        seg: str,
        *,
        prefer_file: str = "",
    ) -> Optional[Tuple[str, str]]:
        """
        Find ``seg`` instance under *module*.

        Tries all module-index files (prefer_file first) so multi-definition
        modules (real vs decoy/stub) resolve to a body that actually has the inst.
        Returns ``(child_module, parent_file_used)``.
        """
        files = self.lookup_module_files(module)
        if not files:
            return None
        if prefer_file:
            pref = abs_rtl_path(prefer_file)
            files = [pref] + [f for f in files if f != pref]
        want = inst_base_name(seg)
        for fpath in files:
            try:
                insts = self._instances_in_module(module, fpath)
            except (OSError, FileNotFoundError):
                continue
            for iname, cmod in insts:
                if iname == want or iname.lower() == want.lower():
                    return cmod, fpath
        return None

    def _leaf_in_module(
        self,
        module: str,
        seg: str,
        *,
        prefer_file: str = "",
    ) -> Optional[Tuple[str, str]]:
        """Return ``(kind, file)`` if *seg* is port/signal in some body of *module*."""
        files = self.lookup_module_files(module)
        if prefer_file:
            pref = abs_rtl_path(prefer_file)
            files = [pref] + [f for f in files if f != pref]
        want = inst_base_name(seg)
        import re

        for fpath in files:
            try:
                ports = self._ports_in_module(module, fpath)
            except (OSError, FileNotFoundError):
                continue
            if want in ports:
                return "port", fpath
            try:
                text = Path(fpath).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # Restrict word search to this module's declaration span when possible
            if re.search(rf"\b{re.escape(want)}\b", text) is not None:
                return "signal", fpath
        return None

    def resolve(
        self,
        spec: str,
        *,
        top: str,
    ) -> PyslangWalkResult:
        """
        Walk ``top.u_a.u_b.port`` using pyslang only on path-relevant files.
        """
        top_name = inst_base_name(top.strip())
        raw = coarse_hierarchy_path(str(spec or "").strip())
        parts = [inst_base_name(p) for p in raw.split(".") if p]
        if not parts:
            return PyslangWalkResult(
                ok=False, hierarchy="", top=top_name, error="empty hierarchy"
            )
        if parts[0] != top_name:
            parts = [top_name, *parts]

        top_file = self.lookup_module_file(top_name)
        if not top_file:
            return PyslangWalkResult(
                ok=False,
                hierarchy=".".join(parts),
                top=top_name,
                error=f"top module {top_name!r} not in module index",
            )

        nodes: List[PyslangWalkNode] = [
            PyslangWalkNode(
                path=top_name,
                segment=top_name,
                role="root",
                module=top_name,
                file=top_file,
            )
        ]
        scoped = {top_file}
        current_mod = top_name
        current_file = top_file

        for i, seg in enumerate(parts[1:], start=1):
            is_last = i == len(parts) - 1
            found = self._find_instance(current_mod, seg, prefer_file=current_file)

            if found is not None:
                child_mod, parent_file = found
                current_file = parent_file
                scoped.add(parent_file)
                # Prefer child body that declares the next hop when possible
                child_file = self.lookup_module_file(child_mod) or current_file
                if child_file:
                    scoped.add(child_file)
                path = ".".join(parts[: i + 1])
                nodes.append(
                    PyslangWalkNode(
                        path=path,
                        segment=seg,
                        role="leaf" if is_last else "inst",
                        module=current_mod,
                        file=current_file,
                        kind="inst" if is_last else None,
                        child_module=child_mod,
                    )
                )
                if is_last:
                    return PyslangWalkResult(
                        ok=True,
                        hierarchy=".".join(parts),
                        top=top_name,
                        nodes=tuple(nodes),
                        scoped_files=tuple(sorted(scoped)),
                    )
                current_mod = child_mod
                current_file = child_file or current_file
                continue

            # Last hop may be port/signal in current module (try all file defs)
            if is_last:
                leaf = self._leaf_in_module(
                    current_mod, seg, prefer_file=current_file
                )
                if leaf is not None:
                    kind, fpath = leaf
                    scoped.add(fpath)
                    path = ".".join(parts[: i + 1])
                    nodes.append(
                        PyslangWalkNode(
                            path=path,
                            segment=seg,
                            role="leaf",
                            module=current_mod,
                            file=fpath,
                            kind=kind,
                        )
                    )
                    return PyslangWalkResult(
                        ok=True,
                        hierarchy=".".join(parts),
                        top=top_name,
                        nodes=tuple(nodes),
                        scoped_files=tuple(sorted(scoped)),
                    )
                return PyslangWalkResult(
                    ok=False,
                    hierarchy=".".join(parts[: i + 1]),
                    top=top_name,
                    error=(
                        f"pyslangwalk: {seg!r} not an instance/port in "
                        f"module {current_mod!r}"
                    ),
                    nodes=tuple(nodes),
                    scoped_files=tuple(sorted(scoped)),
                    fail_segment=seg,
                )

            tried = ",".join(Path(f).name for f in self.lookup_module_files(current_mod)[:4])
            return PyslangWalkResult(
                ok=False,
                hierarchy=".".join(parts[: i + 1]),
                top=top_name,
                error=(
                    f"pyslangwalk: instance {seg!r} not found in module "
                    f"{current_mod!r} (tried {tried or Path(current_file).name})"
                ),
                nodes=tuple(nodes),
                scoped_files=tuple(sorted(scoped)),
                fail_segment=seg,
            )

        return PyslangWalkResult(
            ok=True,
            hierarchy=".".join(parts),
            top=top_name,
            nodes=tuple(nodes),
            scoped_files=tuple(sorted(scoped)),
        )


def _order_module_files(files: Sequence[str]) -> List[str]:
    """Prefer real RTL over decoy/stub/fake multi-definitions."""

    def score(path: str) -> Tuple[int, str]:
        name = Path(path).name.lower()
        s = 0
        if "decoy" in name:
            s += 100
        if "stub" in name:
            s += 50
        if "fake" in name:
            s += 40
        if name.startswith("dw_"):
            s += 30
        return (s, name)

    return sorted((abs_rtl_path(f) for f in files if f), key=score)


def result_to_gate_style_dict(result: PyslangWalkResult) -> Dict[str, Any]:
    """Shape compatible with hierarchy_grep resolve dict / tree insert."""
    nodes = []
    for n in result.nodes:
        d: Dict[str, Any] = {
            "segment": n.segment,
            "role": n.role,
            "module": n.module,
            "file": n.file,
            "hit_file": n.file,
            "found": True,
        }
        if n.kind:
            d["kind"] = n.kind
        if n.child_module:
            d["child_module"] = n.child_module
            d["child_decl_file"] = n.file
        nodes.append(d)
    return {
        "ok": result.ok,
        "top": result.top,
        "hierarchy": result.hierarchy,
        "hierarchy_input": result.hierarchy,
        "error": result.error,
        "ambiguous": False,
        "nodes": nodes,
        "candidates": [],
        "pyslangwalk": True,
        "scoped_files": list(result.scoped_files),
    }


def flat_rows_from_pyslang_result(result: PyslangWalkResult) -> List[Any]:
    """Convert walk nodes to FlatRow list for text-COI seed (inst chain only)."""
    from hierwalk.models import FlatRow

    rows: List[Any] = []
    for n in result.nodes:
        if n.role == "leaf" and n.kind in ("port", "signal"):
            continue
        parent = ".".join(n.path.split(".")[:-1]) or None
        if n.role == "root":
            parent = None
        depth = 0 if parent is None else n.path.count(".")
        module = n.child_module or n.module
        rows.append(
            FlatRow(
                full_path=n.path,
                inst_leaf=n.segment,
                module=module,
                depth=depth,
                parent_path=parent,
                file=n.file,
                refine_status="pyslangwalk",
            )
        )
    return rows
