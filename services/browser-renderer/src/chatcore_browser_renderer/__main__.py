from aiohttp import web

from .server import create_app

web.run_app(create_app(), host="0.0.0.0", port=8080, access_log=None)  # noqa: S104
