"""Path-walk module DB: tier-0 regex + tier-1 validated scan."""

from __future__ import annotations

import time
from pathlib import Path

from hierwalk.filelist import parse_filelist
from hierwalk.index import DesignIndex
from hierwalk.path_walk import create_path_walk_index, run_path_walk_connect
from hierwalk.path_walk_db import PathWalkModuleDb, path_walk_db_cache_key


def _write_dup_module_design(tmp_path: Path) -> tuple[Path, Path]:
    """Same module name in two files; only second has the child instance."""
    wrong = tmp_path / "parent_wrong.v"
    wrong.write_text(
        """
        module parent;
          // no children — wrong decl file for tier-0 hit
        endmodule
        """,
        encoding="utf-8",
    )
    right = tmp_path / "parent_right.v"
    right.write_text(
        """
        module child(input in, output out);
          assign out = in;
        endmodule

        module parent;
          child u_child (.in(1'b0), .out());
        endmodule
        """,
        encoding="utf-8",
    )
    top = tmp_path / "top.v"
    top.write_text(
        """
        module parent;
          // stub in top file — tier-0 will list parent here too
        endmodule

        module top;
          parent u_parent();
        endmodule
        """,
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(
        "\n".join(
            str(p.resolve())
            for p in (wrong, right, top)
        )
        + "\n",
        encoding="utf-8",
    )
    return fl, right


def _write_ifdef_module_design(tmp_path: Path) -> Path:
    rtl = tmp_path / "ifdef_top.v"
    rtl.write_text(
        """
        `define USE_CHILD

        module child(input in, output out);
          assign out = in;
        endmodule

        `ifdef USE_CHILD
        module parent;
          child u_child (.in(1'b0), .out());
        endmodule
        `else
        module parent;
        endmodule
        `endif

        module top;
          parent u_parent();
        endmodule
        """,
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    return fl


def test_tier1_picks_file_with_expected_instance(tmp_path: Path):
    fl_path, right_file = _write_dup_module_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    index, mod_db = create_path_walk_index(fl, "top", defines={})
    assert mod_db.ensure_module_in_index(
        "parent",
        expect_inst=("parent", "u_child"),
    )
    rec = index.get_module("parent")
    assert rec is not None
    assert str(Path(rec.file_path).resolve()) == str(right_file.resolve())


def test_tier1_honors_cross_file_rtl_undef(tmp_path: Path):
    (tmp_path / "a.v").write_text("`define USE_CHILD 1\n", encoding="utf-8")
    (tmp_path / "parent.v").write_text(
        "`undef USE_CHILD\n"
        "module parent;\n"
        "`ifdef USE_CHILD\n"
        "  child u_c ();\n"
        "`endif\n"
        "endmodule\n"
        "module child; endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(
        "\n".join(
            [
                str((tmp_path / "a.v").resolve()),
                str((tmp_path / "parent.v").resolve()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in flr.include_dirs],
        preprocess_defines=dict(flr.defines),
    )
    db = PathWalkModuleDb(
        [str(p) for p in flr.source_files],
        index,
        include_dirs=[str(p) for p in flr.include_dirs],
        defines=dict(flr.defines),
        no_cache=True,
    )
    scanned = db.tier1_scan_file(str((tmp_path / "parent.v").resolve()))
    assert scanned["parent"].instances == []


def test_tier1_honors_cross_file_rtl_define(tmp_path: Path):
    """RTL `` `define `` in file A must activate `` `ifdef `` instances in file B."""
    (tmp_path / "defines.v").write_text(
        "`define USE_CHILD 1\n",
        encoding="utf-8",
    )
    (tmp_path / "parent.v").write_text(
        "module parent;\n"
        "`ifdef USE_CHILD\n"
        "  child u_c ();\n"
        "`endif\n"
        "endmodule\n"
        "module child; endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(
        "\n".join(
            [
                str((tmp_path / "defines.v").resolve()),
                str((tmp_path / "parent.v").resolve()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in flr.include_dirs],
        preprocess_defines=dict(flr.defines),
    )
    db = PathWalkModuleDb(
        [str(p) for p in flr.source_files],
        index,
        include_dirs=[str(p) for p in flr.include_dirs],
        defines=dict(flr.defines),
        no_cache=True,
    )
    scanned = db.tier1_scan_file(str((tmp_path / "parent.v").resolve()))
    insts = {e.inst_name for e in scanned["parent"].instances}
    assert "u_c" in insts


def test_tier1_validated_cache_keys_effective_defines(tmp_path: Path):
    """Tier-1 disk cache must use post-preprocess defines, not filelist-only."""
    rtl = tmp_path / "soc.v"
    rtl.write_text(
        "`define USE_CPU 0\n"
        "module SOC_TOP;\n"
        "`ifdef USE_CPU\n"
        "  CPUSYSTEM_TOP u_cpusystem_top ();\n"
        "`endif\n"
        "endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(str(rtl.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    cache_dir = tmp_path / "pw-cache"
    cache_key = path_walk_db_cache_key(
        [str(p) for p in flr.source_files],
        defines=dict(flr.defines),
        include_dirs=[str(p) for p in flr.include_dirs],
    )
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in flr.include_dirs],
        preprocess_defines=dict(flr.defines),
    )
    db = PathWalkModuleDb(
        [str(p) for p in flr.source_files],
        index,
        include_dirs=[str(p) for p in flr.include_dirs],
        defines=dict(flr.defines),
        cache_dir=cache_dir,
        cache_key=cache_key,
    )
    scanned = db.tier1_scan_file(str(rtl.resolve()))
    assert scanned["SOC_TOP"].instances == []

    # Stale v9-style sidecar keyed by empty filelist digest must not load.
    from hierwalk.path_walk_db import _defines_digest, _file_cache_token

    stale_name = f"{_file_cache_token(str(rtl.resolve()))}_{_defines_digest({})}.pkl"
    stale_path = cache_dir / cache_key / "validated" / stale_name
    assert not stale_path.is_file()

    db2 = PathWalkModuleDb(
        [str(p) for p in flr.source_files],
        index,
        include_dirs=[str(p) for p in flr.include_dirs],
        defines=dict(flr.defines),
        cache_dir=cache_dir,
        cache_key=cache_key,
    )
    hit = db2.tier1_scan_file(str(rtl.resolve()))
    assert hit["SOC_TOP"].instances == []
    assert db2.cache_validated_hits == 1


def test_tier1_validated_cache_invalidates_on_include_change(tmp_path: Path):
    inc = tmp_path / "cfg.vh"
    inc.write_text("", encoding="utf-8")
    top = tmp_path / "top.v"
    top.write_text(
        '`include "cfg.vh"\n'
        "module top;\n"
        "  child u_c ();\n"
        "endmodule\n",
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(str(top.resolve()) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    cache_dir = tmp_path / "pw-cache"
    cache_key = path_walk_db_cache_key(
        [str(p) for p in flr.source_files],
        defines=dict(flr.defines),
        include_dirs=[str(p) for p in flr.include_dirs],
    )
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in flr.include_dirs],
        preprocess_defines=dict(flr.defines),
    )
    db1 = PathWalkModuleDb(
        [str(p) for p in flr.source_files],
        index,
        include_dirs=[str(p) for p in flr.include_dirs],
        defines=dict(flr.defines),
        cache_dir=cache_dir,
        cache_key=cache_key,
    )
    first = db1.tier1_scan_file(str(top.resolve()))
    assert [e.inst_name for e in first["top"].instances] == ["u_c"]

    inc.write_text(
        "`define HIDE_CHILD\n"
        "`ifdef HIDE_CHILD\n"
        "`endif\n",
        encoding="utf-8",
    )
    db2 = PathWalkModuleDb(
        [str(p) for p in flr.source_files],
        index,
        include_dirs=[str(p) for p in flr.include_dirs],
        defines=dict(flr.defines),
        cache_dir=cache_dir,
        cache_key=cache_key,
    )
    second = db2.tier1_scan_file(str(top.resolve()))
    assert db2.cache_validated_hits == 0
    assert [e.inst_name for e in second["top"].instances] == ["u_c"]


def test_path_walk_db_disk_cache_reuse(tmp_path: Path):
    fl_path = _write_ifdef_module_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    cache_dir = tmp_path / "pw-cache"
    cache_key = path_walk_db_cache_key(
        [str(p) for p in fl.source_files],
        defines=dict(fl.defines),
        include_dirs=[str(p) for p in fl.include_dirs],
    )

    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in fl.include_dirs],
        preprocess_defines=dict(fl.defines),
    )
    db1 = PathWalkModuleDb(
        [str(p) for p in fl.source_files],
        index,
        include_dirs=[str(p) for p in fl.include_dirs],
        defines=dict(fl.defines),
        cache_dir=cache_dir,
        cache_key=cache_key,
    )
    db1.tier1_scan_file(str(fl.source_files[0]))
    assert db1.files_validated == 1
    assert db1.cache_validated_hits == 0

    index2 = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in fl.include_dirs],
        preprocess_defines=dict(fl.defines),
    )
    db2 = PathWalkModuleDb(
        [str(p) for p in fl.source_files],
        index2,
        include_dirs=[str(p) for p in fl.include_dirs],
        defines=dict(fl.defines),
        cache_dir=cache_dir,
        cache_key=cache_key,
    )
    db2.tier1_scan_file(str(fl.source_files[0]))
    assert db2.cache_validated_hits == 1
    assert db2.files_validated == 1


def test_tier0_parallel_finds_module_without_waiting_for_all(tmp_path: Path):
    """Target module hit should return early; background workers still close to disk db."""
    files: list[Path] = []
    for i in range(8):
        rtl = tmp_path / f"stub_{i}.v"
        rtl.write_text(f"module stub_{i} (); endmodule\n", encoding="utf-8")
        files.append(rtl)
    target = tmp_path / "target_parent.v"
    target.write_text(
        """
        module child (); endmodule
        module target_parent;
          child u_child ();
        endmodule
        """,
        encoding="utf-8",
    )
    files.append(target)
    fl = tmp_path / "filelist.f"
    fl.write_text("\n".join(str(p.resolve()) for p in files) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        preprocess_include_dirs=[str(p) for p in flr.include_dirs],
        preprocess_defines=dict(flr.defines),
    )
    cache_dir = tmp_path / "pw-par"
    cache_key = path_walk_db_cache_key(
        [str(p) for p in flr.source_files],
        defines=dict(flr.defines),
        include_dirs=[str(p) for p in flr.include_dirs],
    )
    db = PathWalkModuleDb(
        [str(p) for p in flr.source_files],
        index,
        include_dirs=[str(p) for p in flr.include_dirs],
        defines=dict(flr.defines),
        cache_dir=cache_dir,
        cache_key=cache_key,
        jobs=4,
    )
    t0 = time.perf_counter()
    candidates = db._ensure_regex_candidates("target_parent")
    elapsed = time.perf_counter() - t0
    assert candidates
    assert str(target.resolve()) in candidates
    assert "target_parent" in db.module_to_files_snapshot()
    db.drain_background_workers(wait_all=True)
    assert db.files_regex_scanned >= len(files)
    assert elapsed < 5.0


def test_tier1_background_prefetch_warms_unwalked_files(tmp_path: Path, monkeypatch):
    """Opt-in prefetch tier-1-validates files not on the active hierarchy path."""
    monkeypatch.setenv("HIERWALK_PW_DB_PREFETCH", "1")
    monkeypatch.setenv("HIERWALK_PW_DB_PREFETCH_WAIT", "1")

    files: list[Path] = []
    for i in range(6):
        rtl = tmp_path / f"stub_{i}.v"
        rtl.write_text(f"module stub_{i} (); endmodule\n", encoding="utf-8")
        files.append(rtl)
    target = tmp_path / "target_parent.v"
    target.write_text(
        "module child (); endmodule\n"
        "module target_parent;\n"
        "  child u_child ();\n"
        "endmodule\n",
        encoding="utf-8",
    )
    files.append(target)
    fl = tmp_path / "filelist.f"
    fl.write_text("\n".join(str(p.resolve()) for p in files) + "\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest

    request = ConnectivityRequest(
        checks=(ConnectivityCheck("target_parent.u_child", "target_parent.u_child"),),
        top="target_parent",
    )
    _batch, _index, state = run_path_walk_connect(
        request,
        flr,
        top="target_parent",
        no_cache=True,
    )
    assert "target_parent.u_child" in state.rows_by_path
    from hierwalk.path_walk import build_path_walk_db_full

    build_path_walk_db_full(state.mod_db)
    assert state.mod_db.files_validated == len(files)


def test_tier1_background_prefetch_off_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HIERWALK_PW_DB_PREFETCH", raising=False)

    stub = tmp_path / "extra.v"
    stub.write_text("module extra (); endmodule\n", encoding="utf-8")
    top = tmp_path / "top.v"
    top.write_text("module top (); endmodule\n", encoding="utf-8")
    fl = tmp_path / "filelist.f"
    fl.write_text(f"{stub.resolve()}\n{top.resolve()}\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))
    from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest

    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top", "top"),),
        top="top",
    )
    _batch, _index, state = run_path_walk_connect(
        request,
        flr,
        top="top",
        no_cache=True,
    )
    assert state.mod_db.files_validated < len(flr.source_files)


def test_ignore_path_glob_skips_pw_db_tier0(tmp_path: Path, monkeypatch):
    dw = tmp_path / "DW_blabla.v"
    dw.write_text("module DW_blabla_inst; endmodule\n", encoding="utf-8")
    top = tmp_path / "top.v"
    top.write_text("module top; DW_blabla_inst u (); endmodule\n", encoding="utf-8")
    fl = tmp_path / "filelist.f"
    fl.write_text(f"{top.resolve()}\n{dw.resolve()}\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))

    tier0_scans: list[str] = []
    orig_scan = PathWalkModuleDb._tier0_scan_file

    def traced_scan(self, path):
        tier0_scans.append(Path(path).name)
        return orig_scan(self, path)

    monkeypatch.setattr(PathWalkModuleDb, "_tier0_scan_file", traced_scan)
    _index, mod_db = create_path_walk_index(
        flr,
        "top",
        defines={},
        ignore_paths=["DW_*"],
        no_cache=True,
        jobs=1,
    )
    assert "DW_blabla.v" not in tier0_scans
    assert "DW_blabla_inst" not in mod_db._module_to_files
    assert "top.v" in tier0_scans


def test_ignore_module_skips_pw_db_tier0_resolve(tmp_path: Path, monkeypatch):
    """ignore-module must block pw-db tier0/tier1 resolve, not only DesignIndex stubs."""
    bb = tmp_path / "bb.v"
    bb.write_text("module bb_mod(input in); endmodule\n", encoding="utf-8")
    top = tmp_path / "top.v"
    top.write_text(
        """
        module top;
          bb_mod u_bb();
        endmodule
        """,
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(f"{top.resolve()}\n{bb.resolve()}\n", encoding="utf-8")
    flr = parse_filelist(str(fl), index_cwd=str(tmp_path))

    tier0_scans: list[str] = []
    orig_scan = PathWalkModuleDb._tier0_scan_file

    def traced_scan(self, path):
        tier0_scans.append(Path(path).name)
        return orig_scan(self, path)

    monkeypatch.setattr(PathWalkModuleDb, "_tier0_scan_file", traced_scan)
    index, mod_db = create_path_walk_index(
        flr,
        "top",
        defines={},
        ignore_modules=["bb_mod"],
        no_cache=True,
        jobs=1,
    )
    assert mod_db._is_ignored_module("bb_mod")
    assert mod_db.ensure_module_in_index("bb_mod") is False
    assert "bb_mod" not in mod_db._module_to_files
    assert "bb.v" not in tier0_scans
    assert "top.v" in tier0_scans


def test_path_walk_walks_through_dup_module_files(tmp_path: Path):
    fl_path, _right = _write_dup_module_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    from hierwalk.connect_request import ConnectivityCheck, ConnectivityRequest

    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top.u_parent.u_child.in", "top.u_parent.u_child.in"),),
        top="top",
    )
    batch, index, state = run_path_walk_connect(
        request,
        fl,
        top="top",
        no_cache=True,
    )
    assert "top.u_parent.u_child" in state.rows_by_path
    assert batch.results[0].connected is True
    assert index.get_module("child") is not None