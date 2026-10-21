import logging
import signal
import asyncio

from app.config import get_settings
from app.events.topics import Topics


def setup(name: str) -> tuple[logging.Logger, Topics]:
    logging.basicConfig(level=logging.INFO, format=f"%(asctime)s [{name}] %(message)s", datefmt="%H:%M:%S")
    for noisy in ("aiokafka", "httpx", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(name), Topics(get_settings().kafka_topic_prefix)


def stop_event() -> asyncio.Event:
    """Graceful shutdown on Ctrl+C / SIGTERM (what Kubernetes sends before killing a pod):
    finish the current message, commit, leave the group cleanly — so the rebalance is instant."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    return stop
