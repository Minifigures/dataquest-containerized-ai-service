"""Measure what `async def` around a blocking call actually costs.

    python3 benchmark_async.py

The FastAPI course this project comes from says that "using async def without
await blocks the event loop, which is a common mistake". That is correct, and
it is also the kind of claim that is easy to nod along to and then get wrong in
production, because the broken version works perfectly in development: one
request at a time behaves identically either way. The difference only appears
under concurrency, which is exactly when you are least able to investigate.

So this measures it. Two routes do the identical blocking work. One is a plain
`def`, which FastAPI hands to a worker thread. The other is an `async def`,
which runs on the event loop and therefore holds it for the whole sleep.

Nothing here is simulated: a real uvicorn server is started and hit with real
concurrent requests.
"""

import argparse
import statistics
import subprocess
import sys
import time

SERVER_SOURCE = '''
import time
from fastapi import FastAPI

app = FastAPI()
BLOCK_SECONDS = 0.20

@app.get("/threaded")
def threaded():
    """Plain def. FastAPI runs this in a worker thread."""
    time.sleep(BLOCK_SECONDS)
    return {"route": "threaded"}

@app.get("/blocking")
async def blocking():
    """async def wrapped around synchronous work. Holds the event loop."""
    time.sleep(BLOCK_SECONDS)
    return {"route": "blocking"}

@app.get("/ping")
def ping():
    return {"ok": True}
'''


def start_server(port):
    with open("_benchmark_server.py", "w") as handle:
        handle.write(SERVER_SOURCE)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "_benchmark_server:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return process


def wait_for_server(base, timeout=30):
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(base + "/ping", timeout=1).status_code == 200:
                return True
        except Exception:
            time.sleep(0.2)
    return False


def hammer(base, path, concurrency):
    """Fire `concurrency` requests at once and return the wall clock seconds."""
    import httpx
    import threading

    latencies = []
    lock = threading.Lock()

    def one():
        started = time.perf_counter()
        try:
            httpx.get(base + path, timeout=120)
        except Exception as problem:
            print("   request failed: %s" % problem)
        taken = time.perf_counter() - started
        with lock:
            latencies.append(taken)

    threads = [threading.Thread(target=one) for _ in range(concurrency)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return time.perf_counter() - started, latencies


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args(argv)

    base = "http://127.0.0.1:%d" % args.port
    block_seconds = 0.20

    print(__doc__.strip())
    print()
    print("=" * 68)
    print("%d concurrent requests, each route sleeping %.2fs"
          % (args.concurrency, block_seconds))
    print("=" * 68)

    server = start_server(args.port)
    try:
        if not wait_for_server(base):
            print("server did not start")
            return 1

        results = {}
        for path, label in (("/threaded", "plain def  (worker thread)"),
                            ("/blocking", "async def  (event loop)")):
            # One warm-up request so import and connection costs do not land
            # in the measurement.
            hammer(base, path, 1)
            elapsed, latencies = hammer(base, path, args.concurrency)
            results[path] = (elapsed, latencies)
            print("\n%s" % label)
            print("   total wall clock   %7.2fs" % elapsed)
            print("   slowest request    %7.2fs" % max(latencies))
            print("   median request     %7.2fs" % statistics.median(latencies))

        threaded = results["/threaded"][0]
        blocking = results["/blocking"][0]
        serial = block_seconds * args.concurrency

        print("\n" + "=" * 68)
        print("If the requests ran one after another: %.2fs" % serial)
        print("plain def took  %.2fs  (%.0f%% of serial)"
              % (threaded, 100 * threaded / serial))
        print("async def took  %.2fs  (%.0f%% of serial)"
              % (blocking, 100 * blocking / serial))
        if threaded > 0:
            print("\nasync def is %.1fx slower here." % (blocking / threaded))
        print("\nThe async version serialized: it holds the event loop through")
        print("each sleep, so nothing else can be served meanwhile. Both")
        print("routes are identical on a single request, which is why this")
        print("survives development and only shows up under load.")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        import os
        if os.path.exists("_benchmark_server.py"):
            os.remove("_benchmark_server.py")


if __name__ == "__main__":
    sys.exit(main())
