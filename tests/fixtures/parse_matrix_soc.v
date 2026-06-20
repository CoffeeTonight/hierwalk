// Combination-matrix RTL: ifdef/ifndef/elsif + comments + generate + #(params)
`define CELL LEAF

module SOC_TOP;
  parameter N_ARR = 1;
  localparam PASS_THRU = 1;

  // --- axis: line-comment directive trap + nested ifndef/elsif/else ---
  // `endif must not close early
  `ifndef ASD
  A u_A
  (
  .aa (w_aa));
  `elsif USE_B
  B u_B (.bb(w_bb));
  `else
  STUB u_stub ();
  `endif//ASD

  // --- axis: block-comment fake ifdef (must not affect stack) ---
  /*
  `ifdef FAKE_BLK
  FAKE u_fake_blk ();
  `endif
  */

  // --- axis: endif//LABEL same-line next instance ---
  `ifndef NO_CPU
  WRAP u_wrap (.x(w));
  `endif//NO_CPU CPUSYSTEM_TOP u_cpusystem_top (.clk(clk));

  // --- axis: flat param override #(nested expr) ---
  BCD #(.a(2),.b(2-1)) u_BCD (.clk(clk));

  // --- axis: comma-separated + param on first only ---
  TINY #(.w(8)) u_t0 (.d(w)), u_t1 (.d(w));

  // --- axis: macro cell under ifndef ---
  `ifndef NO_MACRO
  `CELL u_macro ();
  `endif

  genvar gi;
  generate
    // --- axis: for-loop + ifdef/elsif/else + param in generate ---
    for (gi = 0; gi < 2; gi++) begin : gen_blk
`ifdef GEN_LEAF
      LEAF #(.idx(gi)) u_leaf (.clk(clk)); // `endif trap
`elsif GEN_ALT
      ALT u_alt ();
`else
      BCD #(.a(gi),.b(2-1)) u_BCD_gen (.clk(clk));
`endif
    end

    // --- axis: if-generate + param instance ---
    if (PASS_THRU) begin : ifg_blk
      IFG_CHILD #(.k(3)) u_ifg (.clk(clk));
    end

    // --- axis: nested generate (for inside for) ---
    for (gi = 0; gi < 2; gi++) begin : outer
      for (genvar gj = 0; gj < 2; gj++) begin : inner
        NEST #(.oi(gi),.ij(gj)) u_nest (.clk(clk));
      end
    end

    // --- axis: nested ifndef inside port map ---
    if (1) begin : port_ifndef_blk
`ifndef ABC
      DEF u_DEF (
`ifndef PORT_X
        .QW (w_QW),
`endif
        .CLK (clk)
      );
`endif
    end

    // --- axis: array instance with param ---
    if (1) begin : arr_blk
      ARR #(.n(N_ARR)) u_arr [0:N_ARR] (.clk(clk));
    end
  endgenerate
endmodule

module A; endmodule
module B; endmodule
module STUB; endmodule
module WRAP; endmodule
module CPUSYSTEM_TOP; endmodule
module BCD; endmodule
module TINY; endmodule
module LEAF; endmodule
module ALT; endmodule
module IFG_CHILD; endmodule
module NEST; endmodule
module DEF(input CLK, output QW); endmodule
module ARR; endmodule

bind SOC_TOP ghost u_ghost ();  // must be ignored
module ghost; endmodule