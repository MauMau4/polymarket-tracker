"""
Development server entry point.

Uses asyncio.run(..., loop_factory=asyncio.SelectorEventLoop) to force the
SelectorEventLoop on Windows (Python 3.14+). psycopg async cannot use the
ProactorEventLoop that is the Windows default; passing loop_factory here is the
only reliable way to override it because:
  - asyncio.set_event_loop_policy() is deprecated in 3.14 and uvicorn 0.45
    does not respect it when starting its internal event loop.
  - Passing loop= to uvicorn.Config also has no effect in 0.45.
  - loop_factory= in asyncio.run() overrides the loop unconditionally, and
    uvicorn's in-process watchfiles reloader inherits it so hot-reload works.
"""
import asyncio
import sys

import uvicorn

if __name__ == "__main__":
    config = uvicorn.Config("app.main:app", host="0.0.0.0", port=8000, reload=True)
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(server.serve())
