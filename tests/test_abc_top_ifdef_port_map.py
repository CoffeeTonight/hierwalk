"""ABC_TOP-style port maps: nested ifndef, inline `ifdef, nested `ifndef IIOO."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hierwalk.connect_scan import instance_port_maps, prepare_connect_body
from hierwalk.index import scan_preprocessed
from hierwalk.inst_scan import slim_body_for_instance_scan
from hierwalk.preprocess import apply_ifdef_filter, clear_include_unit_cache, preprocess_file_for_index


def _abc_top_rtl() -> str:
    return """
module parent;
//////
//ABC TOP
///
`ifndef NO_ABC_TOP
assign w_ASD=1'h0;
assignw_QWE=1'h1;

ABC_TOP u_abc_top
(
    .A (wa),
    .B (wb),
......`ifdef POPO
    .C (wc),
`else
`ifndef IIOO
    .D (wd),
`endif
    .E (we),
`endif
);
`endif
endmodule

module ABC_TOP(input A, B, C, D, E);
endmodule
"""


def test_abc_top_instance_index_nested_ifndef_iioo(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(_abc_top_rtl(), encoding="utf-8")
    clear_include_unit_cache()
    text = preprocess_file_for_index(rtl, [tmp_path], {})
    mods = scan_preprocessed(text, str(rtl))
    insts = [(e.inst_name, e.child_module) for e in mods["parent"].instances]
    assert ("u_abc_top", "ABC_TOP") in insts


def test_slim_body_strips_inline_ifdef_on_port_line():
    body = """
ABC_TOP u_abc_top
(
    .A (wa),
    .B (wb),......`ifdef POPO
    .C (wc),
`else `ifndef IIOO
    .D (wd),
`endif
    .E (we),
`endif
);
"""
    slim = slim_body_for_instance_scan(body)
    assert "`ifdef" not in slim
    assert "`ifndef" not in slim
    assert "`endif" not in slim
    assert ".B (wb),......" in slim
    assert ".C (wc)," in slim
    assert ".D (wd)," in slim
    assert ".E (we)," in slim
    ports = instance_port_maps(slim)
    assert ports["u_abc_top"] == [
        ("A", "wa"),
        ("B", "wb"),
        ("C", "wc"),
        ("D", "wd"),
        ("E", "we"),
    ]


def test_abc_top_connect_body_popo1_keeps_ports_after_nested_endif():
    body = """
ABC_TOP u_abc_top
(
    .A (wa),
    .B (wb),......`ifdef POPO
    .C (wc),
`else
`ifndef IIOO
    .D (wd),
`endif
    .E (we),
`endif
);
"""
    prep = prepare_connect_body(body, defines={"POPO": "1"})
    assert prep.rstrip().endswith(");")
    assert ".C (wc)" in prep
    assert ".E (we)" not in prep
    ports = instance_port_maps(prep)
    assert ports["u_abc_top"] == [("A", "wa"), ("B", "wb"), ("C", "wc")]


def test_abc_top_ifdef_filter_popo0_iioo1_nested_ifndef():
    body = """
ABC_TOP u_abc_top
(
    .A (wa),
    .B (wb),......`ifdef POPO
    .C (wc),
`else
`ifndef IIOO
    .D (wd),
`endif
    .E (we),
`endif
);
"""
    filtered = apply_ifdef_filter(body, {"POPO": "0", "IIOO": "1"})
    assert ".D (wd)" not in filtered
    assert ".E (we)" in filtered
    assert filtered.rstrip().endswith(");")


def test_abc_top_ifndef_popo_nested_iioo_ports():
    """`` `ifndef POPO `` + else + nested `` `ifndef IIOO `` (inverse of ifdef POPO)."""
    body = """
ABC_TOP u_abc_top
(
    .A (wa),
    .B (wb),......`ifndef POPO
    .C (wc),
`else
`ifndef IIOO
    .D (wd),
`endif
    .E (we),
`endif
);
"""
    slim = slim_body_for_instance_scan(body)
    assert "`" not in slim
    ports = instance_port_maps(slim)
    assert ports["u_abc_top"] == [
        ("A", "wa"),
        ("B", "wb"),
        ("C", "wc"),
        ("D", "wd"),
        ("E", "we"),
    ]

    prep_undef = prepare_connect_body(body, defines={})
    assert ".C (wc)" in prep_undef
    assert ".D (wd)" not in prep_undef

    prep_popo1 = prepare_connect_body(body, defines={"POPO": "1"})
    assert ".C (wc)" not in prep_popo1
    assert ".D (wd)" in prep_popo1
    assert ".E (we)" in prep_popo1
    assert prep_popo1.rstrip().endswith(");")


def test_abc_top_cli_path_walk_nested_ifndef(tmp_path: Path):
    rtl = tmp_path / "top.v"
    rtl.write_text(_abc_top_rtl(), encoding="utf-8")
    (tmp_path / "design.f").write_text(f"{rtl}\n", encoding="utf-8")
    run_json = tmp_path / "run.json"
    run_json.write_text(
        json.dumps(
            {
                "filelist": "design.f",
                "top": "parent",
                "no_cache": True,
                "defines": {"POPO": "0", "IIOO": "1"},
                "run_conn_check": {
                    "enable": 1,
                    "mode": "path-walk",
                    "checks": [
                        {"id": "we", "a": "parent.we", "b": "parent.u_abc_top.E"},
                        {"id": "wd", "a": "parent.wd", "b": "parent.u_abc_top.D"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["python3", "-m", "hierwalk", str(run_json)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        check=True,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )
    conn_rows = {}
    for ln in proc.stdout.splitlines():
        if ln.startswith("#") or ln.startswith("check_id"):
            continue
        parts = ln.split("\t")
        if parts and parts[0] in ("we", "wd"):
            conn_rows[parts[0]] = parts
    assert conn_rows["we"][3] == "True"
    assert conn_rows["wd"][3] == "False"