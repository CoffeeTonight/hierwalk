"""Path / instance search with per-segment regex."""

from __future__ import annotations

import subprocess
from pathlib import Path

from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.preprocess import preprocess_file
from hierwalk.search import path_pattern_match, search
from hierwalk.search_spec import execute_search_spec, parse_search_spec_block


def _regex_rtl(tmp_path: Path) -> tuple[DesignIndex, list]:
    rtl = tmp_path / "regex_soc.v"
    rtl.write_text(
        """
module top;
  E_blk u_E ( );
endmodule
module E_blk;
  R_mod u_R ( );
endmodule
module R_mod;
  er_leaf er_12x ( );
  er_leaf er_99z ( );
  er_leaf er_00a ( );
endmodule
module er_leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")
    return index, rows


def test_path_regex_segment_fullmatch(tmp_path):
    _index, rows = _regex_rtl(tmp_path)
    pat = "top.E*.R*.er_[0-9]+[xyz]"
    assert path_pattern_match("top.u_E.u_R.er_12x", pat)
    assert path_pattern_match("top.u_E.u_R.er_99z", pat)
    assert not path_pattern_match("top.u_E.u_R.er_00a", pat)
    assert not path_pattern_match("top.u_E.u_R.er_12x.extra", pat)

    hits = search(pat, rows=rows, pattern_kind="path")
    assert {h.full_path for h in hits} == {
        "top.u_E.u_R.er_12x",
        "top.u_E.u_R.er_99z",
    }


def test_path_regex_mixed_with_ellipsis(tmp_path):
    rtl = tmp_path / "regex_deep.v"
    rtl.write_text(
        """
module top;
  E_blk u_E ( );
endmodule
module E_blk;
  mid u_mid ( );
endmodule
module mid;
  R_mod u_R ( );
endmodule
module R_mod;
  er_leaf er_12x ( );
  er_leaf er_99z ( );
endmodule
module er_leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")

    pat = "top.E*..R*.er_[0-9]+[xyz]"
    assert path_pattern_match("top.u_E.u_mid.u_R.er_12x", pat)
    assert not path_pattern_match("top.u_E.u_R.er_12x", pat)

    hits = search(pat, rows=rows, pattern_kind="path")
    assert {h.full_path for h in hits} == {
        "top.u_E.u_mid.u_R.er_12x",
        "top.u_E.u_mid.u_R.er_99z",
    }


def test_instance_regex_via_search_spec(tmp_path):
    index, rows = _regex_rtl(tmp_path)
    spec = parse_search_spec_block({"instance": ["er_[0-9]+[xyz]"]})
    hits = execute_search_spec(rows, index, spec)
    assert {h.full_path for h in hits} == {
        "top.u_E.u_R.er_12x",
        "top.u_E.u_R.er_99z",
    }


def test_hierwalk_cli_regex_path_search(tmp_path: Path):
    rtl = tmp_path / "regex_soc.v"
    rtl.write_text(
        """
module top;
  E_blk u_E ( );
endmodule
module E_blk;
  R_mod u_R ( );
endmodule
module R_mod;
  er_leaf er_12x ( );
  er_leaf er_99z ( );
endmodule
module er_leaf; endmodule
""",
        encoding="utf-8",
    )
    fl = tmp_path / "filelist.f"
    fl.write_text(f"{rtl}\n", encoding="utf-8")
    out = tmp_path / "hits.tsv"

    proc = subprocess.run(
        [
            "hier-walk",
            str(fl),
            "--top",
            "top",
            "--mode",
            "search",
            "--no-cache",
            "--quiet",
            "--search",
            "top.E*.R*.er_[0-9]+[xyz]",
            "-o",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.returncode == 0
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    paths = {line.split("\t", 1)[0] for line in lines[1:]}
    assert paths == {"top.u_E.u_R.er_12x", "top.u_E.u_R.er_99z"}