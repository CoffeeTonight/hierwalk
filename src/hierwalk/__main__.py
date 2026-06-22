"""Allow: python -m hierwalk [args] (no hier-walk script on PATH required)."""

from hierwalk.cli import main

if __name__ == "__main__":
    raise SystemExit(main())