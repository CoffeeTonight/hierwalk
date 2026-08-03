package zz_pkg;
  parameter int W     = 16;
  parameter int N     = 4;   // generate-for lanes
  parameter int STAGES = 3;  // pipeline depth (always_ff)
  parameter int B_ARMS = 8;  // zz_branch live fan-out arms
  parameter int D_ARMS = 4;  // zz_branch dead / noise arms
  parameter bit USE_INV = 1'b1;
  parameter bit USE_B   = 1'b1; // ifdef-style param branch companion

  typedef enum logic [1:0] {
    OP_PASS = 2'b00,
    OP_XOR  = 2'b01,
    OP_ADD  = 2'b10,
    OP_SEL  = 2'b11
  } zz_op_e;
endpackage
