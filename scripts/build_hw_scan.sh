#!/usr/bin/env bash
# Build Rust hw-scan binary (release). Requires rustup/cargo on PATH.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/rust/hw-scan"
cargo build --release
echo "built: $ROOT/rust/hw-scan/target/release/hw-scan"