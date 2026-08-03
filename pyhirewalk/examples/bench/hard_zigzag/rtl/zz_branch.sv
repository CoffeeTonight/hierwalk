// Deliberate multi-branch fan-out / reconverge for COI exploration stress.
//
// Live forest (from d_i):
//   d_i ──┬── g_arm[0] comb_t0 ── FF ── arm_q[0] ──┐
//         ├── g_arm[1] comb_t1 ── FF ── arm_q[1] ──┤
//         ├── ...                                  ├── nested case mux ── join_o
//         └── g_arm[B-1] ...                       │
//                                                  └── OR-tree (pairwise) ── any_o
//
// Dead forest (from noise_i only — never joins join_o / any_o):
//   noise_i ── g_dead[0..D-1] FF ── dead_q[*]
//
// Ground truth (structural):
//   d_i → every arm_q[i], join_o, any_o   CONNECTED
//   noise_i → dead_q[j]                   CONNECTED
//   noise_i ↛ join_o / any_o / arm_q[*]   DISCONNECTED
//   arm_q[i] ↛ dead_q[j]                  DISCONNECTED
module zz_branch #(
  parameter int W = 16,
  parameter int B = 8,  // live arms (fan-out width)
  parameter int D = 4   // dead / noise arms
) (
  input  logic                 clk,
  input  logic                 rst_n,
  input  logic                 en_i,
  input  logic [W-1:0]         d_i,
  input  logic [W-1:0]         noise_i,
  input  logic [2:0]           br_sel_i,
  output logic [W-1:0]         join_o,
  output logic [W-1:0]         any_o,
  output logic [B-1:0][W-1:0]  arm_q_o,
  output logic [D-1:0][W-1:0]  dead_q_o
);
  logic [B-1:0][W-1:0] arm_d;
  logic [B-1:0][W-1:0] arm_q;
  logic [D-1:0][W-1:0] dead_q;

  // ---- live arms: generate-for fan-out with distinct comb transforms ----
  genvar bi;
  generate
    for (bi = 0; bi < B; bi++) begin : g_arm
      if ((bi % 4) == 0) begin : t0
        assign arm_d[bi] = d_i;
      end else if ((bi % 4) == 1) begin : t1
        assign arm_d[bi] = ~d_i;
      end else if ((bi % 4) == 2) begin : t2
        assign arm_d[bi] = {d_i[0], d_i[W-1:1]};
      end else begin : t3
        assign arm_d[bi] = d_i ^ W'(bi);
      end

      always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) arm_q[bi] <= '0;
        else if (en_i) arm_q[bi] <= arm_d[bi];
      end

      assign arm_q_o[bi] = arm_q[bi];
    end
  endgenerate

  // ---- nested 2-level case mux (more branch nodes than a flat case) ----
  // lo covers arms 0..3, hi covers 4..7; br_sel_i[2] picks bank.
  logic [W-1:0] lo_mux, hi_mux;

  always_comb begin
    unique case (br_sel_i[1:0])
      2'd0:    lo_mux = arm_q[0];
      2'd1:    lo_mux = arm_q[1];
      2'd2:    lo_mux = arm_q[2];
      default: lo_mux = arm_q[3];
    endcase
  end

  always_comb begin
    unique case (br_sel_i[1:0])
      2'd0:    hi_mux = arm_q[4];
      2'd1:    hi_mux = arm_q[5];
      2'd2:    hi_mux = arm_q[6];
      default: hi_mux = arm_q[7];
    endcase
  end

  always_comb begin
    unique case (br_sel_i[2])
      1'b0:    join_o = lo_mux;
      default: join_o = hi_mux;
    endcase
  end

  // ---- pairwise OR-tree: every arm reconverges into any_o ----
  // Level 0: 8 → 4
  logic [W-1:0] or0_0, or0_1, or0_2, or0_3;
  assign or0_0 = arm_q[0] | arm_q[1];
  assign or0_1 = arm_q[2] | arm_q[3];
  assign or0_2 = arm_q[4] | arm_q[5];
  assign or0_3 = arm_q[6] | arm_q[7];
  // Level 1: 4 → 2
  logic [W-1:0] or1_0, or1_1;
  assign or1_0 = or0_0 | or0_1;
  assign or1_1 = or0_2 | or0_3;
  // Level 2: 2 → 1
  assign any_o = or1_0 | or1_1;

  // ---- dead forest: noise only, no structural path into join/any ----
  genvar di;
  generate
    for (di = 0; di < D; di++) begin : g_dead
      always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) dead_q[di] <= '0;
        else if (en_i) dead_q[di] <= noise_i ^ W'(di);
      end
      assign dead_q_o[di] = dead_q[di];
    end
  endgenerate
endmodule
