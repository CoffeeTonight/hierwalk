"""ElabIndex must not alias caller-owned rows_by_path dicts."""

from __future__ import annotations

from hierwalk.models import ElabIndex, FlatRow


def _row(path: str, *, parent: str | None = None) -> FlatRow:
    leaf = path.rsplit(".", 1)[-1]
    return FlatRow(
        full_path=path,
        inst_leaf=leaf,
        module=leaf,
        depth=path.count("."),
        parent_path=parent,
        file="/rtl/d.v",
    )


def test_from_rows_by_path_does_not_alias_external_dict():
    shared = {"top": _row("top")}
    elab = ElabIndex.from_rows_by_path(shared, rows=[shared["top"]])
    elab.rows_by_path["top.u_a"] = _row("top.u_a", parent="top")
    assert "top.u_a" not in shared