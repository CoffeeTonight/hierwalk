"""
Path-walk: hierarchy paths use instance names only (not module type names).

SoC-realistic chain — module decl names and inst names are unrelated, e.g.
``ABD DEF ();`` not ``ABD ABD ();``.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect


def _write_realistic_soc_chain(tmp_path: Path) -> tuple[Path, str]:
    """Four modules, four unrelated instance names, separate RTL files."""
    (tmp_path / "hc_top.v").write_text(
        """
        module hc_top;
          BLK_NOC u_noc_wrap ();
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "blk_noc.v").write_text(
        """
        module BLK_NOC;
          CPU_CLST u_cpu_clst ();
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "cpu_clst.v").write_text(
        """
        module CPU_CLST;
          L2_CACHE u_l2_mem ();
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "l2_cache.v").write_text(
        """
        module L2_CACHE;
          SRAM_CTRL u_sram_ctrl ();
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "sram_ctrl.v").write_text(
        """
        module SRAM_CTRL;
        endmodule
        """,
        encoding="utf-8",
    )
    fl = tmp_path / "design.f"
    fl.write_text(
        "\n".join(
            str((tmp_path / name).resolve())
            for name in (
                "hc_top.v",
                "blk_noc.v",
                "cpu_clst.v",
                "l2_cache.v",
                "sram_ctrl.v",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    leaf = "hc_top.u_noc_wrap.u_cpu_clst.u_l2_mem.u_sram_ctrl"
    return fl, leaf


def _write_abd_def_style(tmp_path: Path) -> tuple[Path, str]:
    """User-style ``ABD DEF ();`` — cell type and inst name fully unrelated."""
    (tmp_path / "top.v").write_text(
        """
        module SOC_TOP;
          ABD u_abd_inst ();
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "abd.v").write_text(
        """
        module ABD;
          DEF u_def_inst ();
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "def.v").write_text(
        """
        module DEF;
          GHI u_ghi_inst ();
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "ghi.v").write_text(
        """
        module GHI;
        endmodule
        """,
        encoding="utf-8",
    )
    fl = tmp_path / "design.f"
    fl.write_text(
        "\n".join(
            str((tmp_path / n).resolve()) for n in ("top.v", "abd.v", "def.v", "ghi.v")
        )
        + "\n",
        encoding="utf-8",
    )
    leaf = "SOC_TOP.u_abd_inst.u_def_inst.u_ghi_inst"
    return fl, leaf


@pytest.mark.parametrize(
    "factory",
    [_write_realistic_soc_chain, _write_abd_def_style],
    ids=["soc_chain", "abd_def_ghi"],
)
def test_path_walk_succeeds_with_real_instance_names(tmp_path: Path, factory):
    fl_path, leaf = factory(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    request = ConnectivityRequest(
        checks=(ConnectivityCheck(leaf, leaf),),
        top=leaf.split(".", 1)[0],
    )
    batch, index, state = run_path_walk_connect(
        request,
        fl,
        top=request.top,
        no_cache=True,
    )
    assert leaf in state.rows_by_path, state.rows_by_path.keys()
    assert batch.results[0].connected is True

    parts = leaf.split(".")
    for i in range(1, len(parts)):
        prefix = ".".join(parts[: i + 1])
        row = state.rows_by_path[prefix]
        assert row.inst_leaf == parts[i]
        assert row.inst_leaf != row.module or i == 0


def test_path_walk_fails_when_path_uses_module_type_names(tmp_path: Path):
    """Module-type segments (int int;) must not resolve real inst-name RTL."""
    fl_path, leaf = _write_abd_def_style(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    wrong = "SOC_TOP.ABD.DEF.GHI"
    request = ConnectivityRequest(
        checks=(ConnectivityCheck(wrong, wrong),),
        top="SOC_TOP",
    )
    batch, _, state = run_path_walk_connect(
        request,
        fl,
        top="SOC_TOP",
        no_cache=True,
    )
    assert wrong not in state.rows_by_path
    assert not batch.results[0].connected


def test_path_walk_trace_shows_real_inst_names_not_module_types(tmp_path: Path):
    fl_path, leaf = _write_abd_def_style(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    buf = io.StringIO()
    request = ConnectivityRequest(
        checks=(ConnectivityCheck(leaf, leaf),),
        top="SOC_TOP",
    )
    run_path_walk_connect(
        request,
        fl,
        top="SOC_TOP",
        no_cache=True,
        trace_stream=buf,
    )
    text = buf.getvalue()
    assert "ok SOC_TOP.u_abd_inst" in text
    assert "ok " + leaf in text
    assert "u_def_inst" in text
    assert "u_ghi_inst" in text
    # module-type fantasy path must not appear as walked ok rows
    assert "ok SOC_TOP.ABD" not in text