"""Confident (child filelist) vs recovery (subtree/global) resolve policy."""

from __future__ import annotations

import io
from pathlib import Path

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect
from hierwalk.path_walk_db import RESOLVE_CONFIDENT, RESOLVE_RECOVERY, PathWalkModuleDb


def _write_nested_child_fl_design(tmp_path: Path) -> Path:
    (tmp_path / "top.v").write_text(
        "module SOC_TOP; BLK u_blk (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "blk_real.v").write_text(
        "module BLK; CORE u_core (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "core.v").write_text(
        "module CORE; LEAF u_leaf (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "leaf.v").write_text("module LEAF; endmodule\n", encoding="utf-8")
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(
        "\n".join(
            str((tmp_path / n).resolve())
            for n in ("blk_real.v", "core.v", "leaf.v")
        )
        + "\n",
        encoding="utf-8",
    )
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp_path / "top.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.f"
    root.write_text(f"-f {(lists / 'parent.f').resolve()}\n", encoding="utf-8")
    return root


def _write_stub_child_recovery_design(tmp_path: Path) -> tuple[Path, str]:
    (tmp_path / "top.v").write_text(
        "module SOC_TOP; BLK u_blk (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "blk_stub.v").write_text("module BLK; endmodule\n", encoding="utf-8")
    (tmp_path / "blk_real.v").write_text(
        "module BLK; CORE u_core (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "core.v").write_text(
        "module CORE; LEAF u_leaf (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "leaf.v").write_text("module LEAF; endmodule\n", encoding="utf-8")
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(
        str((tmp_path / "blk_stub.v").resolve()) + "\n",
        encoding="utf-8",
    )
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp_path / "top.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.f"
    root.write_text(
        "\n".join(
            [
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp_path / "blk_real.v").resolve()),
                str((tmp_path / "core.v").resolve()),
                str((tmp_path / "leaf.v").resolve()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root, "SOC_TOP.u_blk.u_core.u_leaf"


def test_confident_resolves_module_in_direct_child_filelist(tmp_path: Path):
    fl_path = _write_nested_child_fl_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    from hierwalk.path_walk import create_path_walk_index

    index, mod_db = create_path_walk_index(fl, "SOC_TOP", defines={}, no_cache=True)
    assert mod_db.resolve_child_edge(
        "SOC_TOP",
        {},
        "u_blk",
        current_file=str((tmp_path / "top.v").resolve()),
        policy=RESOLVE_CONFIDENT,
    ) is not None


def _minimal_mod_db_for_recovery_test(fl, tmp_path: Path):
    """Build mod_db with explicit tier0 state (no create_path_walk_index side effects)."""
    from hierwalk.filelist import filelist_provenance_maps
    from hierwalk.index import DesignIndex
    from hierwalk.path_walk_db import PathWalkModuleDb

    sources = [str(Path(p).resolve()) for p in fl.source_files]
    via_map, _chain_map = filelist_provenance_maps(fl)
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        filelist_patterns=[],
        library_files=[],
        library_dirs=[],
        libexts=[],
        file_via_filelist={
            str(Path(k).resolve()): str(Path(v).resolve())
            for k, v in fl.source_via_filelist.items()
        },
        file_filelist_chain={
            str(Path(k).resolve()): v for k, v in fl.source_filelist_chain.items()
        },
        preprocess_include_dirs=[str(p) for p in fl.include_dirs],
        preprocess_defines={},
    )
    mod_db = PathWalkModuleDb(
        sources,
        index,
        defines={},
        no_cache=True,
        file_via_filelist=via_map,
        filelist_children={
            str(Path(k).resolve()): [str(Path(c).resolve()) for c in v]
            for k, v in fl.filelist_children.items()
        },
    )
    blk_stub = str((tmp_path / "blk_stub.v").resolve())
    blk_real = str((tmp_path / "blk_real.v").resolve())
    mod_db._tier0_scan_file(blk_stub)
    mod_db._tier0_scan_file(blk_real)
    for src in sources:
        if src not in mod_db._regex_scanned:
            mod_db._tier0_scan_file(src)
    return mod_db, blk_stub, blk_real


def _empty_index():
    from hierwalk.index import DesignIndex

    return DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        filelist_patterns=[],
        library_files=[],
        library_dirs=[],
        libexts=[],
        preprocess_include_dirs=[],
        preprocess_defines={},
    )


def test_recovery_global_tier0_scans_past_pw_global_max(tmp_path: Path):
    """Recovery global expand must not truncate at pw_tier0_global_scan_max (128)."""
    (tmp_path / "blk_stub.v").write_text("module BLK; endmodule\n", encoding="utf-8")
    (tmp_path / "blk_real.v").write_text(
        "module BLK; CORE u_core (); endmodule\n",
        encoding="utf-8",
    )
    for i in range(130):
        (tmp_path / f"f{i}.v").write_text(f"module m{i}; endmodule\n", encoding="utf-8")
    sources = [str((tmp_path / "blk_stub.v").resolve())]
    sources.extend(str((tmp_path / f"f{i}.v").resolve()) for i in range(130))
    blk_real = str((tmp_path / "blk_real.v").resolve())
    sources.append(blk_real)
    mod_db = PathWalkModuleDb(sources, _empty_index(), no_cache=True)
    mod_db._tier0_scan_file(sources[0])
    assert blk_real not in mod_db._module_to_files.get("BLK", [])

    mod_db._scan_remaining_sources_tier0(
        None,
        target_module="BLK",
        policy=RESOLVE_RECOVERY,
    )
    assert blk_real in mod_db._module_to_files["BLK"]


def test_recovery_global_tier0_finds_second_dup_with_tier1_cap_1(
    tmp_path: Path, monkeypatch
):
    """Recovery must not use target_module early-exit (dup decl #2 past tier1 cap)."""
    import hierwalk.perf as perf_mod

    monkeypatch.setattr(perf_mod, "pw_inst_resolve_tier1_max", lambda _policy: 1)
    (tmp_path / "blk_stub.v").write_text("module BLK; endmodule\n", encoding="utf-8")
    (tmp_path / "blk_real.v").write_text(
        "module BLK; CORE u_core (); endmodule\n",
        encoding="utf-8",
    )
    blk_stub = str((tmp_path / "blk_stub.v").resolve())
    blk_real = str((tmp_path / "blk_real.v").resolve())
    mod_db = PathWalkModuleDb([blk_stub, blk_real], _empty_index(), no_cache=True)
    mod_db._tier0_scan_file(blk_stub)
    assert blk_real not in mod_db._module_to_files.get("BLK", [])

    mod_db._scan_remaining_sources_tier0(
        None,
        target_module="BLK",
        policy=RESOLVE_RECOVERY,
    )
    assert blk_real in mod_db._module_to_files["BLK"]


def test_recovery_expand_skips_parallel_sibling_filelist(tmp_path: Path):
    """recovery-expand must not tier0-scan RTL from parallel sibling filelists."""
    from hierwalk.filelist import filelist_provenance_maps
    from hierwalk.index import DesignIndex
    from hierwalk.preprocess_log import register_pp_log_sink

    fl_path, _leaf = _write_stub_child_recovery_design(tmp_path)
    for i in range(12):
        (tmp_path / f"noise_{i}.v").write_text(
            f"module noise_{i}; endmodule\n",
            encoding="utf-8",
        )
    noise_f = tmp_path / "noise.f"
    noise_f.write_text(
        "\n".join(
            str((tmp_path / f"noise_{i}.v").resolve()) for i in range(12)
        )
        + "\n",
        encoding="utf-8",
    )
    mega_f = tmp_path / "mega.f"
    mega_f.write_text(f"-f {fl_path.name}\n-f {noise_f.name}\n", encoding="utf-8")
    fl = parse_filelist(str(mega_f), index_cwd=str(tmp_path))
    sources = [str(Path(p).resolve()) for p in fl.source_files]
    via_map, _chain_map = filelist_provenance_maps(fl)
    index = DesignIndex._assemble(
        {},
        path_patterns=[],
        module_patterns=[],
        filelist_patterns=[],
        library_files=[],
        library_dirs=[],
        libexts=[],
        file_via_filelist={
            str(Path(k).resolve()): str(Path(v).resolve())
            for k, v in fl.source_via_filelist.items()
        },
        file_filelist_chain={
            str(Path(k).resolve()): v for k, v in fl.source_filelist_chain.items()
        },
        preprocess_include_dirs=[str(p) for p in fl.include_dirs],
        preprocess_defines={},
    )
    mod_db = PathWalkModuleDb(
        sources,
        index,
        defines={},
        no_cache=True,
        jobs=4,
        file_via_filelist=via_map,
        filelist_children={
            str(Path(k).resolve()): [str(Path(c).resolve()) for c in v]
            for k, v in fl.filelist_children.items()
        },
    )
    blk_stub = str((tmp_path / "blk_stub.v").resolve())
    blk_real = str((tmp_path / "blk_real.v").resolve())
    pp_t0_files: list[str] = []

    def _capture_pp(line: str) -> None:
        if "[hier-walk pp] pp-t0" in line and "pp-t0-hit" not in line:
            parts = line.split()
            if len(parts) >= 4:
                pp_t0_files.append(parts[3])

    register_pp_log_sink(_capture_pp)
    mod_db._tier0_scan_file(blk_stub)
    candidates = mod_db._ensure_regex_candidates(
        "BLK",
        scope_anchor=blk_stub,
        policy=RESOLVE_RECOVERY,
    )
    assert blk_real in candidates
    for i in range(12):
        assert f"noise_{i}.v" not in pp_t0_files
    recovery_expand = [
        f for f in pp_t0_files if f in {"blk_real.v", "top.v", "core.v", "leaf.v"}
    ]
    assert recovery_expand


def test_ensure_regex_candidates_recovery_scans_past_stub_map(tmp_path: Path):
    """Recovery must not early-return when stub alone is already in _module_to_files."""
    stub = tmp_path / "blk_stub.v"
    stub.write_text("module BLK; endmodule\n", encoding="utf-8")
    real = tmp_path / "blk_real.v"
    real.write_text("module BLK; CORE u_core (); endmodule\n", encoding="utf-8")
    sources = [str(stub.resolve())]
    for i in range(10):
        p = tmp_path / f"f{i}.v"
        p.write_text(f"module m{i}; endmodule\n", encoding="utf-8")
        sources.append(str(p.resolve()))
    sources.append(str(real.resolve()))
    mod_db = PathWalkModuleDb(sources, _empty_index(), no_cache=True)
    mod_db._tier0_scan_file(str(stub.resolve()))
    assert "BLK" in mod_db._module_to_files
    assert str(real.resolve()) not in mod_db._module_to_files["BLK"]

    candidates = mod_db._ensure_regex_candidates(
        "BLK",
        scope_anchor=str(stub.resolve()),
        policy=RESOLVE_RECOVERY,
    )
    assert str(real.resolve()) in candidates
    edge = mod_db.resolve_child_edge(
        "BLK",
        {},
        "u_core",
        current_file=str(stub.resolve()),
        policy=RESOLVE_RECOVERY,
    )
    assert edge is not None
    assert edge.child_module == "CORE"


def test_recovery_finds_real_blk_after_many_dup_stubs_module_cap(tmp_path: Path):
    """Per-module file cap must not permanently drop late dup-module decls."""
    from hierwalk.index import DesignIndex

    sources = []
    for i in range(35):
        p = tmp_path / f"blk_stub_{i}.v"
        p.write_text("module BLK; endmodule\n", encoding="utf-8")
        sources.append(str(p.resolve()))
    real = tmp_path / "blk_real.v"
    real.write_text("module BLK; CORE u_core (); endmodule\n", encoding="utf-8")
    sources.append(str(real.resolve()))
    mod_db = PathWalkModuleDb(
        sources,
        DesignIndex._assemble(
            {},
            path_patterns=[],
            module_patterns=[],
            filelist_patterns=[],
            library_files=[],
            library_dirs=[],
            libexts=[],
            preprocess_include_dirs=[],
            preprocess_defines={},
        ),
        no_cache=True,
    )
    for src in sources:
        mod_db._tier0_scan_file(src)
    assert str(real.resolve()) in mod_db._module_to_files.get("BLK", [])
    edge = mod_db.resolve_child_edge(
        "BLK",
        {},
        "u_core",
        current_file=str(sources[0]),
        policy=RESOLVE_RECOVERY,
    )
    assert edge is not None
    assert edge.child_module == "CORE"


def test_recovery_pass1_retries_mapped_candidates_when_pending_zero(tmp_path: Path):
    """Global tier0 done (pending==0) must still retry _module_to_files entries."""
    fl_path, _leaf = _write_stub_child_recovery_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    mod_db, blk_stub, blk_real = _minimal_mod_db_for_recovery_test(fl, tmp_path)

    assert mod_db._scan_remaining_sources_tier0(None, target_module="BLK") == 0
    assert blk_stub in mod_db._module_to_files["BLK"]
    assert blk_real in mod_db._module_to_files["BLK"]

    refreshed = mod_db._recovery_pass1_candidates(
        "BLK",
        pending=0,
        tried={blk_stub},
        avoid_file=blk_stub,
        scope_anchor=blk_stub,
        trace_label="module=BLK",
    )
    assert refreshed is not None
    assert blk_real in refreshed
    assert blk_stub not in refreshed

    edge = mod_db.resolve_child_edge(
        "BLK",
        {},
        "u_core",
        current_file=blk_stub,
        policy=RESOLVE_RECOVERY,
    )
    assert edge is not None
    assert edge.child_module == "CORE"


def _write_top_a_ub_c_recovery_design(tmp_path: Path) -> tuple[Path, str]:
    """``top.a.u_b.c``: confident miss on ``u_b``, recovery hits real ``B.v``."""
    (tmp_path / "top.v").write_text("module top; A a (); endmodule\n", encoding="utf-8")
    (tmp_path / "a.v").write_text("module A; B u_b (); endmodule\n", encoding="utf-8")
    (tmp_path / "b_stub.v").write_text("module B; endmodule\n", encoding="utf-8")
    (tmp_path / "b_real.v").write_text("module B; C c (); endmodule\n", encoding="utf-8")
    (tmp_path / "c.v").write_text("module C; endmodule\n", encoding="utf-8")
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(
        str((tmp_path / "b_stub.v").resolve()) + "\n",
        encoding="utf-8",
    )
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp_path / "top.v").resolve()),
                str((tmp_path / "a.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.f"
    root.write_text(
        "\n".join(
            [
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp_path / "b_real.v").resolve()),
                str((tmp_path / "c.v").resolve()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root, "top.a.u_b.c"


def test_connect_resolves_top_a_ub_c_after_recovery(tmp_path: Path):
    fl_path, target = _write_top_a_ub_c_recovery_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(target, target, check_id="1"),),
        top="top",
    )
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top="top",
        no_cache=True,
    )
    assert target in state.rows_by_path
    assert not batch.results[0].errors
    assert batch.results[0].connected is True


def _write_top_a_b_cd_scoped_design(tmp_path: Path) -> tuple[Path, str]:
    """``top.a.b.c.d`` with real ``B`` only on ancestor filelist (child FL has stub)."""
    (tmp_path / "top_a.v").write_text(
        "module A; B b (); endmodule\nmodule top; A a (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "b_stub.v").write_text("module B; endmodule\n", encoding="utf-8")
    (tmp_path / "b_real.v").write_text(
        "module B; C c (); endmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "c.v").write_text("module C; D d (); endmodule\n", encoding="utf-8")
    (tmp_path / "d.v").write_text("module D; endmodule\n", encoding="utf-8")
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(
        str((tmp_path / "b_stub.v").resolve()) + "\n",
        encoding="utf-8",
    )
    (lists / "parent.f").write_text(
        "\n".join(
            [
                str((tmp_path / "top_a.v").resolve()),
                f"-f {(lists / 'child.f').resolve()}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.f"
    root.write_text(
        "\n".join(
            [
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp_path / "b_real.v").resolve()),
                str((tmp_path / "c.v").resolve()),
                str((tmp_path / "d.v").resolve()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root, "top.a.b.c.d"


def _write_top_a_b_stub_parent_scoped_design(tmp_path: Path) -> tuple[Path, str]:
    """Real ``A``/``b`` on ancestor FL; child FL holds stub ``A`` without instances."""
    (tmp_path / "top.v").write_text("module top; A a (); endmodule\n", encoding="utf-8")
    (tmp_path / "top_a.v").write_text(
        "module A;\n`ifndef NO_B\n B b ();\n`endif\nendmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "b_stub.v").write_text(
        "module A; endmodule\nmodule B; endmodule\n",
        encoding="utf-8",
    )
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "child.f").write_text(
        str((tmp_path / "b_stub.v").resolve()) + "\n",
        encoding="utf-8",
    )
    (lists / "parent.f").write_text(
        f"-f {(lists / 'child.f').resolve()}\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.f"
    root.write_text(
        "\n".join(
            [
                str((tmp_path / "top.v").resolve()),
                f"-f {(lists / 'parent.f').resolve()}",
                str((tmp_path / "top_a.v").resolve()),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return root, "top.a.b"


def test_confident_ancestor_resolves_edge_b_without_recovery(tmp_path: Path, monkeypatch):
    """``top.a.b`` must resolve ``b`` via ancestor RTL when child FL only has stub ``A``."""
    fl_path, target = _write_top_a_b_stub_parent_scoped_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))

    def _no_recovery(self, spec_targets=None, **kwargs):
        return 0, 0, []

    monkeypatch.setattr(
        "hierwalk.path_walk.PathWalkState.run_recovery_pass",
        _no_recovery,
    )

    req = ConnectivityRequest(
        checks=(ConnectivityCheck(target, target, check_id="1"),),
        top="top",
    )
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top="top",
        no_cache=True,
        connect_phase="text",
    )
    row = state.rows_by_path.get(target)
    row_a = state.rows_by_path.get("top.a")
    assert row is not None, batch.results[0].errors
    assert row.module == "B"
    assert row_a is not None
    assert row_a.file.endswith("top_a.v")
    assert batch.results[0].connected is True
    assert state.mod_db.defer_count() == 0


def test_confident_ancestor_tier0_finds_dup_module_without_recovery(tmp_path: Path):
    """Module co-listed on ancestor FL must resolve under confident child scope."""
    fl_path, target = _write_top_a_b_cd_scoped_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck("top.a.b", "top.a.b", check_id="1"),),
        top="top",
    )
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top="top",
        no_cache=True,
        connect_phase="text",
    )
    row = state.rows_by_path.get("top.a.b")
    assert row is not None
    assert row.module == "B"
    assert batch.results[0].connected is True
    assert state.mod_db.defer_count() == 0


def test_text_phase_resolves_top_a_b_c_d_with_scoped_filelist(tmp_path: Path):
    """Text-conn must selective-recover dup-module paths (not defer to logical only)."""
    fl_path, target = _write_top_a_b_cd_scoped_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(target, target, check_id="1"),),
        top="top",
    )
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top="top",
        no_cache=True,
        connect_phase="text",
    )
    assert target in state.rows_by_path
    assert not batch.results[0].errors
    assert batch.results[0].connected is True


def test_confident_resolves_stub_child_chain_without_recovery_defer(tmp_path: Path):
    """Ancestor/colist tier0 must finish stub-child chains in the confident pass."""
    fl_path, leaf = _write_stub_child_recovery_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    buf = io.StringIO()
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(leaf, leaf),),
        top="SOC_TOP",
    )
    batch, _index, state = run_path_walk_connect(
        req,
        fl,
        top="SOC_TOP",
        no_cache=True,
        trace_stream=buf,
    )
    text = buf.getvalue()
    assert "confident-miss defer" not in text
    assert "recovery-pass start" not in text
    assert leaf in state.rows_by_path
    assert batch.results[0].connected is True
    assert state.mod_db.defer_count() == 0


def test_recovery_skips_duplicate_ensure_for_rewalk_targets(tmp_path: Path):
    """Recovery apply skips ensure_path when endpoint re-walk will cover it."""
    from hierwalk.path_walk import (
        _recovery_skip_ensure_targets,
        _recovery_target_covered_by_rewalk,
    )
    from hierwalk.path_walk_db import DeferredResolve, RESOLVE_RECOVERY

    leaf = "SOC_TOP.u_blk.u_core.u_leaf"
    spec_targets = {leaf: leaf}
    affected = [leaf]
    recovered = [
        DeferredResolve(
            kind="module",
            module_name="CORE",
            scope_anchor="SOC_TOP.u_blk.u_core",
            target_path="SOC_TOP.u_blk.u_core",
        ),
        DeferredResolve(
            kind="module",
            module_name="LEAF",
            scope_anchor=leaf,
            target_path=leaf,
        ),
    ]
    assert _recovery_target_covered_by_rewalk(
        "SOC_TOP.u_blk.u_core",
        spec_targets,
        affected,
    )
    skip = _recovery_skip_ensure_targets(recovered, spec_targets, affected)
    assert skip == {"SOC_TOP.u_blk.u_core", leaf}

    fl_path, leaf_path = _write_stub_child_recovery_design(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    req = ConnectivityRequest(
        checks=(ConnectivityCheck(leaf_path, leaf_path),),
        top="SOC_TOP",
    )
    recovery_ensures: list[tuple[str, str]] = []

    from hierwalk import path_walk as pw

    orig_apply = pw.PathWalkState._apply_recovery_results

    def traced_apply(self, recovered_items, *, skip_targets=None):
        for item in recovered_items:
            path = item.target_path
            if path and (skip_targets is None or path not in skip_targets):
                recovery_ensures.append((path, RESOLVE_RECOVERY))
        return orig_apply(self, recovered_items, skip_targets=skip_targets)

    pw.PathWalkState._apply_recovery_results = traced_apply
    try:
        batch, _index, state = run_path_walk_connect(
            req,
            fl,
            top="SOC_TOP",
            no_cache=True,
        )
    finally:
        pw.PathWalkState._apply_recovery_results = orig_apply

    assert leaf_path in state.rows_by_path
    assert batch.results[0].connected is True
    assert recovery_ensures == []