import os
import subprocess
import sys
from pathlib import Path

from instance_lock import acquire_instance_lock


def test_duplicate_process_cannot_evict_lock_owner(tmp_path):
    path = tmp_path / "bot.instance.lock"
    owner = acquire_instance_lock(path)
    try:
        owner.write(str(os.getpid()).encode())
        code = """
import sys
from pathlib import Path
from instance_lock import acquire_instance_lock
try:
    handle = acquire_instance_lock(Path(sys.argv[1]))
except RuntimeError:
    print('blocked')
else:
    handle.close()
    raise SystemExit('duplicate instance acquired the lock')
"""
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "blocked"
        assert not owner.closed
    finally:
        owner.close()
    # The OS releases ownership; stale metadata needs no PID probe or unlink.
    with acquire_instance_lock(path):
        pass


def test_stale_lock_metadata_is_reusable(tmp_path):
    path = tmp_path / "bot.instance.lock"
    path.write_text('{"pid": 123456789}')
    with acquire_instance_lock(path) as handle:
        handle.write(b'{"pid": 1}')
        handle.truncate()
    assert path.read_text() == '{"pid": 1}'
