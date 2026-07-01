"""Oracle-based connect validation (lazy production vs strict / eager-files)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.connect.session import run_connectivity_request
from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.connect.pipeline.validate import (
    OracleKind,
    compare_connect_batches,
    files_for_endpoint_specs,
    format_validation_report,
    validate_connect_request,
)


def _index_rows(rtl: str, path: Path):
    index = DesignIndex.build({str(path): rtl})
    _root, rows = elaborate(index, "top")
    return index, rows


def test_files_for_endpoint_specs_collects_prefix_files(tmp_path: Path):
    rtl = """
    module leaf(input a); endmodule
    module mid(input a);
      leaf u_l(.a(a));
    endmodule
    module top(input a);
      mid u_m(.a(a));
    endmodule
    """
    v = tmp_path / "t.v"
    v.write_text(rtl, encoding="utf-8")
    index, rows = _index_rows(rtl, v)
    files = files_for_endpoint_specs(rows, ["top.u_m.u_l.a"])
    assert str(v.resolve()) in {str(Path(f).resolve()) for f in files}
    del index


def test_strict_oracle_matches_trivial_connect(tmp_path: Path):
    rtl = """
    module leaf(input src, output dst);
      assign dst = src;
    endmodule
    module top(input src, output dst);
      leaf u (.src(src), .dst(dst));
    endmodule
    """
    v = tmp_path / "t.v"
    v.write_text(rtl, encoding="utf-8")
    index, rows = _index_rows(rtl, v)
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("top.src", "top.dst", "c0"),),
        top="top",
    )
    report = validate_connect_request(
        request,
        rows=rows,
        index=index,
        top="top",
        oracle=OracleKind.STRICT,
    )
    assert report.ok
    assert "OK" in format_validation_report(report)


def test_compare_detects_fp_risk():
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("a", "b", "x"),),
    )
    from hierwalk.connect.session import ConnectResult
    from hierwalk.models import ConnectEndpoint

    def _res(connected: bool) -> ConnectResult:
        ep = ConnectEndpoint(spec="a", inst_path="top", port_name="a")
        epb = ConnectEndpoint(spec="b", inst_path="top", port_name="b")
        return ConnectResult(
            connected=connected,
            endpoint_a=ep,
            endpoint_b=epb,
            mode="port-port",
            check_id="x",
        )

    prod = type("B", (), {"results": (_res(True),)})()
    oracle = type("B", (), {"results": (_res(False),)})()
    diffs = compare_connect_batches(
        request,
        prod,
        oracle,
        oracle_kind=OracleKind.STRICT,
    )
    assert len(diffs) == 1
    assert diffs[0].risk == "fp_risk"


@pytest.mark.skipif(
    not Path(
        "/home/user/tools/CodeFromAI/hc_hierarchy/design/unified_verify/filelist.f"
    ).is_file(),
    reason="unified_verify corpus not available",
)
def test_unified_verify_strict_oracle():
    from hierwalk.cache import load_or_build_index
    from hierwalk.elab import elaborate_tops_parallel
    from hierwalk.filelist import parse_filelist
    from hierwalk.top_find import resolve_top_modules

    design = Path("/home/user/tools/CodeFromAI/hc_hierarchy/design/unified_verify")
    fl = parse_filelist(
        str(design / "filelist.f"),
        index_cwd=str(design),
    )
    index, bundle, *_rest = load_or_build_index(
        design / "filelist.f",
        fl,
        cache_dir=design / ".validate_cache",
        extra_defines={},
        ignore_paths=(),
        ignore_path_files=(),
        ignore_modules=(),
        ignore_filelists=(),
        jobs=1,
        use_cache=True,
        refresh_cache=False,
    )
    tops = resolve_top_modules(index, top="hc_verify_top", filelist_tops=fl.top_modules)
    _roots, rows, _ = elaborate_tops_parallel(index, tops, jobs=1)
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                "hc_verify_top.clk",
                "hc_verify_top.u_gen_soc",
                "clk_reach",
            ),
            ConnectivityCheck(
                "hc_verify_top.clk",
                "hc_verify_top.u_ecc_engine_00.idx",
                "clk_not_idx",
            ),
        ),
        top="hc_verify_top",
    )
    report = validate_connect_request(
        request,
        rows=rows,
        index=index,
        top="hc_verify_top",
        oracle=OracleKind.STRICT,
    )
    assert report.ok, format_validation_report(report)