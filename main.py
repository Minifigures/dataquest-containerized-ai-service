"""HTTP layer for the Prompt Optimizer API.

This module parses requests, maps failures onto status codes, and returns
validated responses. All the actual work lives in service.py and db.py.

Note that the routes are plain `def`, not `async def`. service.optimize() makes
a synchronous network call, and FastAPI runs a plain `def` route in a worker
thread, so a slow provider ties up one thread while other requests continue.
Writing `async def` around the same blocking call would instead occupy the
event loop itself and serialize every request in the process. That is the
single easiest way to make a working FastAPI service fall over under load, and
benchmark_async.py measures the difference rather than asserting it.
"""

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

import config
import db
import service
from models import HealthResponse, PromptRequest, PromptResponse

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("prompt-optimizer")

app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION)


@app.on_event("startup")
def startup():
    """Start up whether or not the database is reachable.

    init_db() returns a boolean instead of raising, so a database that is not
    ready yet degrades the service rather than preventing it from booting into
    a restart loop.
    """
    if db.init_db():
        logger.info("database connected")
    else:
        logger.warning("starting without a database: %s", db.status())


@app.get("/health", response_model=HealthResponse)
def health():
    """Liveness and dependency status.

    Always 200 while the process is serving. A platform health check asks
    whether to restart the container, and the answer is no just because the
    database is down: the optimize endpoint still works. The database state is
    reported in the body for anything that needs to care.
    """
    return HealthResponse(
        status="ok",
        version=config.APP_VERSION,
        database=db.status(),
        llm="configured" if config.has_llm_credentials() else "rules fallback",
    )


@app.get("/styles")
def styles():
    return {"styles": service.available_styles()}


@app.post("/optimize", response_model=PromptResponse)
def optimize(request: PromptRequest):
    """Rewrite a prompt.

    Pydantic has already rejected a malformed body with a 422 before this runs,
    including any unexpected field, because the model forbids extras.
    """
    try:
        optimized, source = service.optimize(request.prompt, request.style)
    except ValueError as problem:
        # A known-bad input that Pydantic could not express, such as an
        # unsupported style. 422 says "your request", not "our fault".
        raise HTTPException(status_code=422, detail=str(problem))
    except service.UpstreamError as problem:
        # The provider failed. 502 tells the caller the fault was upstream and
        # that retrying may work, which a 500 would not.
        logger.warning("upstream failure: %s", problem)
        raise HTTPException(status_code=502, detail=str(problem))
    except Exception:
        # Genuinely unexpected. Log the trace, return no internals.
        logger.exception("unexpected failure in optimize")
        raise HTTPException(status_code=500, detail="Internal error.")

    db.record(request.prompt, optimized, request.style, source)

    return PromptResponse(
        original=request.prompt,
        optimized=optimized,
        style=request.style,
        model=config.MODEL if source == "llm" else "rules",
        source=source,
    )


@app.get("/history")
def history(limit: int = 20):
    """Recent optimizations, or an explanation of why there are none."""
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422,
                            detail="limit must be between 1 and 200.")
    if not db.is_available():
        return JSONResponse(
            status_code=200,
            content={"items": [], "database": db.status(),
                     "note": "History needs a database. The service works "
                             "without one."})
    return {"items": db.history(limit), "database": db.status()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
