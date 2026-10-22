from bess_stack.cli import main

if __name__ == "__main__":     # guard: worker processes (spawn) import this module again
    raise SystemExit(main())
