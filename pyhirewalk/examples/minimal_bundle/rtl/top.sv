// Bank of channels via for-generate; demonstrates multi-dim-ish bus slices
// as separate channel instances (N lanes of W bits).
module top #(
  parameter int N = 4,
  parameter int W = 8
) (
  input  logic             clk,
  input  logic [N-1:0][W-1:0] s_data,  // bundle-like source group
  output logic [N-1:0][W-1:0] d_data   // bundle-like sink group
);
  for (genvar i = 0; i < N; i++) begin : g_ch
    // Special-case lane 0: no pipe (generate if nested in for)
    channel #(
      .W(W),
      .USE_PIPE(i == 0 ? 1'b0 : 1'b1)
    ) u_ch (
      .clk (clk),
      .din (s_data[i]),
      .dout(d_data[i])
    );
  end
endmodule
