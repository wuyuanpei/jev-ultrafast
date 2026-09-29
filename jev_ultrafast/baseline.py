"""Sequential, server-owned baseline runs and durable local evidence."""

import base64
import csv
import hashlib
import io
import json
import os
import re
import threading
import time
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

from .agent import Agent, RunCancelled
from .baseline_tasks import VERSION, inspect, prepare, task_catalog
from .model import action_space
from .questions import MAX_STEPS

TERMINAL = {"completed", "cancelled", "error", "interrupted"}


class BaselineConflict(ValueError):
    pass


class EnvironmentBlocked(RuntimeError):
    pass


class ConstraintViolation(RuntimeError):
    pass


def now():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def atomic_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def public_endpoint(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


class BaselineManager:
    def __init__(self, root, origin, *, agent_factory=Agent, inspector=inspect, preparer=prepare,
                 timeout=300):
        self.root = Path(root).resolve()
        self.tasks = task_catalog(origin)
        self.agent_factory, self.inspector, self.preparer = agent_factory, inspector, preparer
        self.timeout = timeout
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.active = False
        self.batch = None
        self.snapshots = {}
        self.thread = None
        self.recover()

    def clean(self, data):
        if isinstance(data, dict):
            return {k: self.clean(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.clean(v) for v in data]
        if isinstance(data, str):
            for key in ("LAYA_API_KEY", "TEXT_MODEL_API_KEY"):
                secret = os.environ.get(key)
                if secret:
                    data = data.replace(secret, "[REDACTED]")
        return data

    def folder(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"\d{8}_\d{6}_\d{3}(?:_\d+)?", run_id):
            raise ValueError("Invalid run ID")
        folder = (self.root / run_id).resolve()
        if not folder.is_relative_to(self.root):
            raise ValueError("Invalid run path")
        return folder

    def recover(self):
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("*/summary.json")):
            try:
                folder = self.folder(path.parent.name)
                batch = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
                if batch["status"] not in TERMINAL:
                    batch.update(status="interrupted", reason="Server restarted; not resumed automatically")
                    for row in batch["attempts"]:
                        if row["status"] in {"queued", "running"}:
                            row.update(status="interrupted", reason=batch["reason"])
                    self.write_summary(batch)
                self.batch = batch
            except (ValueError, OSError, KeyError, TypeError):
                continue

    def runs(self):
        rows = []
        for path in sorted(self.root.glob("*/summary.json"), reverse=True):
            try:
                batch = self.read_run(path.parent.name)
                rows.append({k: batch[k] for k in ("id", "status", "created_at")})
            except (ValueError, OSError, KeyError):
                continue
        return rows

    def state(self):
        with self.lock:
            state = {"tasks": deepcopy(self.tasks), "active": self.active, "batch": deepcopy(self.batch)}
        state["runs"] = self.runs()
        return self.clean(state)

    def read_run(self, run_id):
        folder = self.folder(run_id)
        with self.lock:
            if self.batch and self.batch["id"] == run_id:
                return self.clean(deepcopy(self.batch))
        try:
            return json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("Unknown run") from None

    def attempt_folder(self, run_id, attempt_id):
        batch = self.read_run(run_id)
        if not isinstance(attempt_id, str) or attempt_id not in {r["id"] for r in batch["attempts"]}:
            raise ValueError("Unknown attempt")
        folder = (self.folder(run_id) / attempt_id).resolve()
        if not folder.is_relative_to(self.folder(run_id)):
            raise ValueError("Invalid attempt path")
        return folder

    def read_attempt(self, run_id, attempt_id):
        folder = self.attempt_folder(run_id, attempt_id)
        with self.lock:
            snapshot = deepcopy(self.snapshots.get((run_id, attempt_id)))
        if snapshot is None:
            path = folder / "trace.json"
            snapshot = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
                "page": None, "history": [], "model_calls": [], "status": "queued",
            }
        row = next(r for r in self.read_run(run_id)["attempts"] if r["id"] == attempt_id)
        snapshot["baseline_attempt"] = row
        frames = snapshot.get("observations", [])
        calls = {call["id"]: call for call in snapshot.get("model_calls", [])}
        if (any("page" not in frame for frame in frames)
                or any("observation_index" not in call for call in calls.values())):
            # Reconstruct old traces from event order, not repeated step numbers or URLs.
            events = folder / "events.jsonl"
            index, laya_id = -1, None
            if events.exists():
                with events.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue  # A running worker may still be appending the last line.
                        if event.get("event") == "observed":
                            index += 1
                            laya_id = None
                            if index < len(frames):
                                frames[index].setdefault("page", event.get("page"))
                        elif event.get("event") == "model_start":
                            call = calls.get(event.get("call", {}).get("id"))
                            if call is not None:
                                call.setdefault("observation_index", index if 0 <= index < len(frames) else None)
                                if call["kind"] == "laya":
                                    laya_id = call["id"]
                                else:
                                    call.setdefault("laya_call_id", laya_id)
        for frame in frames:
            if frame.get("page"):
                frame.setdefault("elements", action_space(frame["page"].get("actions", []))[0])
        return self.clean(snapshot)

    def frame(self, run_id, attempt_id, name):
        if not isinstance(name, str) or not re.fullmatch(r"\d{6}\.jpg", name):
            raise ValueError("Invalid screenshot name")
        folder = self.attempt_folder(run_id, attempt_id)
        path = (folder / "screenshots" / name).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise ValueError("Unknown screenshot")
        return path.read_bytes()

    def start(self, task_ids=None, repeats=1):
        ids = [t["id"] for t in self.tasks] if task_ids is None else task_ids
        allowed = {t["id"] for t in self.tasks}
        if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids) or not set(ids) <= allowed):
            raise ValueError("Choose known task IDs without duplicates")
        if type(repeats) is not int or not 1 <= repeats <= 10:
            raise ValueError("Repeat count must be 1-10")
        with self.lock:
            if self.active:
                raise BaselineConflict("A baseline run is already active")
            self.root.mkdir(parents=True, exist_ok=True)
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            candidate, suffix = run_id, 0
            while True:
                try:
                    self.folder(candidate).mkdir()
                    break
                except FileExistsError:
                    suffix += 1
                    candidate = f"{run_id}_{suffix}"
            run_id = candidate
            tasks = [t for t in self.tasks if t["id"] in ids]
            attempts = [dict(id=f"{t['id']}/attempt-{n:02}", task_id=t["id"], name=t["name"],
                             repeat=n, status="queued", actions=0, laya_calls=0, text_calls=0,
                             elapsed_ms=0, reason="", evaluation=None)
                        for t in tasks for n in range(1, repeats + 1)]
            self.batch = dict(id=run_id, status="running", path=str(self.folder(run_id)),
                              created_at=now(), suite_version=VERSION, attempts=attempts)
            self.snapshots = {}
            self.stop_event.clear()
            self.active = True
        try:
            sources = {str(p.relative_to(Path(__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in Path(__file__).parent.rglob("*")
                       if p.suffix in {".py", ".js", ".html"} and "__pycache__" not in p.parts}
            manifest = {"run_id": run_id, "created_at": self.batch["created_at"], "suite_version": VERSION,
                        "tasks": tasks, "repeats": repeats, "sources_sha256": sources,
                        "limits": {"actions": MAX_STEPS, "laya_calls": MAX_STEPS * 2, "seconds": self.timeout},
                        "viewport": {"width": 1120, "height": 780}, "shared_browser_profile": True,
                        "models": {k: os.environ.get(k, default) for k, default in (
                            ("LAYA_MODEL", "laya-v10s"), ("TEXT_MODEL", "deepseek-chat"),
                            ("LAYA_TIMEOUT_SECONDS", "120"), ("TEXT_MODEL_REASONING", "disabled"))},
                        "endpoints": {"laya": public_endpoint(os.getenv("LAYA_BASE_URL", "http://127.0.0.1:8791")),
                                      "text": public_endpoint(os.getenv("TEXT_MODEL_BASE_URL",
                                                                        "https://api.deepseek.com/v1"))}}
            atomic_json(self.folder(run_id) / "manifest.json", self.clean(manifest))
            self.persist()
            self.thread = threading.Thread(target=self.work, args=(run_id, tasks), daemon=True,
                                           name="baseline-worker")
            self.thread.start()
        except Exception:
            with self.lock:
                self.active = False
                self.batch["status"] = "error"
            raise
        return self.state()

    def stop(self):
        with self.lock:
            if self.active and self.batch["status"] not in TERMINAL:
                self.stop_event.set()
                self.batch["status"] = "stopping"
        self.persist()
        return self.state()

    def write_summary(self, batch):
        folder = self.folder(batch["id"])
        counts = Counter(r["status"] for r in batch["attempts"])
        batch["counts"] = dict(counts)
        batch["success_rate"] = counts["passed"] / max(1, len(batch["attempts"]))
        atomic_json(folder / "summary.json", self.clean(batch))
        fields = ["task_id", "repeat", "status", "actions", "laya_calls", "text_calls", "elapsed_ms", "reason"]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(self.clean(batch["attempts"]))
        (folder / "summary.csv").write_text(output.getvalue(), encoding="utf-8-sig")
        report = ["# 基线运行结果", "", f"批次：{batch['id']}", f"状态：{batch['status']}",
                  f"通过：{counts['passed']} / {len(batch['attempts'])}（包含阻塞和取消的计划次数）", "",
                  f"总成功率：{batch['success_rate']:.1%}；环境阻塞：{counts['environment_blocked']} 次", "",
                  "| 任务 | 轮次 | 状态 | 动作 | Laya / 文本 | 耗时(ms) |", "|---|---:|---|---:|---:|---:|"]
        for row in batch["attempts"]:
            report.append(f"| {row['name']} | {row['repeat']} | {row['status']} | {row['actions']} | "
                          f"{row['laya_calls']} / {row['text_calls']} | {row['elapsed_ms']} |")
        report += ["", "状态与证据详见各任务的 evaluation.json；模型宣告 DONE 不等于独立验收通过。",
                   "文件可能包含网页内容和个人信息，分享前请检查。"]
        (folder / "report.zh.md").write_text("\n".join(report), encoding="utf-8")

    def persist(self):
        with self.io_lock:
            with self.lock:
                batch = deepcopy(self.batch)
            if batch:
                self.write_summary(batch)

    def save(self, run_id=None):
        with self.lock:
            current = self.batch["id"] if self.batch else None
        run_id = run_id or current
        if run_id is None:
            raise ValueError("No run to save")
        if run_id == current:
            self.persist()
        batch = self.read_run(run_id)
        return {**self.state(), "saved_path": batch["path"]}

    def work(self, run_id, tasks):
        by_id = {t["id"]: t for t in tasks}
        fatal = ""
        try:
            for index in range(len(self.batch["attempts"])):
                with self.lock:
                    row = self.batch["attempts"][index]
                    if self.stop_event.is_set() or fatal:
                        row.update(status="cancelled", reason=fatal or "Stopped by user")
                        continue
                    row.update(status="running", started_at=now())
                fatal = self.run_attempt(run_id, row, by_id[row["task_id"]])
                self.persist()
        except Exception as error:
            fatal = self.clean(str(error))
        finally:
            with self.lock:
                for row in self.batch["attempts"]:
                    if row["status"] in {"running", "queued"}:
                        row.update(status="error" if fatal else "cancelled", reason=fatal or "Stopped")
                self.batch.update(status="error" if fatal else "cancelled" if self.stop_event.is_set() else "completed",
                                  ended_at=now(), reason=fatal)
            try:
                self.persist()
            finally:
                with self.lock:
                    self.active = False

    def run_attempt(self, run_id, row, task):
        folder = self.attempt_folder(run_id, row["id"])
        (folder / "screenshots").mkdir(parents=True)
        started = time.monotonic()
        observations, evaluations, violations = [], [], []
        first_met = None
        agent = None
        fatal = ""

        def cancelled():
            if self.stop_event.is_set():
                return "cancelled"
            return "timeout" if time.monotonic() - started >= self.timeout else None

        def append(event, payload):
            with (folder / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(self.clean({"time": now(), "event": event, **payload}),
                                        ensure_ascii=False) + "\n")

        def publish(current):
            snapshot = deepcopy(current.snapshot())
            snapshot["page"].pop("screenshot", None)
            snapshot["observations"] = deepcopy(observations)
            if observations:
                snapshot["page"]["screenshot_url"] = observations[-1].get("screenshot_url")
            snapshot = self.clean(snapshot)
            atomic_json(folder / "trace.json", snapshot)
            with self.lock:
                self.snapshots[(run_id, row["id"])] = snapshot
                calls = snapshot.get("model_calls", [])
                row.update(actions=len(snapshot["history"]), laya_calls=sum(c["kind"] == "laya" for c in calls),
                           text_calls=sum(c["kind"] == "text" for c in calls),
                           elapsed_ms=round((time.monotonic() - started) * 1000))

        def event(name, current, payload):
            nonlocal first_met
            if name == "model_start":
                call = payload["call"]
                call["observation_index"] = len(observations) - 1 if observations else None
                if call["kind"] == "text":
                    call["laya_call_id"] = next((c["id"] for c in reversed(current.state["model_calls"][:-1])
                                                if c["kind"] == "laya"
                                                and c.get("observation_index") == call["observation_index"]), None)
            append(name, payload)
            if name == "action_started" and task["id"] in {"05_baidu", "10_ctrip"}:
                label = payload["action"].get("label", "")
                if re.search(r"登录|注册|预订|订票|购买|选购|支付|\blog\s*in\b", label, re.I):
                    violations.append("Forbidden login or purchase action attempted")
                    append("action_rejected", {"reason": violations[-1]})
                    raise ConstraintViolation(violations[-1])
            if name == "observation":
                page, history = current.state["page"], current.state["history"]
                try:
                    result = self.inspector(task, current.browser, page, history)
                except Exception as error:
                    result = {"outcome": "unknown", "checks": {}, "evidence": {}, "violations": [],
                              "reason": str(error)}
                violations.extend(v for v in result.get("violations", []) if v not in violations)
                step = len(history)
                evaluations.append({"step": step, "time": now(), "phase": payload.get("phase"), **result})
                if result["outcome"] == "met" and first_met is None:
                    first_met = step
                frame = {"step": step, "phase": payload.get("phase"), "url": page["url"],
                         "title": page["title"], "time": now(),
                         "elements": action_space(page.get("actions", []))[0],
                         "page": deepcopy({k: v for k, v in page.items() if k != "screenshot"})}
                try:
                    image = current.browser.call("Page.captureScreenshot", format="jpeg", quality=72,
                                                 _response_timeout=15)["data"]
                    filename = f"{len(observations):06}.jpg"
                    (folder / "screenshots" / filename).write_bytes(base64.b64decode(image, validate=True))
                    frame["screenshot_url"] = "/api/baseline/frame?" + urlencode(
                        {"run_id": run_id, "attempt_id": row["id"], "name": filename})
                except Exception as error:
                    frame["screenshot_error"] = str(error)
                observations.append(frame)
                append("observed", {"page": page, "evaluation": result, "frame": frame})
                atomic_json(folder / "evaluation.json", self.clean({"observations": evaluations,
                            "ever_met": first_met is not None, "first_met_step": first_met}))
                publish(current)
                if result["outcome"] == "environment_blocked" and payload.get("phase") != "final":
                    raise EnvironmentBlocked(result.get("reason", "Website unavailable or requires interaction"))
            else:
                publish(current)

        try:
            append("attempt_start", {"task": task, "repeat": row["repeat"]})
            def setup(browser):
                try:
                    return self.preparer(task, browser)
                except ValueError as error:
                    raise EnvironmentBlocked(str(error)) from error

            agent = self.agent_factory(task["url"], task["goal"], screenshots=False, on_event=event,
                                       should_cancel=cancelled, prepare=setup)
            while agent.state["status"] not in {"done", "blocked"}:
                reason = cancelled()
                if reason:
                    raise RunCancelled(reason)
                if len(agent.state["decisions"]) >= MAX_STEPS * 2:
                    raise RunCancelled("timeout")
                agent.command("tick")
                publish(agent)
            agent.observe("final")
            final = evaluations[-1]
            if violations:
                status = "failed"
            elif final["outcome"] == "unknown":
                status = "evaluation_unknown"
            elif final["outcome"] == "environment_blocked":
                status = "environment_blocked"
            else:
                status = "passed" if agent.state["status"] == "done" and final["outcome"] == "met" else "failed"
            with self.lock:
                row.update(status=status, reason="" if status == "passed" else "See independent evaluation")
        except RunCancelled as error:
            with self.lock:
                row.update(status=str(error),
                           reason="Stopped by user" if str(error) == "cancelled" else "Run budget exceeded")
        except EnvironmentBlocked as error:
            with self.lock:
                row.update(status="environment_blocked", reason=str(error))
        except ConstraintViolation as error:
            with self.lock:
                row.update(status="failed", reason=str(error))
        except Exception as error:
            message = str(error)
            if ("Model connection failed" in message or "Model provider returned HTTP" in message
                    or "TEXT_MODEL_API_KEY" in message):
                fatal = message
            with self.lock:
                row.update(status="timeout" if "budget" in message.lower() else "error", reason=message)
            append("error", {"error": message})
        finally:
            try:
                if agent:
                    try:
                        if not evaluations or evaluations[-1].get("phase") != "final":
                            # Read only: never replay a mutation to collect final evidence.
                            agent.observe("final")
                    except Exception as error:
                        append("final_observation_error", {"error": str(error)})
                        evaluations.append({"phase": "final", "outcome": "unknown", "error": str(error)})
                        if row["status"] == "passed":
                            row.update(status="evaluation_unknown", reason="Final observation failed")
                    publish(agent)
                final = evaluations[-1] if evaluations else {"outcome": "unknown", "checks": {}}
                evaluation = {"ever_met": first_met is not None, "first_met_step": first_met,
                              "extra_actions": row["actions"] - first_met if first_met is not None else None,
                              "early_stop": bool(agent and agent.state["status"] == "done"
                                                 and final["outcome"] == "not_met"),
                              "final": final, "violations": violations}
                with self.lock:
                    row.update(evaluation=evaluation, ended_at=now(),
                               elapsed_ms=round((time.monotonic() - started) * 1000))
                    row["reason"] = self.clean(row["reason"])
                atomic_json(folder / "evaluation.json", self.clean({**evaluation, "observations": evaluations}))
                append("attempt_end", {"result": row})
            finally:
                if agent:
                    try:
                        agent.close()
                    except Exception:
                        pass  # Never mask an artifact or execution error while releasing the owned tab.
        return fatal
