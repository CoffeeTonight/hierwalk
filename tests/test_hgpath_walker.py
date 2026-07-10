"""hgpath walker LPM cache tests."""

from __future__ import annotations

from pathlib import Path

import hierwalk.hierarchy_grep as hg
from hgpath.flat_db import load_or_build_flat_db
from hgpath.tree_db import TreeDb, resolve_tree_db_path
from hgpath.walker import resolve_with_tree


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p.resolve())


def test_resolve_with_tree_full_lpm_hit(tmp_path: Path, monkeypatch):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
          child u_b ();
        endmodule
        """,
    )
    logs: list[str] = []
    _db, session = load_or_build_flat_db([top_v], top="top", work_dir=tmp_path / "w")
    tree = TreeDb(work_dir=tmp_path / "w", path=resolve_tree_db_path(tmp_path / "w"))

    read_count = 0
    orig = hg._read_text

    def counting_read(path):
        nonlocal read_count
        read_count += 1
        return orig(path)

    monkeypatch.setattr(hg, "_read_text", counting_read)

    resolve_with_tree("top.u_a.out", top="top", session=session, tree=tree, on_log=logs.append)
    after_first = read_count
    resolve_with_tree("top.u_a.out", top="top", session=session, tree=tree, on_log=logs.append)
    assert read_count == after_first
    assert any("hit=full" in line for line in logs)


def test_resolve_with_tree_survives_save_load(tmp_path: Path, monkeypatch):
    top_v = _write(
        tmp_path,
        "top.v",
        """
        module child (output logic out);
          assign out = 1'b0;
        endmodule
        module top;
          child u_a ();
          child u_b ();
        endmodule
        """,
    )
    work = tmp_path / "w"
    _db, session = load_or_build_flat_db([top_v], top="top", work_dir=work)
    tree = TreeDb(work_dir=work, path=resolve_tree_db_path(work))
    logs: list[str] = []

    resolve_with_tree("top.u_a.out", top="top", session=session, tree=tree, on_log=logs.append)
    first_nodes = tree.get_full("top.u_a.out").nodes
    first_files = tree.get_full("top.u_a.out").scoped_files
    tree.save()

    resolve_calls = 0
    orig_resolve = session.resolve

    def counting_resolve(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return orig_resolve(*args, **kwargs)

    monkeypatch.setattr(session, "resolve", counting_resolve)

    reloaded = TreeDb.load(work)
    logs.clear()
    resolve_with_tree(
        "top.u_a.out",
        top="top",
        session=session,
        tree=reloaded,
        on_log=logs.append,
    )
    entry = reloaded.get_full("top.u_a.out")
    assert resolve_calls == 0
    assert entry is not None
    assert entry.nodes == first_nodes
    assert entry.scoped_files == first_files
    assert any("hit=full" in line for line in logs)
    assert not any("resolve spec=" in line for line in logs)