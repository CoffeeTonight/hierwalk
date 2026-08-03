// Per-lane datapath: comb case + generate-if + optional bit-slice fan
module zz_lane #(
  parameter int W = 16,
  parameter bit USE_INV = 1'b0,
  parameter int LANE_ID = 0
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [W-1:0]     a_i,
  input  logic [W-1:0]     b_i,
  input  logic [1:0]       op_i,
  input  logic             en_i,
  output logic [W-1:0]     y_o,
  output logic [W-1:0]     y_q_o
);
  import zz_pkg::*;

  logic [W-1:0] sum_w, xor_w, mux_w, inv_w, staged_d;

  assign sum_w = a_i + b_i;
  assign xor_w = a_i ^ b_i;

  // comb case mux
  always_comb begin
    unique case (op_i)
      OP_PASS: mux_w = a_i;
      OP_XOR:  mux_w = xor_w;
      OP_ADD:  mux_w = sum_w;
      default: mux_w = (LANE_ID[0]) ? b_i : a_i; // param/lane-based
    endcase
  end

  // generate-if invert or passthrough
  generate
    if (USE_INV) begin : g_inv
      assign inv_w = ~mux_w;
    end else begin : g_pass
      assign inv_w = mux_w;
    end
  endgenerate

  // parameter-based partial connect
  generate
    if (W > 8) begin : g_wide
      assign staged_d[7:0]   = inv_w[7:0];
      assign staged_d[W-1:8] = inv_w[W-1:8] ^ {W-8{en_i}};
    end else begin : g_narrow
      assign staged_d = inv_w;
    end
  endgenerate

  assign y_o = staged_d;

  // per-lane FF
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) y_q_o <= '0;
    else if (en_i) y_q_o <= staged_d;
  end
endmodule
