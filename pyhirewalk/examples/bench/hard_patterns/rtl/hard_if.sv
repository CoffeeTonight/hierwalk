interface hard_stream_if #(parameter int W = 16);
  logic             valid;
  logic             ready;
  logic [W-1:0]     data;
  modport src (output valid, data, input ready);
  modport snk (input  valid, data, output ready);
endinterface
