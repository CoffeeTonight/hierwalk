# minimal_bundle — essential index example

Tiny design that mixes patterns pyslang must fold before we store anything:

| Pattern | Where |
|---------|--------|
| filelist `+define+` | `filelist.f` → `FEATURE_A=1` |
| `for` generate | `top.sv` → `g_ch[i]` |
| `for` + param `if` | `USE_PIPE = (i==0) ? 0 : 1` |
| `` `ifdef`` choose implementation | `channel.sv` → `leaf_a` vs `leaf_b` |
| multi-dim-ish bundle ports | `s_data[N-1:0][W-1:0]`, `d_data[...]` |

## Regenerate index (needs pyslang)

```bash
cd ~/Desktop/pyhirewalk
PYTHONPATH=src python3 examples/minimal_bundle/build_essential_index.py
```

Output: `essential_index.example.json`

## What is *not* in this DB

- Netlist / assign / always edges (L3, later)
- Per-bit or per-slice graph nodes
- Dead ifdef/generate arms as instances
- Full AST

## Bundle query this example is aiming at (later zigzag)

```text
G_s = top.s_data[*]   (or each s_data[i])
G_t = top.d_data[*]
→ expect per-lane relations; lane 0 combo bypass, others through leaf_a FF
```
