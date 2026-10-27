#!/usr/bin/env python3
"""render_browser.py — the browser HyperFrames renders with, and the localhost wrapper around it.

WHY THE WRAPPER (ISS-77, diagnosed by product/render-check.py). HyperFrames' render page server listens on
127.0.0.1 but hands Chrome the name `localhost`. A VPN, proxy, content filter or security tool can break the
name while the number still works, and then every render stops at page load with "Navigation timeout ...
exceeded" although the check (which loads its page by number) passed. A tiny shell script that starts the
same chrome-headless-shell with --host-resolver-rules="MAP localhost 127.0.0.1" --no-proxy-server, handed to
HyperFrames as HYPERFRAMES_BROWSER_PATH, takes the name out of it. HyperFrames 0.8.181 reads that variable in
its BrowserManager (resolveHeadlessShellPath) after PRODUCER_HEADLESS_SHELL_PATH, and refuses a path that does
not exist, so the script is always written before it is named.

Used by product/render-check.py (variants 3 and 4 of its experiment) and by product/reel_render.py (the third
retry after a render whose page did not load, remembered as "render.localhost-wrapper").

MAC AND LINUX ONLY. On Windows write_localhost_wrapper() returns None and both callers skip the route: the
wrapper is a /bin/sh script, which Windows cannot run as a program, and the .cmd equivalent cannot stand in for
it, because Node (which launches the browser for HyperFrames) refuses to start a .cmd or .bat file as a
program without a shell. Nothing on Windows changes.
"""
import glob, os, shlex, sys

WRAPPER_NAME = "chrome-localhost-wrapper.sh"
WRAPPER_FLAGS = '--host-resolver-rules="MAP localhost 127.0.0.1" --no-proxy-server'


def supported():
    """The wrapper route runs on a Mac or Linux only (see the note at the top)."""
    return os.name != "nt"


def find_browser():
    """The newest chrome-headless-shell HyperFrames downloaded, looked for where HyperFrames itself looks:
    its own cache first, then puppeteer's. None when neither has one (it downloads on the first render)."""
    home = os.path.expanduser("~")
    name = "chrome-headless-shell.exe" if os.name == "nt" else "chrome-headless-shell"
    for cache in (os.path.join(home, ".cache", "hyperframes", "chrome"),
                  os.path.join(home, ".cache", "puppeteer", "chrome-headless-shell")):
        bins = sorted(glob.glob(os.path.join(cache, "**", name), recursive=True))
        bins = [b for b in bins if os.path.isfile(b) and os.access(b, os.X_OK)]
        if bins:
            return bins[-1]
    return None


def default_folder():
    """Where reel_render keeps the wrapper: the engine's own _local/ (never shipped, kept by every update),
    the same folder product/fallbacks.py keeps its records in."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import fallbacks
        return os.path.join(fallbacks._local_dir(), "render-browser")
    except Exception:   # noqa: BLE001 — a copy without fallbacks.py still gets a working folder
        import tempfile
        return os.path.join(tempfile.gettempdir(), "reels-engine-render-browser")


def write_localhost_wrapper(folder=None, browser=None):
    """Write the wrapper into `folder` and return its path, or None when the route cannot run here: Windows,
    or no render browser downloaded yet. Rewritten every time, so it always points at the browser that is
    there now (a `browser ensure` can replace it)."""
    if not supported():
        return None
    browser = browser or find_browser()
    if not browser:
        return None
    folder = folder or default_folder()
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, WRAPPER_NAME)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nexec {shlex.quote(browser)} {WRAPPER_FLAGS} "$@"\n')
        os.chmod(path, 0o755)
        return path
    except OSError:
        return None


if __name__ == "__main__":
    b = find_browser()
    print(f"render browser: {b or 'not downloaded yet (it downloads on the first render)'}")
    print("localhost wrapper: " + ("available here" if supported() else "not used on Windows"))
