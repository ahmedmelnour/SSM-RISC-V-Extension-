`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 08/10/2026 02:27:21 PM
// Design Name: 
// Module Name: blinky_top
// Project Name: 
// Target Devices: 
// Tool Versions: 
// Description: 
// 
// Dependencies: 
// 
// Revision:
// Revision 0.01 - File Created
// Additional Comments:
// 
//////////////////////////////////////////////////////////////////////////////////


module blinky_top(
    input clk_i,
    output led1_o,
    output led2_o
    );
    
    logic [25:0] heartbeat_cnt = '0;
    
    always_ff @(posedge clk_i) begin
        heartbeat_cnt <= heartbeat_cnt +26'd1;
    end
    
    assign led1_o = heartbeat_cnt[25];
    
    logic [15:0] por_cnt = '0;
    logic        rst_n;
    
    always_ff @(posedge clk_i) begin
        if (por_cnt != 16'hFFFF) begin 
            por_cnt <= por_cnt + 16'd1;
        end
        else 
            por_cnt <= por_cnt;
     end
     
     assign rst_n = (por_cnt == 16'hFFFF);
     
     assign led2_o = ~rst_n;
     

endmodule
