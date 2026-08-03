// Multi-stage FF pipeline (STAGES deep) + comb tap
module zz_pipe #(
  parameter int W = 16,
  parameter int STAGES = 3
) (
  input  logic         clk,
  input  logic         rst_n,
  input  logic         en_i,
  input  logic [W-1:0] d_i,
  output logic [W-1:0] q_o,
  output logic [W-1:0] mid_o  // comb tap of stage1
);
  logic [STAGES-1:0][W-1:0] q_s;

  genvar si;
  generate
    for (si = 0; si < STAGES; si++) begin : g_st
      if (si == 0) begin : g0
        always_ff @(posedge clk or negedge rst_n) begin
          if (!rst_n) q_s[0] <= '0;
          else if (en_i) q_s[0] <= d_i;
        end
      end else begin : gn
        always_ff @(posedge clk or negedge rst_n) begin
          if (!rst_n) q_s[si] <= '0;
          else if (en_i) q_s[si] <= q_s[si-1];
        end
      end
    end
  endgenerate

  assign q_o   = q_s[STAGES-1];
  // mid tap: parameter-dependent which stage
  generate
    if (STAGES >= 2) begin : g_mid
      assign mid_o = q_s[1];
    end else begin : g_mid0
      assign mid_o = q_s[0];
    end
  endgenerate
endmodule
