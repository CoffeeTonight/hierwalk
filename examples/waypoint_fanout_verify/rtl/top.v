// PyCharm / scripts/debug_waypoint_fanout.py 용 미니 SoC
// top.drv → assign mid → u_c (waypoint) → always_ff → qout
//              └──────── u_leaf (waypoint 없음 → unqualified terminator)

module top(input logic clk, input logic drv, output logic z, output logic direct);
  wire mid;
  assign mid = drv;  // line 8: assign edge (rtl_line)
  child u_c (.clk(clk), .din(mid), .qout(z));
  leaf u_leaf (.p(drv), .q(direct));
endmodule

module child(input logic clk, input logic din, output logic qout);
  logic r;
  always_ff @(posedge clk) r <= din;  // line 17: FF (rtl_line)
  assign qout = r;
endmodule

module leaf(input logic p, output logic q);
  assign q = p;
endmodule