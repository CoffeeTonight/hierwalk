"""Filelist provenance: which .f listed each RTL source."""

from __future__ import annotations

from hierwalk.filelist import (
    filelist_provenance_maps,
    filelist_status_map,
    parse_filelist,
)


def test_nested_filelist_provenance(tmp_path):
    sub = tmp_path / "sub.f"
    sub.write_text("child.v\n", encoding="utf-8")
    top = tmp_path / "top.f"
    top.write_text(f"-f {sub.name}\nparent.v\n", encoding="utf-8")
    (tmp_path / "parent.v").write_text("module parent; endmodule\n", encoding="utf-8")
    (tmp_path / "child.v").write_text("module child; endmodule\n", encoding="utf-8")

    fl = parse_filelist(str(top), index_cwd=str(tmp_path))
    via, chain = filelist_provenance_maps(fl)

    parent = str((tmp_path / "parent.v").resolve())
    child = str((tmp_path / "child.v").resolve())
    top_f = str(top.resolve())
    sub_f = str(sub.resolve())

    assert via[parent] == top_f
    assert via[child] == sub_f
    assert chain[parent] == top_f
    assert chain[child] == f"{top_f} -> {sub_f}"


def test_filelist_linking_graph(tmp_path):
    sub = tmp_path / "sub.f"
    sub.write_text("child.v\n", encoding="utf-8")
    top = tmp_path / "top.f"
    top.write_text(f"-f {sub.name}\nparent.v\n", encoding="utf-8")
    (tmp_path / "parent.v").write_text("module parent; endmodule\n", encoding="utf-8")
    (tmp_path / "child.v").write_text("module child; endmodule\n", encoding="utf-8")

    fl = parse_filelist(str(top), index_cwd=str(tmp_path))
    top_f = str(top.resolve())
    sub_f = str(sub.resolve())

    assert set(fl.filelist_info) == {top_f, sub_f}
    assert fl.filelist_info[sub_f].parent == top_f
    assert fl.filelist_info[sub_f].include_kind == "-f"
    assert fl.filelist_children[top_f] == [sub_f]
    assert fl.filelist_edges == [(top_f, sub_f, "-f")]

    status = filelist_status_map(fl)
    assert status[top_f] == f"True: {top_f}"
    assert status[sub_f] == f"True: {top_f} -> {sub_f}"