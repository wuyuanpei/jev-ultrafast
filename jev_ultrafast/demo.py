"""Loopback-only inspector for the Jev browser agent."""

import atexit
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .agent import Agent
from .baseline import BaselineConflict, BaselineManager
from .model import system1_config
from .questions import MAX_STEPS

ROOT = Path(__file__).parent
PORT = int(os.environ.get("TYPESAFE_DEMO_PORT", "8766"))
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
AGENT = None
BASELINE = None


def baseline():
    global BASELINE
    if BASELINE is None:
        BASELINE = BaselineManager(Path.cwd() / "artifacts" / "baselines", ORIGIN)
    return BASELINE


def load_environment():
    path = Path.cwd() / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key, value)


def response_state():
    state = (
        AGENT.snapshot()
        if AGENT
        else {"page": None, "status": "idle", "history": [], "decision": None}
    )
    return {
        **state,
        "text_model": os.environ.get("TEXT_MODEL", "deepseek-chat"),
        "laya_base_url": os.environ.get("LAYA_BASE_URL", "http://127.0.0.1:8791").rstrip("/"),
        "text_model_base_url": os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        "max_steps": MAX_STEPS,
        "baseline_active": bool(BASELINE and BASELINE.active),
        "system1_options": [system1_config(p) for p in ("laya", "deepseek", "jev")],
        "system1_default": system1_config()["provider"],
    }


def close_browser():
    global AGENT
    if AGENT:
        AGENT.close()
        AGENT = None


def command(name, body):
    global AGENT
    if name.startswith("baseline/"):
        manager = baseline()
        if name == "baseline/start":
            result = manager.start(body.get("task_ids"), body.get("repeats", 1), body.get("system1_provider"))
            close_browser()
            return result
        if name == "baseline/stop":
            return manager.stop()
        if name == "baseline/save":
            return manager.save(body.get("run_id"))
        if name == "baseline/delete":
            return manager.delete(body.get("run_id"))
        raise ValueError("Unknown baseline command")
    if BASELINE and BASELINE.active:
        raise BaselineConflict("Baseline owns the browser; stop it before running a free task")
    if name == "reset":
        scenario = body.get("scenario", "flights")
        if scenario not in {"travel", "research", "flights", "custom"}:
            raise ValueError("Unknown demo scenario")
        goal = body.get("goal", "").strip()
        if not goal or len(goal) > 2000:
            raise ValueError("Enter 1–2,000 characters")
        default_url = (
            "https://www.google.com/travel/flights?hl=en"
            if scenario == "flights"
            else f"{ORIGIN}/fixture.html?scenario={scenario}" if scenario != "custom" else ""
        )
        url = body.get("url", default_url)
        if not isinstance(url, str):
            raise ValueError("Enter a valid http:// or https:// website URL")
        url = url.strip()
        try:
            parsed = urlparse(url)
            valid_url = (
                0 < len(url) <= 2048
                and parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not any(char.isspace() or ord(char) < 32 for char in url)
            )
            parsed.port  # Reject malformed port numbers before replacing the current task.
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("Enter a valid http:// or https:// website URL (up to 2,048 characters)")
        config = system1_config(body.get("system1_provider"), require_key=True)
        close_browser()
        AGENT = Agent(
            url,
            goal,
            screenshots=True,
            system1_provider=config["provider"],
            record_dir=(
                Path.cwd() / "artifacts" / "frames" if body.get("record") else None
            ),
        )
        AGENT.state["scenario"] = scenario
    else:
        if AGENT is None:
            raise ValueError("Start a demo first")
        AGENT.command(name, body)
    return response_state()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, content, mime="application/json"):
        content = content if isinstance(content, bytes) else content.encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.headers.get("Host") != f"127.0.0.1:{PORT}":
            return self.send(403, "Forbidden", "text/plain")
        path = urlparse(self.path).path
        if path.startswith("/api/baseline"):
            try:
                manager = baseline()
                query = parse_qs(urlparse(self.path).query)
                run_id = query.get("run_id", [None])[0]
                attempt_id = query.get("attempt_id", [None])[0]
                if path == "/api/baseline":
                    result = manager.state()
                elif path == "/api/baseline/run":
                    result = manager.read_run(run_id)
                elif path == "/api/baseline/attempt":
                    result = {**manager.read_attempt(run_id, attempt_id),
                              "text_model": os.getenv("TEXT_MODEL", "deepseek-chat"),
                              "laya_base_url": os.getenv("LAYA_BASE_URL", "http://127.0.0.1:8791"),
                              "text_model_base_url": os.getenv("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1"),
                              "max_steps": MAX_STEPS}
                elif path == "/api/baseline/frame":
                    return self.send(200, manager.frame(run_id, attempt_id, query.get("name", [None])[0]),
                                     "image/jpeg")
                else:
                    raise ValueError("Unknown baseline route")
                return self.send(200, json.dumps(result))
            except (ValueError, OSError) as error:
                return self.send(400, json.dumps({"error": str(error)}))
        if path == "/api/state":
            with LOCK:
                return self.send(200, json.dumps(response_state()))
        if path == "/demo.mp4":
            video = ROOT.parent / "docs" / "demo.mp4"
            if video.exists():
                return self.send(200, video.read_bytes(), "video/mp4")
        files = {
            "/": ("index.html", "text/html"),
            "/app.js": ("app.js", "text/javascript"),
            "/style.css": ("style.css", "text/css"),
            "/fixture.html": ("fixture.html", "text/html"),
        }
        if path not in files:
            return self.send(404, "Not found", "text/plain")
        name, mime = files[path]
        content = (ROOT / "static" / name).read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
        self.send(200, content, mime + "; charset=utf-8")

    def do_POST(self):
        if (
            self.headers.get("Host") != f"127.0.0.1:{PORT}"
            or self.headers.get("X-Demo-Token") != TOKEN
            or self.headers.get("Origin") not in (None, ORIGIN)
        ):
            return self.send(403, json.dumps({"error": "Local demo requests only"}))
        if not LOCK.acquire(blocking=False):
            return self.send(
                409, json.dumps({"error": "A browser step is already running"})
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 8192:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            result = command(self.path.removeprefix("/api/"), body)
            self.send(200, json.dumps(result))
        except BaselineConflict as error:
            self.send(409, json.dumps({"error": str(error)}))
        except (ValueError, RuntimeError, TimeoutError) as error:
            self.send(400, json.dumps({"error": str(error)}))
        except Exception:
            self.send(
                500,
                json.dumps(
                    {
                        "error": "Local demo failed; no automatic retry. Reset to recover."
                    }
                ),
            )
        finally:
            LOCK.release()

    def log_message(self, *_args):
        pass


def main():
    load_environment()
    baseline()
    atexit.register(close_browser)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Jev Ultrafast: {ORIGIN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
