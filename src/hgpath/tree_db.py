"""Tree DB: hierarchy path → inst chain + leaf + filepath per node."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from hierwalk.hierarchy_grep import abs_rtl_path

TREE_JSON_NAME = "hgpath_tree.json"
TREE_SCHEMA_VERSION = 1

LogFn = Optional[Callable[[str], None]]


def resolve_tree_db_path(work_dir: str | Path) -> Path:
    return Path(work_dir).expanduser().resolve() / TREE_JSON_NAME


@dataclass
class TreeNode:
    path: str
    segment: str
    role: str
    module: str
    file: str
    kind: Optional[str] = None
    child_module: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "path": self.path,
            "segment": self.segment,
            "role": self.role,
            "module": self.module,
            "file": self.file,
        }
        if self.kind:
            out["kind"] = self.kind
        if self.child_module:
            out["child_module"] = self.child_module
        return out

    @classmethod
    def from_resolve_node(
        cls,
        *,
        path: str,
        node: Mapping[str, Any],
    ) -> TreeNode:
        role = str(node.get("role", ""))
        module = str(node.get("module", ""))
        file_path = abs_rtl_path(
            node.get("child_decl_file")
            or node.get("hit_file")
            or node.get("file")
            or ""
        )
        if not file_path:
            raise ValueError(f"tree node missing file path={path!r}")
        kind = node.get("kind")
        if kind is not None:
            kind = str(kind)
        child_mod = node.get("child_module")
        return cls(
            path=path,
            segment=str(node.get("segment", "")),
            role=role,
            module=module,
            file=file_path,
            kind=kind,
            child_module=str(child_mod) if child_mod else None,
        )


@dataclass
class TreeEntry:
    key: str
    ok: bool
    ambiguous: bool
    error: str
    port_tail: str
    nodes: Tuple[TreeNode, ...]
    scoped_files: Tuple[str, ...]
    resolve_result: Dict[str, Any] = field(repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "ok": self.ok,
            "ambiguous": self.ambiguous,
            "error": self.error,
            "port_tail": self.port_tail,
            "nodes": [n.to_dict() for n in self.nodes],
            "scoped_files": list(self.scoped_files),
        }


def _nodes_from_result(result: Mapping[str, Any]) -> Tuple[TreeNode, ...]:
    raw_nodes = list(result.get("nodes") or [])
    if not raw_nodes:
        return ()
    out: List[TreeNode] = []
    parts: List[str] = []
    for node in raw_nodes:
        seg = str(node.get("segment", ""))
        if not seg:
            continue
        role = str(node.get("role", ""))
        if role == "genblk":
            continue
        if not parts:
            parts = [seg]
        else:
            parts.append(seg)
        path = ".".join(parts)
        out.append(TreeNode.from_resolve_node(path=path, node=node))
    return tuple(out)


def _scoped_from_nodes(nodes: Sequence[TreeNode]) -> Tuple[str, ...]:
    files = {n.file for n in nodes if n.file}
    return tuple(sorted(files))


@dataclass
class TreeDb:
    work_dir: Path
    path: Path
    entries: Dict[str, TreeEntry] = field(default_factory=dict)
    _dirty: bool = field(default=False, repr=False)

    @property
    def node_count(self) -> int:
        return len(self.entries)

    def longest_prefix(self, key: str) -> Tuple[Optional[TreeEntry], int]:
        """Return (entry, shared_hop_count) for longest cached prefix."""
        if not key:
            return None, 0
        if key in self.entries:
            ent = self.entries[key]
            return ent, len(ent.nodes)
        best: Optional[TreeEntry] = None
        best_len = 0
        parts = key.split(".")
        for i in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:i])
            hit = self.entries.get(prefix)
            if hit is not None and hit.ok:
                n = len(hit.nodes)
                if n > best_len:
                    best = hit
                    best_len = n
        return best, best_len

    def get_full(self, key: str) -> Optional[TreeEntry]:
        return self.entries.get(key)

    def insert_result(
        self,
        key: str,
        result: Mapping[str, Any],
        *,
        port_tail: str = "",
    ) -> TreeEntry:
        nodes = _nodes_from_result(result)
        scoped = _scoped_from_nodes(nodes)
        entry = TreeEntry(
            key=key,
            ok=bool(result.get("ok")),
            ambiguous=bool(result.get("ambiguous")),
            error=str(result.get("error", "")),
            port_tail=port_tail,
            nodes=nodes,
            scoped_files=scoped,
            resolve_result=dict(result),
        )
        self.entries[key] = entry
        self._dirty = True
        parts: List[str] = []
        for i, node in enumerate(nodes):
            parts.append(node.segment)
            prefix = ".".join(parts)
            if prefix == key:
                continue
            if prefix not in self.entries or not self.entries[prefix].ok:
                sub = TreeEntry(
                    key=prefix,
                    ok=entry.ok,
                    ambiguous=entry.ambiguous,
                    error=entry.error,
                    port_tail="",
                    nodes=tuple(nodes[: i + 1]),
                    scoped_files=_scoped_from_nodes(nodes[: i + 1]),
                    resolve_result=dict(result),
                )
                self.entries[prefix] = sub
        return entry

    def _payload_text(self) -> str:
        payload = {
            "schema_version": TREE_SCHEMA_VERSION,
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
        }
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self._payload_text(), encoding="utf-8")
        self._dirty = False

    def save_if_changed(self) -> bool:
        """Persist only when entries changed since load or last save."""
        if not self._dirty:
            return False
        self.save()
        return True

    @classmethod
    def load(cls, work_dir: str | Path) -> TreeDb:
        work = Path(work_dir).expanduser().resolve()
        path = resolve_tree_db_path(work)
        db = cls(work_dir=work, path=path, _dirty=False)
        if not path.is_file():
            return db
        raw = json.loads(path.read_text(encoding="utf-8"))
        for key, blob in (raw.get("entries") or {}).items():
            nodes = tuple(
                TreeNode(
                    path=n["path"],
                    segment=n["segment"],
                    role=n["role"],
                    module=n["module"],
                    file=n["file"],
                    kind=n.get("kind"),
                    child_module=n.get("child_module"),
                )
                for n in blob.get("nodes", [])
            )
            db.entries[key] = TreeEntry(
                key=key,
                ok=bool(blob.get("ok")),
                ambiguous=bool(blob.get("ambiguous")),
                error=str(blob.get("error", "")),
                port_tail=str(blob.get("port_tail", "")),
                nodes=nodes,
                scoped_files=tuple(blob.get("scoped_files", ())),
                resolve_result={},
            )
        return db


def emit_milestone(msg: str, *, on_log: LogFn = None) -> None:
    if on_log is not None:
        on_log(f"hgpath milestone {msg}")