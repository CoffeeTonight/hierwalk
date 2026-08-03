// Top: array of lanes + generate-for + interface + cross-hierarchy fan
module hard_top #(
  parameter int W = 16,
  parameter int N_LANES = 4
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [W-1:0]     a_i,
  input  logic [W-1:0]     b_i,
  input  logic [1:0]       op_i,
  input  logic [1:0]       lane_sel_i,
  output logic [W-1:0]     result_o,
  output logic [W-1:0]     slice_result_o,
  output logic [W-1:0]     stream_data_o,
  output logic             stream_valid_o
);
  import hard_pkg::*;

  logic [N_LANES-1:0][W-1:0] lane_y;
  logic [N_LANES-1:0][W-1:0] lane_ys;

  // --- array of instances (same type, different params via generate) ---
  genvar gi;
  generate
    for (gi = 0; gi < N_LANES; gi++) begin : g_lane
      lane #(
        .W(W),
        .USE_INV(gi[0])  // alternate invert
      ) u_lane (
        .clk(clk),
        .rst_n(rst_n),
        .a_i(a_i),
        .b_i(b_i),
        .op_i(op_i),
        .y_o(lane_y[gi]),
        .y_slice_o(lane_ys[gi])
      );
    end
  endgenerate

  // --- bit-sliced xbar over lane outputs ---
  xbar_slice #(.W(W), .N(N_LANES)) u_xbar (
    .in_i(lane_y),
    .sel_i(lane_sel_i),
    .out_o(result_o)
  );

  // slice path: pick lane0 slice only then OR-reduce style fan
  assign slice_result_o = lane_ys[0];

  // --- interface stream path ---
  hard_stream_if #(.W(W)) s0();
  hard_stream_if #(.W(W)) s1();

  assign s0.valid = 1'b1;
  assign s0.data  = result_o;
  // s0.ready driven by pipe

  stream_pipe #(.W(W)) u_pipe (
    .clk(clk),
    .rst_n(rst_n),
    .in_if(s0),
    .out_if(s1)
  );

  assign stream_data_o  = s1.data;
  assign stream_valid_o = s1.valid;
  assign s1.ready       = 1'b1;

  // package-typed local (not on path, but ensures pkg elab)
  pair_t pack_w;
  assign pack_w.lo = a_i[7:0];
  assign pack_w.hi = b_i[7:0];
endmodule
