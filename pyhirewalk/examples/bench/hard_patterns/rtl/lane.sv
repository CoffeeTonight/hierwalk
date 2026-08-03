// One compute lane: mix of assign, always_comb, generate-if
module lane #(
  parameter int W = 16,
  parameter bit USE_INV = 1'b0
) (
  input  logic         clk,
  input  logic         rst_n,
  input  logic [W-1:0] a_i,
  input  logic [W-1:0] b_i,
  input  logic [1:0]   op_i,
  output logic [W-1:0] y_o,
  output logic [W-1:0] y_slice_o
);
  logic [W-1:0] sum_w, xor_w, mux_w, inv_w;

  assign sum_w = a_i + b_i;
  assign xor_w = a_i ^ b_i;

  always_comb begin
    unique case (op_i)
      2'b00: mux_w = sum_w;
      2'b01: mux_w = xor_w;
      2'b10: mux_w = a_i;
      default: mux_w = b_i;
    endcase
  end

  generate
    if (USE_INV) begin : g_inv
      assign inv_w = ~mux_w;
    end else begin : g_pass
      assign inv_w = mux_w;
    end
  endgenerate

  assign y_o = inv_w;
  // partial bus drive / slice fanout
  assign y_slice_o[W/2-1:0] = inv_w[W/2-1:0];
  assign y_slice_o[W-1:W/2] = inv_w[W-1:W/2];
endmodule
