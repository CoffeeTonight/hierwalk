module top(input logic clk, input logic src, input logic ref);
  wire a, b, d0, d1;
  wire [1:0] bus_b;
  wire tie0, tie1, tie2, tie3;
  wire xA, xB, yA, yB;

  assign a = clk;
  assign b = clk;
  assign bus_b[0] = a;
  assign bus_b[1] = b;
  assign d0 = src;
  assign d1 = src;
  assign tie0 = ref;
  assign tie1 = ref;
  assign tie2 = ref;
  assign tie3 = ref;
  assign xA = ref;
  assign xB = ref;
  assign yA = ref;
  assign yB = ref;
endmodule