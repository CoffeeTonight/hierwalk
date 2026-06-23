"""Per-top .db_{TOP} work directory layout."""

from __future__ import annotations

from pathlib import Path

from hierwalk.cache import (
    cache_path_for,
    ensure_top_work_dir,
    resolve_run_work_dir,
    resolve_top_label,
    set_active_work_dir,
    top_work_dir_name,
    work_base_dir,
)
from hierwalk.filelist import parse_filelist
from hierwalk.hch_compat.filelist_preprocess import slang_filelist_cache_path
from hierwalk.report import default_log_path


def test_top_work_dir_name_sanitizes():
    assert top_work_dir_name("chip_top_example") == ".db_chip_top_example"
    assert top_work_dir_name("a/b") == ".db_a_b"


def test_resolve_run_work_dir_creates_db_top(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HIERWALK_CACHE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    root = resolve_run_work_dir("SOC_TOP", base=work_base_dir())
    assert root == tmp_path / ".db_SOC_TOP"
    assert root.is_dir()
    assert (root / "tmp").is_dir()


def test_work_base_dir_ignores_index_cwd(tmp_path: Path, monkeypatch):
    """``.db_{TOP}`` base is shell cwd, not ``index-cwd`` (filelist-only)."""
    shell = tmp_path / "shell"
    json_dir = tmp_path / "json"
    shell.mkdir()
    json_dir.mkdir()
    monkeypatch.chdir(shell)
    assert work_base_dir() == shell.resolve()
    assert resolve_run_work_dir("top", base=work_base_dir()) == shell / ".db_top"
    assert not (json_dir / ".db_top").exists()


def test_default_log_path_under_work_dir(tmp_path: Path):
    work = ensure_top_work_dir("top", base=tmp_path)
    log = default_log_path("design.f", "-", work_dir=work)
    assert log == work / "design.hier-walk.log"


def test_slang_cache_under_work_dir(tmp_path: Path):
    work = ensure_top_work_dir("top", base=tmp_path)
    set_active_work_dir(work)
    fl = parse_filelist(
        str(Path(__file__).resolve().parents[1] / "examples/stress_seed42/filelist.f"),
        index_cwd=str(Path(__file__).resolve().parents[1] / "examples/stress_seed42"),
    )
    raw = fl.raw
    assert raw is not None
    dest = slang_filelist_cache_path(raw)
    assert dest.parent == work / "tmp"


def test_resolve_top_label_prefers_cfg_top():
    assert resolve_top_label(cfg_top="chip_top", filelist_tops=["other"]) == "chip_top"


def test_cache_files_live_under_work_dir(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HIERWALK_CACHE_DIR", raising=False)
    work = resolve_run_work_dir("top", base=tmp_path)
    pkl = cache_path_for(work, "abc123")
    assert pkl.parent == work
    assert str(pkl).startswith(str(work))