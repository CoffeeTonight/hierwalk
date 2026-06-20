module leaf(input in, output out);
  assign out = in;
endmodule

module bus_mux(
  input wire_a,
  input wire_b,
  input wire_c,
  output [2:0] bus_out
);
  assign bus_out[0] = wire_a;
  assign bus_out[1] = wire_b;
  assign bus_out[2] = wire_c;
endmodule

module top(
  input wire_a,
  input wire_b,
  input wire_c,
  output [2:0] y
);
  bus_mux u_mux (
    .wire_a(wire_a),
    .wire_b(wire_b),
    .wire_c(wire_c),
    .bus_out(y)
  );
endmodule