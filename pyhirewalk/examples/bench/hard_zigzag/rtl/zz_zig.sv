// Zigzag shuffle: comb fold + ifdef alternate route + FF
module zz_zig #(
  parameter int W = 16
) (
  input  logic         clk,
  input  logic         rst_n,
  input  logic [W-1:0] east_i,   // from xbar
  input  logic [W-1:0] north_i,  // from pipe mid
  input  logic [1:0]   mode_i,
  output logic [W-1:0] west_o,   // to top pad
  output logic [W-1:0] south_q_o // registered zigzag out
);
  logic [W-1:0] mix_w, fold_w, alt_w;

  // comb zigzag mix
  always_comb begin
    unique case (mode_i)
      2'b00: mix_w = east_i;
      2'b01: mix_w = north_i;
      2'b10: mix_w = east_i ^ north_i;
      default: mix_w = {east_i[W/2-1:0], north_i[W-1:W/2]};
    endcase
  end

  // ifdef-twisted route
`ifdef ZZ_ALT_PATH
  assign alt_w = ~mix_w;
`else
  assign alt_w = mix_w ^ {W{mix_w[0]}};
`endif

  // parameter-free fold (still comb)
  assign fold_w = {alt_w[0], alt_w[W-1:1]}; // rotate

  assign west_o = fold_w;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) south_q_o <= '0;
    else south_q_o <= fold_w;
  end
endmodule
