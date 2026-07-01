"""Path-walk searches the parent node's filelist subtree before the whole design."""

from __future__ import annotations

from pathlib import Path

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect


def _write_nested_soc_with_global_stub(tmp_path: Path) -> tuple[Path, str]:
    """
    soc.f lists soc_top.v and includes block.f (real ABD).
    global.f lists abd_stub.v (wrong duplicate) — unrelated filelist.
    """
    (tmp_path / "soc_top.v").write_text(
        """
        module SOC_TOP;
          ABD u_abd_inst ();
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "abd_real.v").write_text(
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
    (tmp_path / "abd_stub.v").write_text(
        """
        module ABD;
        endmodule
        """,
        encoding="utf-8",
    )
    block_f = tmp_path / "block.f"
    block_f.write_text(
        "\n".join(
            str((tmp_path / name).resolve())
            for name in ("abd_real.v", "def.v", "ghi.v")
        )
        + "\n",
        encoding="utf-8",
    )
    global_f = tmp_path / "global.f"
    global_f.write_text(f"{(tmp_path / 'abd_stub.v').resolve()}\n", encoding="utf-8")
    soc_f = tmp_path / "soc.f"
    soc_f.write_text(
        f"-f {block_f.name}\n{(tmp_path / 'soc_top.v').resolve()}\n",
        encoding="utf-8",
    )
    mega_f = tmp_path / "mega.f"
    mega_f.write_text(
        f"-f {soc_f.name}\n-f {global_f.name}\n",
        encoding="utf-8",
    )
    leaf = "SOC_TOP.u_abd_inst.u_def_inst.u_ghi_inst"
    return mega_f, leaf


def _write_cross_filelist_soc_dup(tmp_path: Path) -> tuple[Path, str]:
    """SOC_TOP stub in soc.f; real body with child inst only in another filelist."""
    (tmp_path / "soc_top_stub.v").write_text(
        "module SOC_TOP;\nendmodule\n",
        encoding="utf-8",
    )
    (tmp_path / "soc_top_real.v").write_text(
        """
        module SOC_TOP;
          CPUSYSTEM_TOP u_cpusystem_top (.clk(clk));
        endmodule
        module CPUSYSTEM_TOP;
        endmodule
        """,
        encoding="utf-8",
    )
    (tmp_path / "soc.f").write_text(
        f"{(tmp_path / 'soc_top_stub.v').resolve()}\n",
        encoding="utf-8",
    )
    (tmp_path / "global.f").write_text(
        f"{(tmp_path / 'soc_top_real.v').resolve()}\n",
        encoding="utf-8",
    )
    mega_f = tmp_path / "mega.f"
    mega_f.write_text(f"-f soc.f\n-f global.f\n", encoding="utf-8")
    return mega_f, "SOC_TOP.u_cpusystem_top"


def test_path_walk_falls_back_to_global_dup_when_scoped_stub_lacks_inst(tmp_path: Path):
    fl_path, leaf = _write_cross_filelist_soc_dup(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    request = ConnectivityRequest(
        checks=(ConnectivityCheck(leaf, leaf),),
        top="SOC_TOP",
    )
    batch, index, state = run_path_walk_connect(
        request,
        fl,
        top="SOC_TOP",
        no_cache=True,
    )
    assert leaf in state.rows_by_path
    assert batch.results[0].connected is True
    soc = index.get_module("SOC_TOP")
    assert soc is not None
    assert Path(soc.file_path).name == "soc_top_real.v"


def test_path_walk_prefers_filelist_subtree_over_unrelated_duplicate(tmp_path: Path):
    fl_path, leaf = _write_nested_soc_with_global_stub(tmp_path)
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    request = ConnectivityRequest(
        checks=(ConnectivityCheck(leaf, leaf),),
        top="SOC_TOP",
    )
    batch, index, state = run_path_walk_connect(
        request,
        fl,
        top="SOC_TOP",
        no_cache=True,
    )
    assert leaf in state.rows_by_path, sorted(state.rows_by_path)
    assert batch.results[0].connected is True
    abd = index.get_module("ABD")
    assert abd is not None
    assert Path(abd.file_path).name == "abd_real.v"