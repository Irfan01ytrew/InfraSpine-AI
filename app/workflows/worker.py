"""Independent workflow worker: python -m app.workflows.worker [--once]."""

import argparse
import asyncio
import signal

from app.core.config import get_settings
from app.core.crypto import rotating_cipher_from_settings
from app.db.session import create_engine, create_sessionmaker
from app.workflows.engine import run_worker, tick


async def main(once=False):
    settings = get_settings()
    engine = create_engine(settings.database_url.get_secret_value())
    factory = create_sessionmaker(engine)
    cipher = rotating_cipher_from_settings(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    try:
        if once: await tick(factory, cipher)
        else: await run_worker(factory, cipher, stop)
    finally:
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    asyncio.run(main(parser.parse_args().once))
