"""Run every pytest case with default pool sizing limited to two logical CPUs."""
from pathlib import Path
import os
import sys

def main():
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    # Tests that override cpu_count still exercise their own sizing contracts.
    # Production code and explicit worker-count tests are unaffected.
    os.cpu_count = lambda: 2
    import pytest
    return pytest.main(['-q', '--tb=short', '--maxfail=4', '-o', 'faulthandler_timeout=45', *sys.argv[1:]])

if __name__ == '__main__':
    raise SystemExit(main())
