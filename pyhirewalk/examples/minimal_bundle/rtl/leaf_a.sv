// Leaf used when FEATURE_A is selected (ifdef / generate if).
module leaf_a #(
  parameter int W = 8
) (
  input  logic         clk,
  input  logic [W-1:0] din,
  output logic [W-1:0] dout
);
  always_ff @(posedge clk) dout <= din;
endmodule
