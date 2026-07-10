"""Zigzag hierarchy parity via hgpath tree batch."""

from __future__ import annotations

import pytest

from hierwalk.filelist import parse_filelist
from hierwalk.hierarchy_grep import build_module_index, resolve_hierarchy_grep
from hierwalk.zigzag_torture_gen import TOP, write_stress_artifacts

from hgpath.batch import run_batch
from hgpath.flat_db import load_or_build_flat_db
from hgpath.tree_db import TreeDb, resolve_tree_db_path
from hierwalk.connect.shared.request import ConnectivityCheck


@pytest.fixture(scope="module")
def zigzag_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("zz_hgpath")
    fl_path, _req, design = write_stress_artifacts(root)
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    rtl = [str(p.resolve()) for p in fl.source_files]
    return rtl, design, root


def test_hgpath_zigzag_all_hierarchy_specs(zigzag_bundle):
    rtl, design, root = zigzag_bundle
    work = root / "db"
    _db, session = load_or_build_flat_db(rtl, top=TOP, work_dir=work)
    tree = TreeDb(work_dir=work, path=resolve_tree_db_path(work))
    index = build_module_index(rtl)
    failures: list[str] = []
    checks = [
        ConnectivityCheck(spec, spec, check_id=f"zz{i}")
        for i, spec in enumerate(design.hierarchy_specs)
    ]
    run_batch(checks, top=TOP, session=session, tree=tree)
    for spec in design.hierarchy_specs:
        legacy = resolve_hierarchy_grep(spec, top=TOP, rtl_paths=rtl, module_index=index)
        ent = tree.get_full(legacy.get("hierarchy", spec))
        if not legacy.get("ok"):
            failures.append(f"{spec}: legacy fail {legacy.get('error')}")
        elif ent is None or not ent.ok:
            failures.append(f"{spec}: tree miss")
    assert not failures, "\n".join(failures)