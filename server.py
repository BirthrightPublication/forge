from __future__ import annotations
import contextlib
import os
import secrets
import socket
import sys
from pathlib import Path
import psutil
import uvicorn
from forge.app import create_app
from forge import __version__
from forge.core import data_directory, json_write


def main():
    data = data_directory()
    data.mkdir(parents=True, exist_ok=True)
    lock = (data/'service.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0'); lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # Another instance owns this user's runtime.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # POSIX reuse permits a clean restart through TIME_WAIT, not another live listener.
    # Do not enable SO_REUSEADDR on Windows; Winsock has different sharing semantics.
    if os.name != 'nt':
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    port = int(os.getenv('FORGE_PORT','8765'))
    if not 1024 <= port <= 65535:
        raise RuntimeError('FORGE_PORT must be between 1024 and 65535.')
    try:
        sock.bind(('127.0.0.1', port))
    except OSError as exc:
        raise RuntimeError(f'Port {port} is already in use. Stop the other FORGE instance or explicitly set FORGE_PORT. The address is not silently changed, so bookmarks remain stable.') from exc
    token = secrets.token_urlsafe(32)
    app = create_app(data, token, port)
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', access_log=False,
                            log_config=None, proxy_headers=False, server_header=False, timeout_keep_alive=5)
    server = uvicorn.Server(config)
    app.state.shutdown = lambda: setattr(server, 'should_exit', True)
    runtime = {'port':port,'token':token,'pid':os.getpid(),'started':psutil.Process().create_time(),'version':__version__,'root':str(Path(__file__).resolve().parent)}
    json_write(data/'runtime.json', runtime)
    try:
        server.run(sockets=[sock])
    finally:
        with contextlib.suppress(OSError):
            (data/'runtime.json').unlink()
        sock.close()
        lock.close()

if __name__ == '__main__':
    main()
