"""Keep runtime state outside a replaceable Git checkout."""
import os
import secrets
from pathlib import Path


def state_directory():
    root=Path(os.getenv('BOOKING_DATA_DIR', str(Path.home()/'.local'/'share'/'efremov-booking'/'client-9d51bf9a')))
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root


def database_path():
    configured=Path(os.getenv('DATABASE_PATH','booking.sqlite3'))
    path=configured if configured.is_absolute() else state_directory()/configured
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    return str(path)


def session_secret():
    configured=os.getenv('SESSION_SECRET')
    if configured is not None:
        if len(configured)<32:
            raise RuntimeError('SESSION_SECRET must contain at least 32 characters')
        return configured
    path=state_directory()/'session.key'
    if path.exists():
        value=path.read_text(encoding='ascii')
        if len(value)<32:
            raise RuntimeError('Stored session key is invalid')
        return value
    temporary=path.with_name('session.'+secrets.token_hex(12)+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w',encoding='ascii') as stream:
            stream.write(secrets.token_urlsafe(48))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary,path)
        except FileExistsError:
            pass
        return path.read_text(encoding='ascii')
    finally:
        temporary.unlink(missing_ok=True)
