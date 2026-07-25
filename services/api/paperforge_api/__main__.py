from __future__ import annotations

import uvicorn

from paperforge_api.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "paperforge_api.main:app",
        host=settings.api_host,
        port=settings.api_port,
    )


if __name__ == "__main__":
    main()
