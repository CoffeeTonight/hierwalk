// coi_ladder — FF-depth ladder for coi_until (NOT hard_zigzag).
//
// Ground-truth fan-out from seed a_i:
//
//   a_i
//     --comb--> pre_w                 (ff=0)
//                 |-- pure_comb_o     (ff=0 leaf)
//                 |--FF#1--> s0_q
//                              |-- short_o          (ff=1 leaf; early end)
//                              |--FF#2--> s1_q      (ff=2  → stop for FF:2)
//                              |            |-- mid_o   (comb of s1; only if expand)
//                              |            +--FF#3--> s2_q --> deep_o
//                              +--FF#2--> fork_q[i] (parallel arms, each 2nd FF)
//
// noise_i --> dead_q only (never on a_i cone).
//
// Answer key for until=FF:2, direction=fanout, seed=a_i:
//   must_satisfy: s1_q, fork_q[0], fork_q[1]  (2nd FF destinations)
//   must_not_coi: s2_q, deep_o, dead_q        (3rd FF / other forest)
//   exhausted-ish: pure_comb_o (0 FF), short_o (1 FF) if they are leaves
module coi_top #(
  parameter int W = coi_pkg::W,
  parameter int N_FORK = coi_pkg::N_FORK
) (
  input  logic         clk,
  input  logic         rst_n,
  input  logic         en_i,
  input  logic [W-1:0] a_i,
  input  logic [W-1:0] b_i,
  input  logic [W-1:0] noise_i,
  output logic [W-1:0] pure_comb_o,
  output logic [W-1:0] short_o,
  output logic [W-1:0] mid_o,
  output logic [W-1:0] deep_o,
  output logic [W-1:0] s0_q,
  output logic [W-1:0] s1_q,
  output logic [W-1:0] s2_q,
  output logic [N_FORK-1:0][W-1:0] fork_q_o,
  output logic [W-1:0] dead_o
);
  import coi_pkg::*;

  logic [W-1:0] pre_w;
  logic [W-1:0] s0_w, s1_w, s2_w;
  logic [N_FORK-1:0][W-1:0] fork_d;
  logic [N_FORK-1:0][W-1:0] fork_q;
  logic [W-1:0] dead_q;

  // comb only (0 FF)
  assign pre_w       = a_i ^ b_i;
  assign pure_comb_o = pre_w;

  // FF #1
  coi_stage #(.W(W)) u_s0 (
    .clk(clk),
    .rst_n(rst_n),
    .en_i(en_i),
    .d_i(pre_w),
    .q_o(s0_w)
  );
  assign s0_q   = s0_w;
  assign short_o = s0_w;  // 1-FF leaf (no further local fan-out from short_o)

  // FF #2 — main ladder
  coi_stage #(.W(W)) u_s1 (
    .clk(clk),
    .rst_n(rst_n),
    .en_i(en_i),
    .d_i(s0_w),
    .q_o(s1_w)
  );
  assign s1_q = s1_w;
  assign mid_o = s1_w;  // comb view of 2nd FF (reachable only if expand past sat)

  // FF #3 — beyond FF:2 budget; must NOT enter COI when until=FF:2 stops at s1
  coi_stage #(.W(W)) u_s2 (
    .clk(clk),
    .rst_n(rst_n),
    .en_i(en_i),
    .d_i(s1_w),
    .q_o(s2_w)
  );
  assign s2_q  = s2_w;
  assign deep_o = s2_w;

  // Parallel 2nd-FF arms from s0 (branchy, still FF:2)
  genvar gi;
  generate
    for (gi = 0; gi < N_FORK; gi++) begin : g_fork
      assign fork_d[gi] = s0_w ^ W'(gi);
      coi_stage #(.W(W)) u_fk (
        .clk(clk),
        .rst_n(rst_n),
        .en_i(en_i),
        .d_i(fork_d[gi]),
        .q_o(fork_q[gi])
      );
      assign fork_q_o[gi] = fork_q[gi];
    end
  endgenerate

  // Dead forest — only noise_i
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) dead_q <= '0;
    else if (en_i) dead_q <= noise_i;
  end
  assign dead_o = dead_q;
endmodule
