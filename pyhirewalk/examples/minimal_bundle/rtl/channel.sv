// One channel: optional pipe leaf chosen by generate if + ifdef.
module channel #(
  parameter int W = 8,
  parameter bit USE_PIPE = 1'b1
) (
  input  logic         clk,
  input  logic [W-1:0] din,
  output logic [W-1:0] dout
);
`ifdef FEATURE_A
  if (USE_PIPE) begin : g_pipe
    leaf_a #(.W(W)) u_leaf (.clk(clk), .din(din), .dout(dout));
  end else begin : g_bypass
    assign dout = din;
  end
`else
  leaf_b #(.W(W)) u_leaf (.clk(clk), .din(din), .dout(dout));
`endif
endmodule
