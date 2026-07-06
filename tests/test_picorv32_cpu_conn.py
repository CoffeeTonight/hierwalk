"""Hierarchy + connectivity validation on YosysHQ PicoRV32/PicoSoC (GitHub RTL)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hierwalk.connect.shared.request import ConnectivityCheck, ConnectivityRequest
from hierwalk.elab import elaborate
from hierwalk.filelist import parse_filelist
from hierwalk.path_walk import (
    build_path_walk_state_from_specs,
    create_path_walk_index,
    run_path_walk_connect,
    run_path_walk_index,
)

_REPO = Path(__file__).resolve().parents[1]
_PICORV32_ROOT = _REPO / "third_party" / "picorv32"
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "picorv32_cpu"
_PICOSOC_RTL = _PICORV32_ROOT / "picosoc"

pytestmark = pytest.mark.skipif(
    not (_PICORV32_ROOT / "picorv32.v").is_file(),
    reason="clone YosysHQ/picorv32 into third_party/picorv32",
)


def _picorv32_sources(*, board: bool) -> list[Path]:
    core = [
        _PICOSOC_RTL / "picosoc.v",
        _PICORV32_ROOT / "picorv32.v",
        _PICOSOC_RTL / "spimemio.v",
        _PICOSOC_RTL / "simpleuart.v",
    ]
    if board:
        return [_FIXTURE / "picosoc_board.v", *core]
    return core


def _write_filelist(tmp_path: Path, *, board: bool) -> Path:
    fl_path = tmp_path / "picorv32.f"
    lines = [str(p.resolve()) for p in _picorv32_sources(board=board)]
    fl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fl_path


def _parse_fl(tmp_path: Path, *, board: bool):
    fl_path = _write_filelist(tmp_path, board=board)
    return parse_filelist(str(fl_path), index_cwd=str(tmp_path))


@pytest.fixture
def picosoc_fl(tmp_path: Path):
    return _parse_fl(tmp_path, board=False)


@pytest.fixture
def board_fl(tmp_path: Path):
    return _parse_fl(tmp_path, board=True)


def test_picorv32_index_registers_soc_children(picosoc_fl):
    index, _db = create_path_walk_index(
        picosoc_fl, "picosoc", defines=dict(picosoc_fl.defines), no_cache=True
    )
    _, rows = elaborate(index, "picosoc")
    paths = {r.full_path for r in rows}
    assert "picosoc.cpu" in paths
    assert "picosoc.spimemio" in paths
    assert "picosoc.simpleuart" in paths
    assert "picosoc.memory" in paths


def test_picorv32_hierarchy_walk_cpu_uart_spi(picosoc_fl):
    specs = [
        "picosoc.cpu",
        "picosoc.simpleuart",
        "picosoc.spimemio",
        "picosoc.memory",
    ]
    _index, state, _top = run_path_walk_index(
        picosoc_fl, specs, top="picosoc", no_cache=True
    )
    for spec in specs:
        assert spec in state.rows_by_path, spec


def test_picorv32_connect_text_phase_only(picosoc_fl):
    """Text-conn alone must pass on inst port-map probes (no logical phase)."""
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck("picosoc.clk", "picosoc.cpu.clk", check_id="clk_cpu"),
            ConnectivityCheck(
                "picosoc.ser_tx", "picosoc.simpleuart.ser_tx", check_id="uart_tx"
            ),
            ConnectivityCheck(
                "picosoc.flash_csb",
                "picosoc.spimemio.flash_csb",
                check_id="flash_csb",
            ),
        ),
        top="picosoc",
        include_ff=True,
    )
    batch, _index, _state = run_path_walk_connect(
        request,
        picosoc_fl,
        top="picosoc",
        no_cache=True,
        connect_phase="text",
    )
    for r in batch.results:
        assert r.connected_text is True, (
            f"{r.check_id}: text={r.connected_text} note={r.note} errors={r.errors}"
        )


def test_picorv32_connect_inst_port_maps(picosoc_fl):
    """Top-level bus pins reach child peripheral ports through picosoc."""
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck("picosoc.clk", "picosoc.cpu.clk", check_id="clk_cpu"),
            ConnectivityCheck(
                "picosoc.ser_tx", "picosoc.simpleuart.ser_tx", check_id="uart_tx"
            ),
            ConnectivityCheck(
                "picosoc.flash_csb",
                "picosoc.spimemio.flash_csb",
                check_id="flash_csb",
            ),
        ),
        top="picosoc",
        include_ff=True,
    )
    batch, _index, state = run_path_walk_connect(
        request, picosoc_fl, top="picosoc", no_cache=True
    )
    failures = [r for r in batch.results if not r.connected]
    assert not failures, "\n".join(
        f"{r.check_id}: text={r.connected_text} logical={r.connected_logical} "
        f"note={r.note} errors={r.errors}"
        for r in failures
    )
    assert "picosoc.cpu" in state.rows_by_path


def test_picorv32_board_hierarchy_depth(board_fl):
    index, mod_db = create_path_walk_index(
        board_fl, "picosoc_board", defines=dict(board_fl.defines), no_cache=True
    )
    paths = [
        "picosoc_board.soc.cpu",
        "picosoc_board.soc.simpleuart",
    ]
    state = build_path_walk_state_from_specs(index, "picosoc_board", paths, mod_db)
    for path in paths:
        assert path in state.rows_by_path, path
        row = state.rows_by_path[path]
        assert row.depth == 2, (path, row.depth)


def test_picorv32_board_gpio_and_debug_taps(board_fl):
    """Board-level assigns and GPIO slice reach SoC/external taps."""
    top = "picosoc_board"
    request = ConnectivityRequest(
        checks=(
            ConnectivityCheck(
                f"{top}.leds",
                f"{top}.gpio[7:0]",
                check_id="led_gpio_bus",
            ),
            ConnectivityCheck(
                f"{top}.debug_ser_tx",
                f"{top}.ser_tx",
                check_id="debug_uart_tap",
            ),
            ConnectivityCheck(
                f"{top}.debug_flash_csb",
                f"{top}.soc.flash_csb",
                check_id="debug_flash_tap",
            ),
            ConnectivityCheck(
                f"{top}.clk",
                f"{top}.soc.cpu.clk",
                check_id="board_clk_cpu",
            ),
        ),
        top=top,
        include_ff=True,
    )
    batch, _index, _state = run_path_walk_connect(
        request, board_fl, top=top, no_cache=True
    )
    failures = [r for r in batch.results if not r.connected]
    assert not failures, "\n".join(
        f"{r.check_id}: {r.endpoint_a.spec} -> {r.endpoint_b.spec} "
        f"note={r.note} errors={r.errors}"
        for r in failures
    )