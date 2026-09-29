"""Versioned baseline tasks and read-only outcome checks, never agent guidance."""

import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from .browser import READ_STATE, fingerprint

VERSION = "2026-09-28.1"
ARTICLE_GOAL = "Open the article about using finite choices to control browser agents."
TASKS = [
    {
        "id": "01_article_open",
        "name": "从列表打开文章",
        "url": "/fixture.html?scenario=research",
        "goal": ARTICLE_GOAL,
        "capability": "语义匹配、终止判断",
    },
    {
        "id": "02_article_already_open",
        "name": "已完成时直接停止",
        "url": "/fixture.html?scenario=research",
        "goal": ARTICLE_GOAL,
        "capability": "初始完成、零动作",
        "setup": "choices",
    },
    {
        "id": "03_article_recover",
        "name": "从错误文章恢复",
        "url": "/fixture.html?scenario=research",
        "goal": ARTICLE_GOAL,
        "capability": "错误状态识别、恢复导航",
        "setup": "latency",
    },
    {
        "id": "04_wikipedia",
        "name": "查找英文维基词条",
        "url": "https://www.wikipedia.org/",
        "goal": "Find and open the English Wikipedia article about Alan Turing.",
        "capability": "文本生成、搜索提交、详情识别",
    },
    {
        "id": "05_baidu",
        "name": "百度搜索后停止",
        "url": "https://www.baidu.com/",
        "goal": "在百度搜索“华为官网”，停留在搜索结果页，不登录、不打开任何结果。",
        "capability": "中文搜索、终点约束",
    },
    {
        "id": "06_pagination",
        "name": "滚动并进入第二页",
        "url": "https://books.toscrape.com/",
        "goal": "Go to page 2 of the book catalogue and leave that catalogue page open.",
        "capability": "滚动、新候选发现、翻页",
    },
    {
        "id": "07_checkboxes",
        "name": "修改并保持勾选状态",
        "url": "https://the-internet.herokuapp.com/checkboxes",
        "goal": "Check the first checkbox. Leave the second checkbox checked without changing it.",
        "capability": "布尔状态、避免重复操作",
    },
    {
        "id": "08_travel_filters",
        "name": "筛选住宿并打开详情",
        "url": "/fixture.html",
        "goal": (
            "Search for stays in Lisbon, set the category to Design, enable Free cancellation, then open Casa Flora."
        ),
        "capability": "提交、下拉选择、多约束保持",
    },
    {
        "id": "09_dynamic_loading",
        "name": "等待异步结果",
        "url": "https://the-internet.herokuapp.com/dynamic_loading/2",
        "goal": "Start loading and stop when “Hello World!” is visible.",
        "capability": "等待、加载完成判断",
    },
    {
        "id": "10_ctrip",
        "name": "携程多条件机票查询",
        "url": "https://flights.ctrip.com/online/channel/domestic",
        "goal": (
            "在携程查询 2026 年 12 月 20 日从北京到上海的单程经济舱直飞航班。"
            "看到符合条件的航班列表后停止，不登录、不选购或预订航班。"
        ),
        "capability": "自动补全、日期、多条件、禁止预订",
        "date": "2026-12-20",
    },
]
PROBE = Path(__file__).with_name("baseline_probe.js").read_text(encoding="utf-8")


def task_catalog(origin):
    """Resolve only the built-in fixture URLs; callers cannot supply executable setup."""
    return [{**deepcopy(task), "version": VERSION, "url": urljoin(origin, task["url"])} for task in TASKS]


def prepare(task, browser):
    """Deterministic unscored initial state, separate from the model policy."""
    article = task.get("setup")
    if article:
        if article not in {"choices", "latency"}:
            raise ValueError("Unknown baseline article setup")
        actual = browser.evaluate("location.href")
        expected = urlsplit(task["url"])
        current = urlsplit(actual or "")
        if (current.netloc, current.path) != (expected.netloc, expected.path):
            raise ValueError("Local fixture did not load for baseline setup")
        result = browser.evaluate(
            "(() => { const a=document.querySelector('[data-article=\"" + article + "\"]');"
            " if(!a) return false; a.click(); return !!document.querySelector('.article-content'); })()"
        )
        if not result:
            raise ValueError("Article setup was not confirmed")
    if task["id"] == "07_checkboxes":
        checked = browser.evaluate(
            "[...document.querySelectorAll('#checkboxes input[type=checkbox]')].map(e=>e.checked)"
        )
        if checked != [False, True] or any(type(value) is not bool for value in checked):
            raise ValueError("Checkbox initial state is not [false, true]")
    return {"setup": article, "scored_actions": 0}


