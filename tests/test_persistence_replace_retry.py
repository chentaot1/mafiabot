"""Brief Windows replacement locks preserve atomic saves and write order."""
import asyncio
import ctypes
import json
import os
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

import persistence


PENDING = {'game_key': 'saved-match', 'outcome': 'Town'}


def prepare(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(persistence, 'STATE_DIR', tmp_path)
    if kind in {'state', 'embed', 'clear'}:
        path = tmp_path / '123.json'
        persistence.save_state(123, {'in_progress': True, 'version': 1})
        if kind == 'clear':
            persistence.embed_pending_endgame_in_game_state(123, PENDING)
        if kind == 'state':
            write = lambda: persistence.save_state(123, {'in_progress': True, 'version': 2})
        elif kind == 'embed':
            write = lambda: persistence.embed_pending_endgame_in_game_state(123, PENDING)
        else:
            write = lambda: persistence.clear_inline_pending_endgame_from_game_state(123)
    elif kind == 'stats':
        path = tmp_path / '123.stats.json'
        persistence.save_stats(123, {'players': {'1': {'wins': 3}}})
        write = lambda: persistence.save_stats(123, {'players': {'1': {'wins': 4}}})
    else:
        path = tmp_path / '123.pending_endgame.json'
        persistence.save_pending_endgame_fallback(123, {'game_key': 'earlier', 'outcome': 'Town'})
        write = lambda: persistence.save_pending_endgame_fallback(123, PENDING)
    return path, write


def windows_error(code):
    error = PermissionError(13, 'Windows replacement unavailable')
    error.winerror = code
    return error


@pytest.mark.parametrize('kind', ['state', 'stats', 'fallback', 'embed', 'clear'])
@pytest.mark.parametrize('code', [5, 32, 33])
def test_brief_replacement_failure_retries_same_prepared_bytes(tmp_path, monkeypatch, kind, code):
    path, write = prepare(tmp_path, monkeypatch, kind)
    original, replace = path.read_bytes(), Path.replace
    attempts = []
    delays = Mock()
    monkeypatch.setattr(persistence.time, 'sleep', delays)
    def blocked_once(self, target):
        if Path(target) == path:
            attempts.append((self, self.read_bytes()))
            if len(attempts) == 1:
                assert path.read_bytes() == original
                raise windows_error(code)
        return replace(self, target)
    monkeypatch.setattr(Path, 'replace', blocked_once)
    write()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert path.read_bytes() == attempts[0][1]
    assert not list(tmp_path.glob('*.tmp.*'))
    delays.assert_called_once()
    if kind == 'state':
        assert len(list(tmp_path.glob('123.json.bak.*'))) == 1


@pytest.mark.parametrize('code,retries', [(5, True), (32, True), (33, True), (112, False), (2, False), (None, False)])
def test_persistent_replacement_failure_is_bounded_and_keeps_old_save(tmp_path, monkeypatch, code, retries):
    path, write = prepare(tmp_path, monkeypatch, 'state')
    original = path.read_bytes()
    failed = windows_error(code)
    attempts, delays = Mock(side_effect=failed), Mock()
    monkeypatch.setattr(Path, 'replace', attempts)
    monkeypatch.setattr(persistence.time, 'sleep', delays)
    with pytest.raises(PermissionError) as raised:
        write()
    assert raised.value is failed
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('*.tmp.*'))
    if retries:
        assert 1 < attempts.call_count <= 6
        assert delays.call_count == attempts.call_count - 1
        assert sum(call.args[0] for call in delays.call_args_list) < 1
    else:
        attempts.assert_called_once()
        delays.assert_not_called()


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows replacement lock')
@pytest.mark.asyncio
async def test_cancelled_save_drains_real_replacement_lock_before_next_writer(tmp_path, monkeypatch):
    from ctypes import wintypes
    path, _ = prepare(tmp_path, monkeypatch, 'state')
    original = path.read_bytes()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    entered, release = threading.Event(), threading.Event()
    observed, completed = [], []
    replace = Path.replace
    def replacing(self, target):
        try:
            result = replace(self, target)
        except OSError as error:
            observed.append(error.winerror)
            entered.set()
            raise
        completed.append(json.loads(Path(target).read_text())['version'])
        return result
    def pause(seconds):
        assert release.wait(3), 'Replacement retry was not released'
    monkeypatch.setattr(Path, 'replace', replacing)
    monkeypatch.setattr(persistence.time, 'sleep', pause)
    first = asyncio.create_task(persistence.save_state_async(123, {'in_progress': True, 'version': 2}))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert path.read_bytes() == original
        first.cancel()
        second = asyncio.create_task(persistence.save_state_async(123, {'in_progress': True, 'version': 3}))
        await asyncio.sleep(0)
        first.cancel()
        assert not first.done() and not second.done()
        assert kernel.CloseHandle(handle)
        handle = None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 3)
        await asyncio.wait_for(second, 3)
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    assert observed and set(observed) <= {5, 32, 33}
    assert completed == [2, 3]
    assert persistence.load_state(123)['version'] == 3
    assert sorted(json.loads(p.read_text())['version'] for p in tmp_path.glob('123.json.bak.*')) == [1, 2]
    assert not list(tmp_path.glob('*.tmp.*'))
