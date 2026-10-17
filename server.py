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
    bound = False
    for port in range(8765, 8786):
        try:
            sock.bind(('127.0.0.1', port)); bound = True; break
        except OSError:
            continue
    if not bound:
        sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    app = create_app(data, token, port)
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', access_log=False,
                            log_config=None, proxy_headers=False, server_header=False, timeout_keep_alive=5)
    server = uvicorn.Server(config)
    app.state.shutdown = lambda: setattr(server, 'should_exit', True)
    runtime = {'port':port,'token':token,'pid':os.getpid(),'started':psutil.Process().create_time(),'version':'0.1.0'}
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