def _same_site(url, host):
    actual = urlsplit(url).hostname or ""
    return actual == host or actual.endswith("." + host)


def evaluate_facts(task, facts, history, violations=None):
    """Pure verifier for independently sampled DOM facts; also used by offline tests."""
    violations = list(violations or [])
    checks = {}
    evidence = {"url": facts.get("url"), "facts": facts}
    if not facts or not isinstance(facts.get("url"), str):
        return {"outcome": "unknown", "checks": {}, "evidence": evidence, "violations": violations}
    if facts.get("environment_block"):
        return {"outcome": "environment_blocked", "checks": {}, "evidence": evidence, "violations": violations}
    ident, url = task["id"], facts["url"]
    unknown = False
    if ident.startswith(("01_", "02_", "03_")):
        checks = {
            "fixture_origin": urlsplit(url).netloc == urlsplit(task["url"]).netloc,
            "article_body_visible": facts.get("article_visible") is True,
            "correct_heading": facts.get("article_heading") == "A browser is a choice, not a conversation",
            "actual_body": "Enumerate the actions that are actually available" in facts.get("article_text", ""),
        }
        if ident.startswith("02_") and history:
            violations.append("初始任务已完成，但执行了浏览器动作")
    elif ident == "04_wikipedia":
        checks = {
            "english_article": (
                urlsplit(url).hostname == "en.wikipedia.org" and urlsplit(url).path.rstrip("/") == "/wiki/Alan_Turing"
            ),
            "heading": facts.get("wiki_heading") == "Alan Turing",
            "body_visible": facts.get("wiki_visible") is True,
            "biographical_body": bool(re.search(r"mathematician|computer scientist", facts.get("wiki_text", ""), re.I)),
        }
    elif ident == "05_baidu":
        query = parse_qs(urlsplit(url).query).get("wd", [""])[0]
        checks = {
            "baidu_results_url": _same_site(url, "baidu.com") and urlsplit(url).path == "/s",
            "submitted_query": query.strip() == "华为官网",
            "visible_results": facts.get("baidu_results", 0) > 0,
        }
        if history and not _same_site(url, "baidu.com"):
            violations.append("离开百度搜索结果页")
        if any(
            h.get("kind") == "click" and re.search(r"登录|登陆|log\s*in", h.get("action", ""), re.I) for h in history
        ):
            violations.append("点击了登录入口")
    elif ident == "06_pagination":
        checks = {
            "catalogue_site": _same_site(url, "books.toscrape.com"),
            "page_two": bool(re.search(r"Page\s+2\s+of\s+\d+", facts.get("pagination", ""), re.I)),
            "visible_books": facts.get("books_visible", 0) > 0,
        }
    elif ident == "07_checkboxes":
        boxes = facts.get("checkboxes", [])
        unknown = len(boxes) != 2
        checks = {
            "expected_site": _same_site(url, "the-internet.herokuapp.com") and urlsplit(url).path == "/checkboxes",
            "first_checked": len(boxes) == 2 and boxes[0].get("checked") is True,
            "second_checked": len(boxes) == 2 and boxes[1].get("checked") is True,
        }
        second = boxes[1].get("node") if len(boxes) == 2 else None
        if second is not None and any(h.get("kind") == "click" and h.get("node") == second for h in history):
            violations.append("操作了要求保持不变的第二个复选框")
    elif ident == "08_travel_filters":
        travel = facts.get("travel") or {}
        unknown = not travel
        checks = {
            "fixture_origin": urlsplit(url).netloc == urlsplit(task["url"]).netloc,
            "search_submitted": travel.get("searched") is True,
            "destination": str(travel.get("query", "")).strip().lower() == "lisbon",
            "design_category": travel.get("category") == "Design",
            "free_cancellation": travel.get("free") is True,
            "detail_visible": facts.get("stay_visible") is True,
            "correct_detail": facts.get("stay_heading") == "Casa Flora" and "LISBON" in facts.get("stay_text", ""),
            "detail_body": "A quiet courtyard" in facts.get("stay_text", ""),
        }
    elif ident == "09_dynamic_loading":
        checks = {
            "expected_site": _same_site(url, "the-internet.herokuapp.com")
            and urlsplit(url).path == "/dynamic_loading/2",
            "started": any(h.get("kind") == "click" and h.get("action", "").strip() == "Start" for h in history),
            "finish_visible": facts.get("loading_finish_visible") is True,
            "finish_text": facts.get("loading_finish_text", "").strip() == "Hello World!",
        }
        if sum(h.get("kind") == "click" and h.get("action", "").strip() == "Start" for h in history) > 1:
            violations.append("重复点击了加载按钮")
    elif ident == "10_ctrip":
        controls = facts.get("ctrip_controls", "")
        rows = facts.get("flight_rows", [])
        parsed = urlsplit(url)
        expected_route = bool(re.search(r"/oneway[/-](?:bjs|bjd|pek)-sha(?:/|$)", parsed.path, re.I))
        date_query = parse_qs(parsed.query)
        date_values = [
            v for key, values in date_query.items() if key.lower() in {"depdate", "date", "departdate"} for v in values
        ]
        visible_dates = {
            tuple(map(int, match))
            for match in re.findall(r"(?<!\d)(20\d{2})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})(?!\d)", controls)
        }
        expected_date = (2026, 12, 20)
        date_conflict = bool(visible_dates - {expected_date}) or any(value != "2026-12-20" for value in date_values)
        date_matches = expected_date in visible_dates or (
            "2026-12-20" in date_values and bool(re.search(r"(?<!\d)12\s*[月/.-]\s*20(?!\d)", controls))
        )
        direct_control = facts.get("ctrip_direct_selected") is True
        row_direct = bool(rows) and all(row.get("direct") is True for row in rows)
        checks = {
            "ctrip_site": _same_site(url, "ctrip.com"),
            "beijing_to_shanghai": expected_route and "北京" in controls and "上海" in controls,
            "date": date_matches and not date_conflict,
            "one_way": expected_route and "单程" in controls,
            "economy": "经济舱" in controls,
            "visible_flights": len(rows) > 0,
            "direct_flights": direct_control or row_direct,
        }
        unknown = not rows or not checks["economy"] or not checks["beijing_to_shanghai"]
        for h in history:
            if h.get("kind") == "click" and re.search(
                r"登录|登陆|预订|订票|选购|立即购买|支付|选择航班", h.get("action", "")
            ):
                violations.append("点击了禁止的登录或购票控件")
        if re.search(r"/online/booking(?:/|$)|/order(?:/|$)|passport\.", url, re.I):
            violations.append("进入登录或预订页面")
    else:
        unknown = True
    violations = list(dict.fromkeys(violations))
    outcome = "met" if checks and all(checks.values()) and not violations else "unknown" if unknown else "not_met"
    return {"outcome": outcome, "checks": checks, "evidence": evidence, "violations": violations}


