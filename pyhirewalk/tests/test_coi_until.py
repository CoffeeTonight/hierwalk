"""Unit tests for COI-until stop conditions (no pyslang, no pytest)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from pyhirewalk.conn.coi_until import (  # noqa: E402
    StopCond,
    _edge_bucket,
    _edge_counts_toward,
    _net_matches,
    coi_until,
    verify_expect,
)
from pyhirewalk.conn.pyslang_app import Edge, Graph, netkey  # noqa: E402


def test_parse_ff() -> None:
    c = StopCond.parse("FF:2")
    assert c.kind == "ff" and c.limit == 2
    assert StopCond.parse("proc:3").kind == "ff"


def test_parse_bad() -> None:
    try:
        StopCond.parse("nope")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_edge_buckets() -> None:
    assert _edge_bucket("proc", "proc") == "ff"
    assert _edge_bucket("assign", "assign") == "assign"
    assert _edge_counts_toward("ff", "ff")
    assert not _edge_counts_toward("ff", "assign")
    assert _edge_counts_toward("hop", "assign")


def _toy_graph() -> Graph:
    """a --assign--> w --proc--> q1 --assign--> short
                         \\
                          --proc--> q2 --proc--> q3
    """
    g = Graph()
    a = netkey("t", "a")
    w = netkey("t", "w")
    q1 = netkey("t", "q1")
    short = netkey("t", "short")
    q2 = netkey("t", "q2")
    q3 = netkey("t", "q3")
    g.inst_info["t"] = {"module": "t"}
    g.add(a, Edge(dst=w, kind="assign", evidence={"via": "assign", "snippet": "w=a"}))
    g.add(w, Edge(dst=q1, kind="proc", evidence={"via": "proc", "snippet": "q1<=w"}))
    g.add(
        q1,
        Edge(dst=short, kind="assign", evidence={"via": "assign", "snippet": "short=q1"}),
    )
    g.add(w, Edge(dst=q2, kind="proc", evidence={"via": "proc", "snippet": "q2<=w"}))
    g.add(q2, Edge(dst=q3, kind="proc", evidence={"via": "proc", "snippet": "q3<=q2"}))
    return g


def test_ff1_stops_before_second_ff() -> None:
    g = _toy_graph()
    r = coi_until(g, ["t.a"], until="FF:1", direction="fanout")
    nets = set(r.nodes)
    assert netkey("t", "q1") in nets
    assert netkey("t", "short") in nets  # alias after 1st FF
    assert netkey("t", "q2") in nets
    assert netkey("t", "q3") not in nets  # 2nd FF on that arm blocked
    assert all(n.counters.ff <= 1 for n in r.nodes.values())


def test_ff2_reaches_q3() -> None:
    g = _toy_graph()
    r = coi_until(g, ["t.a"], until="FF:2", direction="fanout")
    assert netkey("t", "q3") in r.nodes
    assert r.nodes[netkey("t", "q3")].counters.ff == 2
    assert all(n.counters.ff <= 2 for n in r.nodes.values())


def test_early_exhaust_on_short_branch() -> None:
    g = Graph()
    g.inst_info["t"] = {}
    a = netkey("t", "a")
    leaf = netkey("t", "leaf")
    g.add(a, Edge(dst=leaf, kind="assign", evidence={"via": "assign"}))
    r = coi_until(g, ["t.a"], until="FF:2")
    assert r.nodes[leaf].status == "exhausted"
    assert r.nodes[leaf].counters.ff == 0


def test_diamond_reconverge_not_false_exhausted() -> None:
    """a→b→d and a→c→d: later arm must not mark c exhausted just because d visited."""
    g = Graph()
    g.inst_info["t"] = {}
    a, b, c, d = (netkey("t", x) for x in "abcd")
    for s, t in ((a, b), (a, c), (b, d), (c, d)):
        g.add(s, Edge(dst=t, kind="assign", evidence={"via": "assign"}))
    r = coi_until(g, ["t.a"], until="FF:5")
    assert r.nodes[a].status == "internal"
    assert r.nodes[b].status == "internal"
    # c is a join feeder, not a dead leaf
    assert r.nodes[c].status == "internal", r.nodes[c].status
    assert r.nodes[d].status == "exhausted"  # true structural leaf
    assert c not in r.exhausted
    assert d in r.exhausted


def test_net_matches_strict() -> None:
    assert _net_matches("zz_top.mid_o", "zz_top.mid_o")
    assert _net_matches("mid_o", "zz_top.u_pipe.mid_o")
    assert _net_matches("u_pipe.mid_o", "zz_top.u_pipe.mid_o")
    assert _net_matches("arm_q_o[0]", "zz_top.u_branch.arm_q_o[0]")
    # bare substring must not match
    assert not _net_matches("q", "zz_top.y_q_o")
    assert not _net_matches("mid", "zz_top.u_pipe.mid_o")
    assert not _net_matches("s0", "zz_top.s0_q")


def test_verify_expect_match_and_dedupe() -> None:
    g = _toy_graph()
    r = coi_until(g, ["t.a"], until="FF:1")
    # satisfied should be unique
    assert len(r.satisfied) == len(set(r.satisfied))
    ok = verify_expect(
        r,
        {
            "must_satisfy": ["t.q1", "short"],
            "must_not_coi": ["t.q3"],
            "max_ff_in_coi": 1,
        },
    )
    assert ok["pass"], ok["failures"]
    bad = verify_expect(r, {"must_satisfy": ["t.q3"]})
    assert not bad["pass"]


def test_skip_array_el_optional_strict_mode() -> None:
    """skip_array_el=True drops all array_el links (strict opt-in)."""
    g = Graph()
    g.inst_info["t"] = {}
    d = netkey("t", "d")
    q0 = netkey("t", "q", "[0]")
    whole = netkey("t", "q", "")
    g.add(d, Edge(dst=q0, kind="proc", evidence={"via": "proc"}))
    g.add(q0, Edge(dst=whole, kind="array_el", evidence={"via": "array_el"}))
    r = coi_until(g, ["t.d"], until="FF:1", direction="fanout", skip_array_el=True)
    assert q0 in r.nodes
    assert whole not in r.nodes
    r2 = coi_until(g, ["t.d"], until="FF:1", direction="fanout", skip_array_el=False)
    assert whole in r2.nodes


def test_hard_zigzag_no_false_mid_from_qs0() -> None:
    """Shipped graph extract: STAGES=3 elaborates g_mid (q_s[1]), not g_mid0 (q_s[0])."""
    import time
    from pathlib import Path

    from pyhirewalk.conn.pyslang_app import (
        apply_config_env,
        build_graph,
        compile_design,
        load_rtl_sources,
        netkey_fmt,
    )
    from pyhirewalk.run_config import load_run_config

    cfg_path = _ROOT / "examples/bench/configs/coi_zigzag.json"
    assert cfg_path.is_file(), f"missing required config {cfg_path}"
    t0 = time.perf_counter()
    cfg = load_run_config(cfg_path)
    apply_config_env(dict(cfg.env), t0=t0)
    files, inc, _defs, _err = load_rtl_sources(
        cfg.filelist, env=dict(cfg.env), index_cwd=cfg.index_cwd, t0=t0
    )
    params = {str(k): str(v) for k, v in (cfg.raw.get("parameters") or {}).items()}
    assert params.get("STAGES") == "3"
    comp, root, _diags, fatal = compile_design(
        files=files,
        top="zz_top",
        defines=dict(cfg.defines),
        includes=inc,
        t0=t0,
        parameters=params,
    )
    assert not fatal
    g = build_graph(root, comp.sourceManager, t0=t0)
    mid_drivers = []
    for src, edges in g.forward.items():
        for e in edges:
            if e.dst[1] == "mid_o" and "u_pipe" in e.dst[0] and e.kind == "assign":
                mid_drivers.append(netkey_fmt(src))
    # True tap only
    assert any(d.endswith("q_s[1]") for d in mid_drivers), mid_drivers
    # False uninstantiated g_mid0 must not contribute
    assert not any(d.endswith("q_s[0]") for d in mid_drivers), (
        f"false mid edge from q_s[0] still present: {mid_drivers}"
    )
    # COI-until: mid_o first-arrival needs 4 FFs (lane+branch+qs0+qs1)
    r = coi_until(g, ["zz_top.a_i"], until="FF:5", direction="fanout")
    mid_keys = [k for k in r.nodes if k[1] == "mid_o" and "u_pipe" in k[0]]
    assert mid_keys, "mid_o not in COI"
    assert r.nodes[mid_keys[0]].counters.ff == 4, r.nodes[mid_keys[0]].counters
    res_keys = [k for k in r.nodes if k[1] == "result_q_o"]
    assert res_keys
    assert r.nodes[res_keys[0]].counters.ff == 5, r.nodes[res_keys[0]].counters


def main() -> int:
    test_parse_ff()
    test_parse_bad()
    test_edge_buckets()
    test_net_matches_strict()
    test_verify_expect_match_and_dedupe()
    test_ff1_stops_before_second_ff()
    test_ff2_reaches_q3()
    test_early_exhaust_on_short_branch()
    test_diamond_reconverge_not_false_exhausted()
    test_skip_array_el_optional_strict_mode()
    test_hard_zigzag_no_false_mid_from_qs0()
    print("test_coi_until: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
