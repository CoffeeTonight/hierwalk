// One registered stage: d -> q (always_ff). Used as a clear FF boundary.
module coi_stage #(
  parameter int W = 8
) (
  input  logic         clk,
  input  logic         rst_n,
  input  logic         en_i,
  input  logic [W-1:0] d_i,
  output logic [W-1:0] q_o
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) q_o <= '0;
    else if (en_i) q_o <= d_i;
  end
endmodule