def inspect(task, browser, page, history):
    """Sample facts atomically and reject a different page than the agent observed."""
    cache = getattr(browser, "_baseline_evidence", None)
    if not isinstance(cache, dict):
        cache = {"processed": 0, "violations": [], "result_nodes": [], "second_node": None}
        browser._baseline_evidence = cache
    expression = "(() => { const page=" + READ_STATE + "; const facts=" + PROBE + "; return {page,facts}; })()"
    try:
        sample = browser.evaluate(expression)
        if not isinstance(sample, dict) or not sample.get("page") or not isinstance(sample.get("facts"), dict):
            raise ValueError("No independent DOM facts")
        if fingerprint(sample["page"]) != page.get("fingerprint"):
            raise ValueError("Page changed during independent evaluation")
    except Exception as error:
        return {
            "outcome": "unknown",
            "checks": {},
            "evidence": {"error": str(error)},
            "violations": cache["violations"][:],
        }
    facts = sample["facts"]
    for action in history[cache["processed"] :]:
        if action.get("kind") != "click":
            continue
        if task["id"] == "05_baidu" and action.get("node") in cache["result_nodes"]:
            cache["violations"].append("打开了百度搜索结果")
        if (
            task["id"] == "07_checkboxes"
            and cache["second_node"] is not None
            and action.get("node") == cache["second_node"]
        ):
            cache["violations"].append("操作了要求保持不变的第二个复选框")
    cache["processed"] = len(history)
    cache["result_nodes"] = facts.get("baidu_result_nodes", [])
    boxes = facts.get("checkboxes", [])
    cache["second_node"] = boxes[1].get("node") if len(boxes) == 2 else cache["second_node"]
    result = evaluate_facts(task, facts, history, cache["violations"])
    cache["violations"] = result["violations"]
    return result
