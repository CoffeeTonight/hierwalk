//! Lightweight structural RTL scanner — no full SV parse.
//!
//! Extracts: module names, ports, wire/reg declarations, continuous assign LHS.

use serde::Serialize;
use std::collections::HashSet;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct AssignLhs {
    pub lhs: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ModuleScan {
    pub name: String,
    pub kind: String,
    pub ports: Vec<String>,
    pub wires: Vec<String>,
    pub regs: Vec<String>,
    pub assigns: Vec<AssignLhs>,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct FileScan {
    pub modules: Vec<ModuleScan>,
}

const KEYWORDS: &[&str] = &[
    "module", "endmodule", "interface", "endinterface", "program", "endprogram", "input",
    "output", "inout", "wire", "wand", "wor", "reg", "logic", "assign", "always",
    "always_ff", "always_comb", "initial", "parameter", "localparam", "genvar", "generate",
    "endgenerate", "if", "else", "case", "endcase", "for", "while", "function",
    "endfunction", "task", "endtask", "typedef", "begin", "end", "bind",
];

fn is_keyword(s: &str) -> bool {
    KEYWORDS.iter().any(|k| k.eq_ignore_ascii_case(s))
}

fn is_ident_start(b: u8) -> bool {
    b.is_ascii_alphabetic() || b == b'_'
}

fn is_ident_continue(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_' || b == b'$'
}

fn push_unique(vec: &mut Vec<String>, set: &mut HashSet<String>, name: String) {
    if set.insert(name.clone()) {
        vec.push(name);
    }
}

fn first_ident_after_keywords(line: &str, keywords: &[&str]) -> Option<String> {
    let bytes = line.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        while i < bytes.len() && bytes[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= bytes.len() || !is_ident_start(bytes[i]) {
            break;
        }
        let start = i;
        i += 1;
        while i < bytes.len() && is_ident_continue(bytes[i]) {
            i += 1;
        }
        let word = &line[start..i];
        if keywords.iter().any(|k| k.eq_ignore_ascii_case(word)) {
            continue;
        }
        if !is_keyword(word) {
            return Some(word.to_string());
        }
    }
    None
}

fn collect_idents_in_segment(segment: &str, out: &mut Vec<String>, seen: &mut HashSet<String>) {
    let bytes = segment.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        if !is_ident_start(bytes[i]) {
            i += 1;
            continue;
        }
        let start = i;
        i += 1;
        while i < bytes.len() && is_ident_continue(bytes[i]) {
            i += 1;
        }
        let word = &segment[start..i];
        if !is_keyword(word) {
            push_unique(out, seen, word.to_string());
        }
    }
}

fn strip_comments_line(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut chars = line.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '/' && matches!(chars.peek(), Some('/')) {
            break;
        }
        out.push(c);
    }
    out
}

fn strip_bracket_dims(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let bytes = s.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] == b'[' {
            let mut depth = 1usize;
            i += 1;
            while i < bytes.len() && depth > 0 {
                if bytes[i] == b'[' {
                    depth += 1;
                }
                if bytes[i] == b']' {
                    depth -= 1;
                }
                i += 1;
            }
        } else {
            out.push(bytes[i] as char);
            i += 1;
        }
    }
    out
}

fn parse_port_header(header: &str, ports: &mut Vec<String>, seen: &mut HashSet<String>) {
    let mut s = header.to_string();
    if let Some(lp) = s.find('(') {
        s = s[lp + 1..].to_string();
    }
    if let Some(rp) = s.rfind(')') {
        s.truncate(rp);
    }
    for part in s.split(',') {
        let p = strip_bracket_dims(part.trim());
        if p.is_empty() {
            continue;
        }
        if let Some(name) = first_ident_after_keywords(
            &p,
            &["input", "output", "inout", "wire", "reg", "logic", "signed", "unsigned", "var"],
        ) {
            let base = name.split('[').next().unwrap_or(&name).to_string();
            if !base.is_empty() && !is_keyword(&base) {
                push_unique(ports, seen, base);
            }
        }
    }
}

fn parse_wire_reg_line(line: &str, wires: &mut Vec<String>, regs: &mut Vec<String>, seen_w: &mut HashSet<String>, seen_r: &mut HashSet<String>) {
    let trimmed = line.trim_start();
    let lower = trimmed.to_ascii_lowercase();
    let (is_wire, is_reg) = (
        lower.starts_with("wire") || lower.starts_with("wand") || lower.starts_with("wor"),
        lower.starts_with("reg") || lower.starts_with("logic"),
    );
    if !is_wire && !is_reg {
        return;
    }
    let keywords = if is_wire {
        &["wire", "wand", "wor"][..]
    } else {
        &["reg", "logic"][..]
    };
    let stripped = strip_bracket_dims(trimmed);
    if let Some(name) = first_ident_after_keywords(&stripped, keywords) {
        let base = name.split('[').next().unwrap_or(&name).to_string();
        if is_wire {
            push_unique(wires, seen_w, base);
        } else {
            push_unique(regs, seen_r, base);
        }
    } else {
        collect_idents_in_segment(
            trimmed,
            if is_wire { wires } else { regs },
            if is_wire { seen_w } else { seen_r },
        );
    }
}

