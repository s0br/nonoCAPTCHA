import asyncio
import signal
import shutil
import sys

from aiohttp import web
from async_timeout import timeout
from asyncio import CancelledError
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from functools import partial, wraps
from pathlib import Path

from nonocaptcha import util
from nonocaptcha.proxy import ProxyDB
from nonocaptcha.solver import Solver

SECRET_KEY = "CHANGEME"

proxy_source = None  # Can be URL or file location
proxies = ProxyDB(last_banned_timeout=45*60)

pool = ThreadPoolExecutor()
main_loop = asyncio.get_event_loop()
asyncio.get_child_watcher().attach_loop(main_loop)
app = web.Application()

# Clear Chrome temporary profiles
dir = f"{Path.home()}/.pyppeteer/.dev_profile"
shutil.rmtree(dir, ignore_errors=True)


def timer(duration):
    def wrap(func):
        @wraps(func)
        async def wrap(*args, **kwargs):
            async with timeout(duration):
                return await func(*args, **kwargs)
        return wrap
    return wrap


@timer(duration=10)  # 180 seconds seems reasonable
async def work(pageurl, sitekey, loop):
    asyncio.set_event_loop(loop)
    while 1:
        fut = asyncio.run_coroutine_threadsafe(
            proxies.get(), main_loop
        )
        proxy = fut.result()
        options = {"ignoreHTTPSErrors": True, "args": ["--timeout 5"]}
        client = Solver(
            pageurl,
            sitekey,
            options=options,
            proxy=proxy,
            loop=loop,
        )
        try:
            task = loop.create_task(client.start())
            await task
            with suppress(BaseException):
                task.exception()
        except CancelledError:
            break
        else:
            result = task.result()
            if result:
                if result['status'] == "detected":
                    asyncio.run_coroutine_threadsafe(
                        proxies.set_banned(proxy), main_loop
                    )
                else:
                    if result['status'] == "success":
                        return result['code']
                        

def sub_loop(pageurl, sitekey):
    async def cancel_task(task):
        task.cancel()
        #  Please don't hurt me, I'll do better next time
        with suppress(BaseException):
            await task

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(work(pageurl, sitekey, loop))
    #  Cancel all pending tasks in the loop
    for task in asyncio.Task.all_tasks():
        loop.run_until_complete(cancel_task(task))
    if result:
        return result


async def get_solution(request):
    params = request.rel_url.query
    pageurl = params.get("pageurl")
    sitekey = params.get("sitekey")
    secret_key = params.get("secret_key")
    if not pageurl or not sitekey or not secret_key:
        response = {"error": "invalid request"}
    else:
        if secret_key != SECRET_KEY:
            response = {"error": "unauthorized attempt logged"}
        else:
            if pageurl and sitekey:
                f = partial(sub_loop, pageurl, sitekey)
                result = await main_loop.run_in_executor(pool, f)
                if result:
                    response = {"solution": result}
                else:
                    response = {"error": "worker timed-out"}
    return web.json_response(response)


async def load_proxies():
    print('Loading proxies')
    while 1:
        protos = ["http://", "https://"]
        if any(p in proxy_source for p in protos):
            f = util.get_page
        else:
            f = util.load_file

        try:
            result = await f(proxy_source)
        except Exception:
            continue
        else:
            proxies.add(result.split('\n'))
            print('Proxies loaded')
            await asyncio.sleep(10 * 60)


async def start_background_tasks(app):
    app["dispatch"] = app.loop.create_task(load_proxies())


async def cleanup_background_tasks(app):
    app["dispatch"].cancel()
    await app["dispatch"]


#  Not sure if I need these here, will check later. And loop.add_signal_handler
#  might be the better option
def signal_handler(signal, frame):
    main_loop.stop()
    main_loop.close()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGHUP, signal_handler)

app.router.add_get("/", get_solution)
app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=5000)
