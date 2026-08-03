// Bit-sliced crossbar-ish: generate-for over bits
module xbar_slice #(
  parameter int W = 16,
  parameter int N = 4
) (
  input  logic [N-1:0][W-1:0] in_i,
  input  logic [1:0]          sel_i,
  output logic [W-1:0]        out_o
);
  logic [W-1:0] picked;
  always_comb begin
    unique case (sel_i)
      2'd0: picked = in_i[0];
      2'd1: picked = in_i[1];
      2'd2: picked = in_i[2];
      default: picked = in_i[3];
    endcase
  end

  genvar gi;
  generate
    for (gi = 0; gi < W; gi++) begin : g_bit
      // per-bit buffer (forces generate block names in hierarchy)
      wire bit_w;
      assign bit_w = picked[gi];
      assign out_o[gi] = bit_w;
    end
  endgenerate
endmodule
