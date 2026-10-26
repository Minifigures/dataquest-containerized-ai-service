# Prompt Optimizer API

Dataquest guided project, *Deploy a Containerized AI Service*. A FastAPI
service that rewrites prompts, packaged to run somewhere other than the laptop
it was written on: multi-stage image, non-root user, health checks, Postgres
via Compose, and the two changes that decide whether a deploy succeeds.

```
python3 -m pytest test_app.py -v      # 35 tests, no DB, no key, no network
python3 benchmark_async.py            # measures the async trap, below
docker compose up --build             # the full stack
curl localhost:8000/health
curl -X POST localhost:8000/optimize \
     -H 'Content-Type: application/json' \
     -d '{"prompt":"please can you explain caching","style":"concise"}'
```

```json
{
  "original": "please can you explain caching",
  "optimized": "Explain caching. Answer in at most three sentences. Include one concrete example.",
  "style": "concise", "model": "rules", "source": "rules"
}
```

With `OPENAI_API_KEY` set the rewrite comes from the model and `source` reads
`llm`. Without it a deterministic rule-based rewriter answers, so the service
starts, responds and is testable with no key and no network. Which one answered
is reported rather than hidden, because a caller that must not ship a
rules-based answer needs to be able to tell.

## The measured part

The FastAPI course this builds on notes that "using `async def` without `await`
blocks the event loop, which is a common mistake". That is true, and it is the
kind of claim that is easy to agree with and still get wrong, because **the
broken version is indistinguishable from the correct one in development.** One
request at a time behaves identically. The difference appears only under
concurrency, which is when you are least equipped to investigate it.

So `benchmark_async.py` starts a real uvicorn server with two routes doing
identical blocking work, one a plain `def` and one an `async def`, and fires 20
real concurrent requests at each:

```
20 concurrent requests, each route sleeping 0.20s

plain def  (worker thread)
   total wall clock      0.33s
   slowest request       0.27s
   median request        0.24s

async def  (event loop)
   total wall clock      4.10s
   slowest request       3.97s
   median request        2.13s

If the requests ran one after another: 4.00s
plain def took  0.33s  (8% of serial)
async def took  4.10s  (102% of serial)

async def is 12.5x slower here.
```

The `async def` version took 102% of fully serial time: it did not overlap a
single request. It holds the event loop through each sleep, so nothing else in
the process can be served meanwhile. Adding workers does not fix it, it just
buys a small multiple of the same ceiling.

That is why every route in `main.py` is a plain `def`. `service.optimize()`
makes a synchronous network call, and FastAPI runs a plain `def` route in a
worker thread, so a slow provider occupies one thread rather than the whole
process. The one-word change that looks more modern is the one that falls over.

## The two deployment fixes

**The port must come from the environment, and the CMD form decides whether it
can.** Cloud platforms assign a port and pass it in `PORT`. An app that ignores
it and binds 8000 while the platform routes to 10000 is marked unhealthy and
killed. The fix is to read `$PORT`, but exec form `CMD` does not start a shell,
so nothing expands it:

```
$ uvicorn main:app --port '$PORT'
Error: Invalid value for '--port': '$PORT' is not a valid integer.
```

The container exits instantly, the platform restarts it, and the deploy fails
as a crash loop whose entire cause is a pair of JSON brackets. Shell form
expands it, with a local fallback:

```dockerfile
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

`test_dockerfile_cmd_is_shell_form` asserts there are no brackets on that line,
that `${PORT` appears, and that a `:-8000` fallback exists. Writing that test
also caught a parsing trap worth knowing: `HEALTHCHECK`'s own body begins with
the word `CMD` on a continuation line, so a naive line scan finds two `CMD`
instructions and cannot tell which one starts the container. The test joins
backslash continuations before parsing.

**The app must start without its database.** Platforms routinely start the web
service before the database is reachable, and on a first deploy it may not be
provisioned at all. An app that raises during startup in that situation never
starts: the platform sees the container exit, restarts it, watches it exit
again, and reports a crash loop whose real cause is one connection refused.

So `init_db()` returns a boolean and never raises, writes are best effort, and
`/health` reports the true state:

```json
{"status": "ok", "version": "1.0.0",
 "database": "unavailable (DATABASE_URL is not set)", "llm": "rules fallback"}
```

`/health` is deliberately still 200 there. A platform health check is asking
whether to restart the container, and a missing database is not a reason to,
because `/optimize` works fine without one. `/history` returns an empty list
and says why instead of returning a 500.

Four degradation paths are tested: no URL, an unparseable URL, an unreachable
host, and a write failing after a successful connect.

## Other decisions

**`extra: "forbid"` on the request model.** Without it, `{"promt": "..."}` is
accepted, the typo ignored, and the caller gets a 200 answering a question they
did not ask. With it they get a 422 naming the unexpected field. There are
tests for both the typo and an unexpected extra field.

**502 for an upstream failure, not 500.** A 502 tells the caller the fault was
outside this service and that retrying may work. A 500 tells them to file a
bug. The provider failing is not a bug in this service, so `UpstreamError` is
its own exception type and maps to 502.

**No internals in a 500 body.** The unexpected-failure handler logs the
traceback and returns a fixed string. A test raises an exception containing a
recognizable secret and asserts it does not appear in the response.

**The provider failure is not silently absorbed.** When credentials are
configured and the call fails, the service raises rather than quietly falling
back to the rules rewriter. Falling back would hide an outage behind output
that looks fine.

**Non-root, and `USER` before `CMD`.** A test asserts the ordering, because
`USER` placed after `CMD` still runs the app as root and looks correct at a
glance.

**Compose waits for a healthy database,** not just a started one, via
`depends_on: condition: service_healthy` plus a `pg_isready` healthcheck. The
API survives a late database now, but without this it would start without
persistence and stay that way silently.

## What was and was not verified here

| | |
|---|---|
| App, service, database degradation, error mapping | 35 tests passing |
| The async benchmark | run, numbers above are real output |
| Exec vs shell form `$PORT` | reproduced, error text above is real |
| Dockerfile and Compose contents | asserted by tests |
| `docker build` / `compose up` | **not run here**, no Docker daemon available |
| Deployment to a PaaS | **not run**, needs an account |

`render.yaml` is included as a working blueprint, but it has not been applied.

## Files

| File | Contents |
|---|---|
| `main.py` | HTTP layer: routes, status code mapping |
| `service.py` | prompt optimization, LLM call and rules fallback |
| `models.py` | Pydantic contracts with `extra: forbid` |
| `db.py` | optional persistence that degrades instead of crashing |
| `config.py` | environment-driven settings |
| `benchmark_async.py` | the measurement above |
| `test_app.py` | 35 tests |
| `Dockerfile` | multi-stage, non-root, healthcheck, shell form CMD |
| `docker-compose.yml` | API plus Postgres with a readiness gate |
| `render.yaml` | PaaS blueprint |
