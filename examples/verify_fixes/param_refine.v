module top;
  localparam N = 4;
  localparam W_EARLY = 8;
  child #( .W(W_EARLY) ) u_arr[N-1:0] ();
  localparam W_LATE = 64;
endmodule

module child #(parameter int W = 1) (
  input logic [W-1:0] data
);
endmodule