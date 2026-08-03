// Top: deliberately zig-zag hierarchy for COI stress
//
// Intended hard path (RTL ground truth):
//   seed_a  a_i
//     -> g_lane[k].u_lane.a_i (port, generate-for)
//     -> ... comb case / gen-if ... -> y_q_o (FF1)
//     -> bus_q[k] array
//     -> u_xbar.in_i / case / generate-for bits -> xbar_o
//     -> u_branch.d_i  **FAN-OUT** g_arm[0..B-1] each FF
//         -> nested case mux -> join_o  (and OR-tree -> any_o)
//     -> u_pipe.d_i -> q_s[*] multi-FF (STAGES) -> pipe_q / mid_o
//     -> u_zig.east_i / north_i (zigzag) comb+ifdef -> west_o / south_q_o (FF)
//     -> result_o / result_q_o
//
// Dead forest (noise_i only): u_branch.g_dead[*] — never joins join_o.
// Alternate ifdef path changes only zig alt_w polarity, not topology.
module zz_top #(
  parameter int W = zz_pkg::W,
  parameter int N = zz_pkg::N,
  parameter int STAGES = zz_pkg::STAGES,
  parameter int B_ARMS = zz_pkg::B_ARMS,
  parameter int D_ARMS = zz_pkg::D_ARMS,
  parameter bit USE_INV = zz_pkg::USE_INV
) (
  input  logic         clk,
  input  logic         rst_n,
  input  logic [W-1:0] a_i,
  input  logic [W-1:0] b_i,
  input  logic [W-1:0] noise_i,
  input  logic [1:0]   op_i,
  input  logic [1:0]   lane_sel_i,
  input  logic [2:0]   br_sel_i,
  input  logic [1:0]   zig_mode_i,
  input  logic         en_i,
  output logic [W-1:0] result_o,
  output logic [W-1:0] result_q_o,
  output logic [W-1:0] pipe_mid_o,
  output logic [W-1:0] lane0_y_q_o,
  output logic [W-1:0] branch_join_o,
  output logic [W-1:0] branch_any_o,
  output logic [W-1:0] branch_arm0_o,
  output logic [W-1:0] branch_arm3_o,
  output logic [W-1:0] branch_arm7_o,
  output logic [W-1:0] dead0_o
);
  import zz_pkg::*;

  logic [N-1:0][W-1:0] lane_y;
  logic [N-1:0][W-1:0] lane_yq;
  logic [W-1:0]        xbar_o;
  logic [W-1:0]        branch_join;
  logic [W-1:0]        branch_any;
  logic [B_ARMS-1:0][W-1:0] branch_arm_q;
  logic [D_ARMS-1:0][W-1:0] branch_dead_q;
  logic [W-1:0]        pipe_q;
  logic [W-1:0]        pipe_mid;
  logic [W-1:0]        zig_west;
  logic [W-1:0]        zig_south_q;

  // ---- generate-for lanes; generate-if USE_INV alternates by lane index ----
  genvar gi;
  generate
    for (gi = 0; gi < N; gi++) begin : g_lane
      // parameter-based invert: even lanes follow USE_INV, odd invert sense
      localparam bit LANE_INV = USE_INV ^ gi[0];
      zz_lane #(
        .W(W),
        .USE_INV(LANE_INV),
        .LANE_ID(gi)
      ) u_lane (
        .clk(clk),
        .rst_n(rst_n),
        .a_i(a_i),
        .b_i(b_i),
        .op_i(op_i),
        .en_i(en_i),
        .y_o(lane_y[gi]),
        .y_q_o(lane_yq[gi])
      );
    end
  endgenerate

  // zig: feed xbar from *registered* lane outputs (force FF before xbar)
  zz_xbar #(.W(W), .N(N)) u_xbar (
    .in_i(lane_yq),
    .sel_i(lane_sel_i),
    .out_o(xbar_o)
  );

  // multi-branch fan-out / nested mux reconverge / dead noise forest
  zz_branch #(
    .W(W),
    .B(B_ARMS),
    .D(D_ARMS)
  ) u_branch (
    .clk(clk),
    .rst_n(rst_n),
    .en_i(en_i),
    .d_i(xbar_o),
    .noise_i(noise_i),
    .br_sel_i(br_sel_i),
    .join_o(branch_join),
    .any_o(branch_any),
    .arm_q_o(branch_arm_q),
    .dead_q_o(branch_dead_q)
  );

  // multi-stage pipe on *joined* branch result (main path through fan-out)
  zz_pipe #(.W(W), .STAGES(STAGES)) u_pipe (
    .clk(clk),
    .rst_n(rst_n),
    .en_i(en_i),
    .d_i(branch_join),
    .q_o(pipe_q),
    .mid_o(pipe_mid)
  );

  // zigzag: east=xbar path after pipe q, north=mid (earlier FF stage)
  // This creates a re-convergent multi-FF structure.
  zz_zig #(.W(W)) u_zig (
    .clk(clk),
    .rst_n(rst_n),
    .east_i(pipe_q),
    .north_i(pipe_mid),
    .mode_i(zig_mode_i),
    .west_o(zig_west),
    .south_q_o(zig_south_q)
  );

  // top outs: comb + final registered + branch observability
  assign result_o      = zig_west;
  assign result_q_o    = zig_south_q;
  assign pipe_mid_o    = pipe_mid;
  assign lane0_y_q_o   = lane_yq[0];
  assign branch_join_o = branch_join;
  assign branch_any_o  = branch_any;
  assign branch_arm0_o = branch_arm_q[0];
  assign branch_arm3_o = branch_arm_q[3];
  assign branch_arm7_o = branch_arm_q[7];
  assign dead0_o       = branch_dead_q[0];

  // ifdef dead-ish side path (still elaborates under ZZ_SIDE)
`ifdef ZZ_SIDE
  logic [W-1:0] side_q;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) side_q <= '0;
    else side_q <= a_i ^ result_o;
  end
`endif
endmodule
