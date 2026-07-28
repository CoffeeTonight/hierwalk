// Alternate leaf when FEATURE_A is off.
module leaf_b #(
  parameter int W = 8
) (
  input  logic         clk,
  input  logic [W-1:0] din,
  output logic [W-1:0] dout
);
  always_ff @(posedge clk) dout <= din + 1'b1;
endmodule
