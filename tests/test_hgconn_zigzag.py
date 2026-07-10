"""hgconn bloom on zigzag self-check endpoints."""

from __future__ import annotations

import pytest

from hierwalk.connect.shared.request import ConnectivityCheck
from hierwalk.filelist import parse_filelist
from hierwalk.zigzag_torture_gen import DEEP_D5, TOP, write_stress_artifacts

from hgpath.batch import run_batch
from hgpath.flat_db import load_or_build_flat_db
from hgpath.tree_db import TreeDb, resolve_tree_db_path
from hgconn.walk import run_bloom_batch


@pytest.fixture(scope="module")
def zigzag_conn_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("zz_hgconn")
    fl_path, _req, design = write_stress_artifacts(root)
    fl = parse_filelist(str(fl_path), index_cwd=str(fl_path.parent))
    rtl = [str(p.resolve()) for p in fl.source_files]
    return rtl, root


def test_hgconn_zigzag_leaf_out_self_connected(zigzag_conn_bundle):
    rtl, root = zigzag_conn_bundle
    work = root / "db"
    _db, session = load_or_build_flat_db(rtl, top=TOP, work_dir=work)
    tree = TreeDb(work_dir=work, path=resolve_tree_db_path(work))
    spec = f"{DEEP_D5}.leaf_out"
    chk = ConnectivityCheck(spec, spec, check_id="zz_leaf")
    batch = run_batch([chk], top=TOP, session=session, tree=tree)
    results = run_bloom_batch(batch.check_results)
    assert results[0].connected