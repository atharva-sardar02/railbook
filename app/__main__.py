"""Twelve-Factor VII: the app is self-contained and binds to $PORT."""

import uvicorn

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - must listen on all interfaces inside a container
        port=settings.port,
        workers=1,  # factor VIII: scale with more pods, not more workers
        log_config=None,  # keep our JSON logging
        timeout_graceful_shutdown=settings.shutdown_grace_seconds,
    )
