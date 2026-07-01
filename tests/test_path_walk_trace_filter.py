"""Path-walk trace: results only, no search-step noise."""

from __future__ import annotations

import io
from pathlib import Path

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import run_path_walk_connect


def test_path_walk_abcd_trace_shows_hits_not_search(tmp_path: Path):
    (tmp_path / "a.v").write_text("module A; B B (); endmodule\n", encoding="utf-8")
    (tmp_path / "b_stub.v").write_text("module B; endmodule\n", encoding="utf-8")
    (tmp_path / "b_real.v").write_text("module B; C C (); endmodule\n", encoding="utf-8")
    (tmp_path / "c.v").write_text("module C; D D (); endmodule\n", encoding="utf-8")
    (tmp_path / "d.v").write_text("module D; endmodule\n", encoding="utf-8")
    fl_path = tmp_path / "design.f"
    fl_path.write_text(
        "\n".join(
            str((tmp_path / name).resolve())
            for name in ("a.v", "b_stub.v", "b_real.v", "c.v", "d.v")
        )
        + "\n",
        encoding="utf-8",
    )
    fl = parse_filelist(str(fl_path), index_cwd=str(tmp_path))
    buf = io.StringIO()
    request = ConnectivityRequest(
        checks=(ConnectivityCheck("A.B.C.D", "A.B.C.D"),),
        top="A",
    )
    batch, _, state = run_path_walk_connect(
        request,
        fl,
        top="A",
        no_cache=True,
        connect_phase="logical",
        trace_stream=buf,
    )
    assert "A.B.C.D" in state.rows_by_path
    assert batch.results[0].connected is True
    text = buf.getvalue()
    assert "ok A.B.C.D" in text
    assert "pw-db   edge hit" in text or "pw-db   hit" in text
    assert "pw-db tier0 scan" not in text
    assert "pw-db inst-resolve tier1 enter" not in text
    assert "pw-db tier0 expand" not in text
    assert "edge miss" not in text
    assert "walk target=" not in text