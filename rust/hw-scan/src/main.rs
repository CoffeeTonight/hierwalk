use hw_scan::scan_text;
use std::env;
use std::fs;
use std::io::{self, Read};
use std::process;

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: hw-scan <file.v> | hw-scan -   (JSON on stdout)");
        process::exit(2);
    }
    let path = &args[1];
    let text = if path == "-" {
        let mut buf = String::new();
        io::stdin().read_to_string(&mut buf).unwrap_or_else(|e| {
            eprintln!("stdin read failed: {e}");
            process::exit(1);
        });
        buf
    } else {
        fs::read_to_string(path).unwrap_or_else(|e| {
            eprintln!("read {path}: {e}");
            process::exit(1);
        })
    };
    let scan = scan_text(&text);
    println!("{}", serde_json::to_string(&scan).expect("json"));
}