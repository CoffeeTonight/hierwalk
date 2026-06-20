// Extreme newline-split tokens: cell, inst, #(params), port map — each on its own line.
module SOC_TOP;

`ifndef ASD
ASD
 u_asd
(
.a
(
w
)
,
.b
(
w2
)
)
;
`endif

BCD
#
(
.a
(
2
)
,
.b
(
2-1
)
)
u_BCD
(
.clk
(
clk
)
)
;

genvar gi;
generate
for
(
gi
=
0
;
gi
<
2
;
gi
++
)
begin
:
gen_blk
BCD
#
(
.a
(
gi
)
,
.b
(
2-1
)
)
u_BCD_gen
(
.clk
(
clk
)
)
;
end
endgenerate

endmodule

module ASD; endmodule
module BCD; endmodule