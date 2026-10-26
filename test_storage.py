from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import storage


def test_relative_database_is_outside_checkout(tmp_path,monkeypatch):
    state=tmp_path/'persistent'
    monkeypatch.setenv('BOOKING_DATA_DIR',str(state))
    monkeypatch.setenv('DATABASE_PATH','booking.sqlite3')
    checkout=tmp_path/'release-1'
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    first=storage.database_path()
    checkout2=tmp_path/'release-2'
    checkout2.mkdir()
    monkeypatch.chdir(checkout2)
    assert storage.database_path()==first
    assert Path(first).parent==state


def test_session_key_is_atomically_created_once(tmp_path,monkeypatch):
    monkeypatch.setenv('BOOKING_DATA_DIR',str(tmp_path))
    monkeypatch.delenv('SESSION_SECRET',raising=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys=list(pool.map(lambda _:storage.session_secret(),range(4)))
    assert len(set(keys))==1
    assert len(keys[0])>=32
    assert not list(tmp_path.glob('*.tmp'))
    assert storage.session_secret()==keys[0]


def test_explicit_secret_and_database_are_honored(tmp_path,monkeypatch):
    monkeypatch.setenv('SESSION_SECRET','x'*48)
    assert storage.session_secret()=='x'*48
    monkeypatch.setenv('SESSION_SECRET','short')
    with pytest.raises(RuntimeError):
        storage.session_secret()
    expected=tmp_path/'other'/'custom.sqlite3'
    monkeypatch.setenv('DATABASE_PATH',str(expected))
    assert storage.database_path()==str(expected)
