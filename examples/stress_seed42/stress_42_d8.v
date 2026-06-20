module stress_decoy #(
  parameter int DECOY_ID = 0,
  parameter int DECOY_BASE = 1
)(
  input  logic clk,
  input  logic rst_n,
  output logic noise
);
  localparam int DECOY_SPAN = DECOY_BASE + DECOY_ID;
  logic r;
  logic [3:0] cnt;
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      r <= 1'b0;
      cnt <= DECOY_SPAN[3:0];
    end else begin
      r <= ~r;
      cnt <= cnt + 1'b1;
    end
  end
  assign noise = r ^ ^cnt;
endmodule

module stress_leaf #(
  parameter int BASE = 2,
  parameter int STRIDE = BASE + 2,
  parameter int LEVEL = 0,
  parameter int PASS_THRU = 1
)(
  input  logic clk,
  input  logic rst_n,
  input  logic probe_in,
  output logic probe_out
);
  logic [1:0][1:0] leaf_arr;
  logic leaf_q;
  localparam int LEAF_LP = BASE * STRIDE + LEVEL;
  localparam int LEAF_IDX = LEAF_LP[1:0];
  `ifdef STRESS_USE_IN
    assign leaf_arr[0][0] = probe_in;
    assign leaf_arr[1][LEAF_IDX] = leaf_arr[0][0];
    always_ff @(posedge clk) begin
      if (!rst_n)
        leaf_q <= 1'b0;
      else
        leaf_q <= leaf_arr[1][LEAF_IDX];
    end
    assign probe_out = {leaf_q};
  `else
    assign probe_out = 1'b0;
  `endif
endmodule

module stress_spine_0 #(
          parameter int BASE = 2;
  parameter int STRIDE = BASE + 2;
  parameter int LEVEL = 0;
  localparam int SPAN = BASE + LEVEL;
  localparam int WIN = (BASE + LEVEL) - BASE + 1;
  localparam int PASS_THRU = ((BASE + LEVEL) - BASE + 1 > 0) ? 1 : 0;
  localparam int LP_OFF = BASE * STRIDE + LEVEL;
        )(
        input  logic clk,
input  logic rst_n,
input  logic en_0,
input  logic probe_in,
output logic probe_out
        );
          wire link;
assign link = probe_in;
logic noise_0; assign noise_0 = probe_in & 1'b0;
        stress_spine_1 u_spine (
        .clk(clk),
        .rst_n(rst_n),
        .en_1(en_0),
.probe_in(link),
        .probe_out(probe_out)
      );
          stress_decoy u_d0_0 (.clk(clk), .rst_n(rst_n), .noise()),
             u_d0_1 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d0_2 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d0_3 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy #(.DECOY_ID(0)) u_d0_p (.clk(clk), .rst_n(rst_n), .noise());
        endmodule

module stress_spine_1 #(
          parameter int BASE = 2;
  parameter int STRIDE = BASE + 2;
  parameter int LEVEL = 1;
  localparam int SPAN = BASE + LEVEL;
  localparam int WIN = (BASE + LEVEL) - BASE + 1;
  localparam int PASS_THRU = ((BASE + LEVEL) - BASE + 1 > 0) ? 1 : 0;
  localparam int LP_OFF = BASE * STRIDE + LEVEL;
        )(
        input  logic clk,
input  logic rst_n,
input  logic en_1,
input  logic probe_in,
output logic probe_out
        );
          logic link;
always_ff @(posedge clk) begin
  if (!rst_n)
    link <= 1'b0;
  else
    link <= probe_in;
end
logic noise_1; assign noise_1 = probe_in & 1'b0;
        stress_spine_2 u_spine (
        .clk(clk),
        .rst_n(rst_n),
        .en_2(en_1),
.probe_in(link),
        .probe_out(probe_out)
      );
          stress_decoy u_d1_0 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d1_2 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d1_3 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d1_4 (.clk(clk), .rst_n(rst_n), .noise());
        endmodule

module stress_spine_2 #(
          parameter int BASE = 2;
  parameter int STRIDE = BASE + 2;
  parameter int LEVEL = 2;
  localparam int SPAN = BASE + LEVEL;
  localparam int WIN = (BASE + LEVEL) - BASE + 1;
  localparam int PASS_THRU = ((BASE + LEVEL) - BASE + 1 > 0) ? 1 : 0;
  localparam int LP_OFF = BASE * STRIDE + LEVEL;
        )(
        input  logic clk,
input  logic rst_n,
input  logic en_2,
input  logic probe_in,
output logic probe_out
        );
          wire link = probe_in;
logic noise_2; assign noise_2 = probe_in & 1'b0;
        stress_spine_3 u_spine (
        .clk(clk),
        .rst_n(rst_n),
        .en_3(en_2),
.probe_in(link),
        .probe_out(probe_out)
      );
          stress_decoy u_d2_0 (.clk(clk), .rst_n(rst_n), .noise()),
             u_d2_1 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d2_2 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d2_3 (.clk(clk), .rst_n(rst_n), .noise());
        endmodule

module stress_spine_3 #(
          parameter int BASE = 2;
  parameter int STRIDE = BASE + 2;
  parameter int LEVEL = 3;
  localparam int SPAN = BASE + LEVEL;
  localparam int WIN = (BASE + LEVEL) - BASE + 1;
  localparam int PASS_THRU = ((BASE + LEVEL) - BASE + 1 > 0) ? 1 : 0;
  localparam int LP_OFF = BASE * STRIDE + LEVEL;
        )(
        input  logic clk,
input  logic rst_n,
input  logic en_3,
input  logic probe_in,
output logic probe_out
        );
          wire link;
generate
  for (genvar gi = 0; gi < 1; gi++) begin : gen_pass_3
    assign link = probe_in;
  end
endgenerate
        stress_spine_4 u_spine (
        .clk(clk),
        .rst_n(rst_n),
        .en_4(en_3),
.probe_in(link),
        .probe_out(probe_out)
      );
          stress_decoy u_d3_0 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d3_2 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d3_3 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d3_4 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy #(.DECOY_ID(3)) u_d3_p (.clk(clk), .rst_n(rst_n), .noise());
        endmodule

module stress_spine_4 #(
          parameter int BASE = 2;
  parameter int STRIDE = BASE + 2;
  parameter int LEVEL = 4;
  localparam int SPAN = BASE + LEVEL;
  localparam int WIN = (BASE + LEVEL) - BASE + 1;
  localparam int PASS_THRU = ((BASE + LEVEL) - BASE + 1 > 0) ? 1 : 0;
  localparam int LP_OFF = BASE * STRIDE + LEVEL;
        )(
        input  logic clk,
input  logic rst_n,
input  logic en_4,
input  logic probe_in,
output logic probe_out
        );
          logic link;
logic [1:0] sel_4;
assign sel_4 = 2'b00;
always_ff @(posedge clk) begin
  case (sel_4)
    2'b00: link <= probe_in;
    2'b01: link <= probe_in;
    2'b10: link <= probe_in;
    default: link <= probe_in;
  endcase
end
        stress_spine_5 u_spine (
        .clk(clk),
        .rst_n(rst_n),
        .en_5(en_4),
.probe_in(link),
        .probe_out(probe_out)
      );
          stress_decoy u_d4_0 (.clk(clk), .rst_n(rst_n), .noise()),
             u_d4_1 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d4_2 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d4_3 (.clk(clk), .rst_n(rst_n), .noise());
        endmodule

module stress_spine_5 #(
          parameter int BASE = 2;
  parameter int STRIDE = BASE + 2;
  parameter int LEVEL = 5;
  localparam int SPAN = BASE + LEVEL;
  localparam int WIN = (BASE + LEVEL) - BASE + 1;
  localparam int PASS_THRU = ((BASE + LEVEL) - BASE + 1 > 0) ? 1 : 0;
  localparam int LP_OFF = BASE * STRIDE + LEVEL;
        )(
        input  logic clk,
input  logic rst_n,
input  logic en_5,
input  logic probe_in,
output logic probe_out
        );
          logic link;
always_comb begin
  if (en_5)
    link = probe_in;
  else
    link = 1'b0;
end
        stress_spine_6 u_spine (
        .clk(clk),
        .rst_n(rst_n),
        .en_6(en_5),
.probe_in(link),
        .probe_out(probe_out)
      );
          stress_decoy u_d5_0 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d5_2 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d5_3 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d5_4 (.clk(clk), .rst_n(rst_n), .noise());
        endmodule

module stress_spine_6 #(
          parameter int BASE = 2;
  parameter int STRIDE = BASE + 2;
  parameter int LEVEL = 6;
  localparam int SPAN = BASE + LEVEL;
  localparam int WIN = (BASE + LEVEL) - BASE + 1;
  localparam int PASS_THRU = ((BASE + LEVEL) - BASE + 1 > 0) ? 1 : 0;
  localparam int LP_OFF = BASE * STRIDE + LEVEL;
        )(
        input  logic clk,
input  logic rst_n,
input  logic en_6,
input  logic probe_in,
output logic probe_out
        );
          wire link;
`ifdef STRESS_USE_IN
  assign link = probe_in;
`else
  assign link = 1'b0;
`endif
        stress_leaf u_spine (
  .clk(clk),
  .rst_n(rst_n),
  .probe_in(link),
  .probe_out(probe_out)
);
          stress_decoy u_d6_0 (.clk(clk), .rst_n(rst_n), .noise()),
             u_d6_1 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d6_2 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d6_3 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy #(.DECOY_ID(6)) u_d6_p (.clk(clk), .rst_n(rst_n), .noise());
        endmodule

module stress_top #(
          parameter int BASE = 3,
          parameter int STRIDE = BASE + 2
        )(
          input  logic clk,
          input  logic rst_n,
          input  logic probe_in,
          output logic probe_out
        );
          stress_spine_0 u_spine (
  .clk(clk),
  .rst_n(rst_n),
  .en_0(1'b1),
  .probe_in(probe_in),
  .probe_out(probe_out)
);
          stress_decoy u_d99_0 (.clk(clk), .rst_n(rst_n), .noise()),
             u_d99_1 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d99_2 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy u_d99_3 (.clk(clk), .rst_n(rst_n), .noise());
  stress_decoy #(.DECOY_ID(99)) u_d99_p (.clk(clk), .rst_n(rst_n), .noise());
        endmodule
