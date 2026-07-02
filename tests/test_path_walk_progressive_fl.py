"""Progressive filelist-shell tier-0 decl resolve (no whole-design queue)."""

from __future__ import annotations

from pathlib import Path

from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.path_walk_db import PathWalkModuleDb


def _write_deep_fl_top(tmp_path: Path) -> tuple[Path, str]:
    """Top RTL in root.f shell; many decoys live only in a deep child filelist."""
    (tmp_path / "soc_top.v").write_text(
        "module SOC_TOP;\n  BLK u_blk ();\nendmodule\n",
        encoding="utf-8",
    )
    lists = tmp_path / "lists"
    lists.mkdir()
    deep = lists / "deep"
    deep.mkdir()
    for i in range(40):
        (deep / f"noise_{i}.v").write_text(
            f"module noise_{i} (); endmodule\n",
            encoding="utf-8",
        )
    (lists / "child.f").write_text(
        "\n".join(str((deep / f"noise_{i}.v").resolve()) for i in range(40)) + "\n",
        encoding="utf-8",
    )
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp_path / "soc_top.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.f"
    root.write_text(f"-f {(lists / 'parent.f').resolve()}\n", encoding="utf-8")
    return root, "SOC_TOP"


def test_top_module_found_in_root_shell_without_deep_scan(tmp_path: Path):
    root_fl, top_name = _write_deep_fl_top(tmp_path)
    fl = parse_filelist(str(root_fl), index_cwd=str(tmp_path))
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in fl.include_dirs],
        preprocess_defines=dict(fl.defines),
    )
    db = PathWalkModuleDb(
        [str(p.resolve()) for p in fl.source_files],
        index,
        include_dirs=[str(p) for p in fl.include_dirs],
        defines=dict(fl.defines),
        file_via_filelist={
            str(Path(k).resolve()): str(Path(v).resolve())
            for k, v in (fl.source_via_filelist or {}).items()
        },
        filelist_children={
            str(Path(k).resolve()): [str(Path(c).resolve()) for c in v]
            for k, v in (fl.filelist_children or {}).items()
        },
        root_filelist=str(root_fl.resolve()),
        no_cache=True,
    )
    top_file = db.find_module_decl_file(top_name)
    assert top_file is not None
    assert Path(top_file).name == "soc_top.v"
    assert db.files_regex_scanned <= 5
    db.shutdown_workers(wait=True)


def test_tier0_line_scan_finds_module(tmp_path: Path):
    from hierwalk.path_walk_db import tier0_regex_module_names_from_path

    rtl = tmp_path / "big.v"
    rtl.write_text(
        "// header\n" + "module other (); endmodule\n" * 5 + "module TARGET (); endmodule\n",
        encoding="utf-8",
    )
    names = tier0_regex_module_names_from_path(rtl)
    assert "TARGET" in names
    assert "other" in names