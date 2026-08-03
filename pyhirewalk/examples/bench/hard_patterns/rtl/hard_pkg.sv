package hard_pkg;
  typedef struct packed {
    logic [7:0] lo;
    logic [7:0] hi;
  } pair_t;
  typedef enum logic [1:0] { OP_A=2'b00, OP_B=2'b01, OP_C=2'b10, OP_D=2'b11 } op_e;
endpackage