fn parse_assign_line(line: &str, assigns: &mut Vec<AssignLhs>, seen: &mut HashSet<String>) {
    let trimmed = line.trim_start();
    if !trimmed.to_ascii_lowercase().starts_with("assign") {
        return;
    }
    let rest = trimmed[6..].trim_start();
    let Some(eq) = rest.find('=') else {
        return;
    };
    let lhs = rest[..eq].trim();
    let Some(base) = lhs.split('[').next() else {
        return;
    };
    let base = base.trim();
    if base.is_empty() || is_keyword(base) {
        return;
    }
    let key = base.to_string();
    if seen.insert(key.clone()) {
        assigns.push(AssignLhs { lhs: key });
    }
}

fn module_kind_end(kind: &str) -> &'static str {
    match kind.to_ascii_lowercase().as_str() {
        "interface" => "endinterface",
        "program" => "endprogram",
        _ => "endmodule",
    }
}

/// Scan preprocessed or raw Verilog text.
pub fn scan_text(text: &str) -> FileScan {
    let mut modules = Vec::new();
    let mut cur_name: Option<String> = None;
    let mut cur_kind = "module".to_string();
    let mut in_header = false;
    let mut header_buf = String::new();
    let mut ports: Vec<String> = Vec::new();
    let mut wires: Vec<String> = Vec::new();
    let mut regs: Vec<String> = Vec::new();
    let mut assigns: Vec<AssignLhs> = Vec::new();
    let mut seen_p = HashSet::new();
    let mut seen_w = HashSet::new();
    let mut seen_r = HashSet::new();
    let mut seen_a = HashSet::new();

    let flush = |name: String,
                 kind: String,
                 ports: &mut Vec<String>,
                 wires: &mut Vec<String>,
                 regs: &mut Vec<String>,
                 assigns: &mut Vec<AssignLhs>,
                 modules: &mut Vec<ModuleScan>| {
        modules.push(ModuleScan {
            name,
            kind,
            ports: std::mem::take(ports),
            wires: std::mem::take(wires),
            regs: std::mem::take(regs),
            assigns: std::mem::take(assigns),
        });
    };

    for raw_line in text.lines() {
        let line = strip_comments_line(raw_line);
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let lower = trimmed.to_ascii_lowercase();

        if let Some(rest) = lower.strip_prefix("module")
            .or_else(|| lower.strip_prefix("interface"))
            .or_else(|| lower.strip_prefix("program"))
        {
            if cur_name.is_some() {
                if let Some(n) = cur_name.take() {
                    flush(n, cur_kind.clone(), &mut ports, &mut wires, &mut regs, &mut assigns, &mut modules);
                    seen_p.clear();
                    seen_w.clear();
                    seen_r.clear();
                    seen_a.clear();
                }
            }
            cur_kind = if lower.starts_with("interface") {
                "interface".to_string()
            } else if lower.starts_with("program") {
                "program".to_string()
            } else {
                "module".to_string()
            };
            let after_kw = rest.trim_start();
            if let Some((name, _)) = after_kw.split_once(|c: char| {
                c.is_whitespace() || c == '(' || c == '#' || c == ';'
            }) {
                if !name.is_empty() && is_ident_start(name.as_bytes()[0]) {
                    cur_name = Some(name.to_string());
                    in_header = !trimmed.contains(';');
                    header_buf.clear();
                    if trimmed.contains('(') {
                        header_buf.push_str(trimmed);
                        if trimmed.contains(')') && trimmed.contains(';') {
                            parse_port_header(&header_buf, &mut ports, &mut seen_p);
                            in_header = false;
                            header_buf.clear();
                        }
                    }
                    continue;
                }
            }
        }

        if let Some(name) = cur_name.clone() {
            let end_kw = module_kind_end(&cur_kind);
            if lower.starts_with(end_kw) {
                flush(name, cur_kind.clone(), &mut ports, &mut wires, &mut regs, &mut assigns, &mut modules);
                cur_name = None;
                in_header = false;
                header_buf.clear();
                seen_p.clear();
                seen_w.clear();
                seen_r.clear();
                seen_a.clear();
                continue;
            }

            if in_header {
                header_buf.push(' ');
                header_buf.push_str(trimmed);
                if trimmed.contains(')') {
                    parse_port_header(&header_buf, &mut ports, &mut seen_p);
                    in_header = false;
                    header_buf.clear();
                }
                continue;
            }

            parse_wire_reg_line(trimmed, &mut wires, &mut regs, &mut seen_w, &mut seen_r);
            parse_assign_line(trimmed, &mut assigns, &mut seen_a);
        }
    }

    if let Some(name) = cur_name.take() {
        flush(name, cur_kind, &mut ports, &mut wires, &mut regs, &mut assigns, &mut modules);
    }

    FileScan { modules }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_child_parent() {
        let src = r#"
module child (
  input wire clk,
  output reg [7:0] q
);
  wire internal;
  assign q = internal;
endmodule

module parent;
  wire x;
endmodule
"#;
        let scan = scan_text(src);
        assert_eq!(scan.modules.len(), 2);
        assert_eq!(scan.modules[0].name, "child");
        assert!(scan.modules[0].ports.contains(&"clk".to_string()));
        assert!(scan.modules[0].ports.contains(&"q".to_string()));
        assert!(scan.modules[0].wires.contains(&"internal".to_string()));
        assert_eq!(scan.modules[0].assigns[0].lhs, "q");
        assert_eq!(scan.modules[1].name, "parent");
    }
}