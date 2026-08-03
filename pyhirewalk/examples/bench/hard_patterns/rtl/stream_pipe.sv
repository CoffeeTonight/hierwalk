// Interface-port module
// in_if  = snk (consume upstream), out_if = src (produce downstream)
module stream_pipe #(
  parameter int W = 16
) (
  input logic clk,
  input logic rst_n,
  hard_stream_if.snk  in_if,
  hard_stream_if.src  out_if
);
  logic [W-1:0] data_q;
  logic         valid_q;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      data_q  <= '0;
      valid_q <= 1'b0;
    end else if (in_if.valid && in_if.ready) begin
      data_q  <= in_if.data;
      valid_q <= 1'b1;
    end else if (out_if.ready) begin
      valid_q <= 1'b0;
    end
  end

  assign in_if.ready  = !valid_q || out_if.ready;
  assign out_if.valid = valid_q;
  assign out_if.data  = data_q;
endmodule
