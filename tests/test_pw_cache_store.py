"""Path-walk cache store: pickle vs sqlite equivalence."""

from __future__ import annotations

from pathlib import Path

from hierwalk.models import InstanceEdge, ModuleRecord
from hierwalk.pw_cache_store import PickleCacheStore, SqliteCacheStore, open_cache_store


def _sample_module(path: str) -> ModuleRecord:
    return ModuleRecord(
        module_name="child",
        file_path=path,
        body="",
        raw_params={},
        instances=[
            InstanceEdge(
                inst_name="u_leaf",
                child_module="leaf",
            )
        ],
    )


def test_sqlite_regex_roundtrip(tmp_path: Path) -> None:
    rtl = tmp_path / "mod.v"
    rtl.write_text("module child; endmodule\n", encoding="utf-8")
    store = SqliteCacheStore(tmp_path / "cache")
    digest = "abc123"
    store.save_regex(str(rtl), content_digest=digest, names=["child"])
    hit = store.load_regex(str(rtl), content_digest=digest)
    assert hit is not None
    assert hit.module_names == ("child",)
    assert store.cache_artifact_count() == 1


def test_sqlite_validated_roundtrip(tmp_path: Path) -> None:
    rtl = tmp_path / "mod.v"
    rtl.write_text("module child; endmodule\n", encoding="utf-8")
    store = SqliteCacheStore(tmp_path / "cache")
    digest = "abc123"
    mod = _sample_module(str(rtl))
    store.save_validated(
        str(rtl),
        {"child": mod},
        content_digest=digest,
        defines_digest="def1",
        include_closure_digest="inc1",
        preprocess_tag="no-inc",
    )
    hit = store.load_validated(
        str(rtl),
        content_digest=digest,
        defines_digest="def1",
        include_closure_digest="inc1",
        preprocess_tag="no-inc",
    )
    assert hit is not None
    assert "child" in hit
    assert hit["child"].instances[0].inst_name == "u_leaf"


def test_sqlite_preprocessed_roundtrip(tmp_path: Path) -> None:
    rtl = tmp_path / "mod.v"
    rtl.write_text("module child; endmodule\n", encoding="utf-8")
    store = SqliteCacheStore(tmp_path / "cache")
    digest = "abc123"
    text = "module child; leaf u(); endmodule\n"
    store.save_preprocessed(
        str(rtl),
        text,
        content_digest=digest,
        defines_digest="def1",
        include_closure_digest="inc1",
        preprocess_tag="no-inc",
    )
    hit = store.load_preprocessed(
        str(rtl),
        content_digest=digest,
        defines_digest="def1",
        include_closure_digest="inc1",
        preprocess_tag="no-inc",
    )
    assert hit == text


def test_pickle_and_sqlite_same_validated_keys(tmp_path: Path) -> None:
    rtl = tmp_path / "mod.v"
    rtl.write_text("module child; endmodule\n", encoding="utf-8")
    pickle_store = PickleCacheStore(tmp_path / "pickle")
    sqlite_store = SqliteCacheStore(tmp_path / "sqlite")
    digest = "digest01"
    mod = _sample_module(str(rtl))
    kwargs = dict(
        content_digest=digest,
        defines_digest="d1",
        include_closure_digest="c1",
        preprocess_tag="no-inc",
    )
    pickle_store.save_validated(str(rtl), {"child": mod}, **kwargs)
    sqlite_store.save_validated(str(rtl), {"child": mod}, **kwargs)
    p_hit = pickle_store.load_validated(str(rtl), **kwargs)
    s_hit = sqlite_store.load_validated(str(rtl), **kwargs)
    assert p_hit is not None and s_hit is not None
    assert p_hit["child"].module_name == s_hit["child"].module_name


def test_open_cache_store_backend(tmp_path: Path) -> None:
    assert isinstance(open_cache_store(tmp_path, "pickle"), PickleCacheStore)
    assert isinstance(open_cache_store(tmp_path, "sqlite"), SqliteCacheStore)