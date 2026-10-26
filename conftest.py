"""Initialize a test-only runtime before any test imports the application."""
import os
import tempfile

_runtime=tempfile.TemporaryDirectory()
os.environ['BOOKING_DATA_DIR']=_runtime.name
os.environ['DATABASE_PATH']=os.path.join(_runtime.name,'initial.sqlite3')
os.environ['SESSION_SECRET']='test-only-session-secret-at-least-32-characters'
os.environ['ALLOW_DEMO']='true'
