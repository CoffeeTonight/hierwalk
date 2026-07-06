"""Instance glob search with full hierarchy paths."""

from __future__ import annotations

from hierwalk.elab import elaborate
from hierwalk.index import DesignIndex
from hierwalk.preprocess import preprocess_file
from hierwalk.search import (
    normalize_search_patterns,
    parse_search_patterns,
    path_pattern_match,
    search,
)


def test_parse_search_patterns():
    assert parse_search_patterns('niu,sramc') == ["niu", "sramc"]
    assert parse_search_patterns('"niu","sramc"') == ["niu", "sramc"]
    assert normalize_search_patterns("*niu*") == ["*niu*"]
    assert normalize_search_patterns("niu,sramc") == ["niu", "sramc"]


def test_search_multiple_patterns_or(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  niu_wrap u_niu0 ( );
  sram_ctrl u_sramc0 ( );
  other u_other ( );
endmodule
module niu_wrap; endmodule
module sram_ctrl; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")

    hits = search("niu,sramc", rows=rows)
    assert {h.full_path for h in hits} == {"top.u_niu0", "top.u_sramc0"}

    hits_q = search('"niu","sramc"', rows=rows)
    assert {h.full_path for h in hits_q} == {"top.u_niu0", "top.u_sramc0"}


def test_path_pattern_dotted_glob():
    path = "top.u_ab_block.c_ctrl.asd_wrap"
    assert path_pattern_match(path, "top.*ab.*c*.asd*")
    assert path_pattern_match("soc.ab.c.asd", "soc.*ab.*c*.asd*")
    assert not path_pattern_match("top.u_other", "top.*ab.*c*.asd*")
    assert not path_pattern_match(path, "*ab.*c*.asd*")


def test_path_pattern_fixed_depth_not_subsequence():
    deep = "top.E_blk.mid.deep.u_log_blk.u_cpu"
    assert not path_pattern_match(deep, "E*.*log.*cpu*")
    assert not path_pattern_match(deep, "top.E*.*log.*cpu*")
    assert path_pattern_match(deep, "top.E*..*log.*cpu*")
    assert not path_pattern_match(deep, "top.E*..*log..*cpu*")


def test_path_pattern_ellipsis_requires_hops():
    assert path_pattern_match("top.a.b.c", "top.a..c")
    assert not path_pattern_match("top.a.c", "top.a..c")


def test_search_dotted_glob_pattern(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  ab_wrap u_ab_block ( );
endmodule
module ab_wrap;
  c_mod c_ctrl ( );
endmodule
module c_mod;
  asd_leaf asd_wrap ( );
endmodule
module asd_leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")

    hits = search("top.*ab.*c*.asd*", rows=rows)
    assert {h.full_path for h in hits} == {"top.u_ab_block.c_ctrl.asd_wrap"}


def test_search_niu_glob_returns_full_path(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  niu_wrap u_niu0 ( );
  other u_other ( );
endmodule
module niu_wrap;
  leaf u_child ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")

    hits = search("*niu*", rows=rows)
    assert {h.full_path for h in hits} == {
        "top.u_niu0",
        "top.u_niu0.u_child",
    }
    kinds = {h.full_path: h.match_kind for h in hits}
    assert kinds["top.u_niu0"] == "instance"
    assert kinds["top.u_niu0.u_child"] == "hierarchy-under"


def test_search_niu_exact_anchor_when_subtree_off(tmp_path):
    rtl = tmp_path / "d.v"
    rtl.write_text(
        """
module top;
  niu_wrap u_niu0 ( );
  other u_other ( );
endmodule
module niu_wrap;
  leaf u_child ( );
endmodule
module leaf; endmodule
""",
        encoding="utf-8",
    )
    text = preprocess_file(rtl, [], {})
    index = DesignIndex.build({str(rtl): text})
    _root, rows = elaborate(index, "top")

    hits = search("*niu*", rows=rows, include_subtree=False)
    assert {h.full_path for h in hits} == {"top.u_niu0"}
    assert hits[0].match_kind == "instance"