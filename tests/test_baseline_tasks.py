"""Independent baseline verifiers use DOM facts, not model decisions or paid APIs."""

import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from jev_ultrafast.baseline_tasks import evaluate_facts, inspect, prepare, task_catalog
from jev_ultrafast.browser import fingerprint


def task(number):
    return task_catalog("http://127.0.0.1:8766")[number - 1]


def article():
    return {
        "url": task(1)["url"] + "#choices",
        "article_visible": True,
        "article_heading": "A browser is a choice, not a conversation",
        "article_text": "Observe the browser. Enumerate the actions that are actually available.",
    }


def observed():
    page = {"url": task(7)["url"], "text": "Checkboxes", "actions": [], "scroll": {"y": 0}}
    page["fingerprint"] = fingerprint(page)
    return page


def test_catalog_is_versioned_and_returns_independent_tasks():
    catalog = task_catalog("http://127.0.0.1:8888")
    assert len(catalog) == len({item["id"] for item in catalog}) == 10
    assert catalog[0]["url"] == "http://127.0.0.1:8888/fixture.html?scenario=research"
    assert all(item["version"] for item in catalog)
    catalog[0]["goal"] = "changed"
    assert task(1)["goal"] != "changed"
    assert "2026 年 12 月 20 日" in task(10)["goal"]


def test_article_list_wrong_article_and_actual_body():
    facts = article()
    assert evaluate_facts(task(1), facts, [])["outcome"] == "met"
    facts["article_visible"] = False
    assert evaluate_facts(task(1), facts, [])["outcome"] == "not_met"
    facts.update(article_visible=True, article_heading="Where the milliseconds go")
    assert evaluate_facts(task(3), facts, [])["outcome"] == "not_met"
    assert evaluate_facts(task(2), article(), [{"kind": "wait"}])["violations"]
    assert evaluate_facts(task(2), article(), [])["outcome"] == "met"


def test_wikipedia_requires_english_article_body_not_heading_alone():
    facts = {
        "url": "https://en.wikipedia.org/wiki/Alan_Turing",
        "wiki_heading": "Alan Turing",
        "wiki_visible": True,
        "wiki_text": "Alan Turing was an English mathematician and computer scientist.",
    }
    assert evaluate_facts(task(4), facts, [])["outcome"] == "met"
    facts["wiki_text"] = ""
    assert evaluate_facts(task(4), facts, [])["outcome"] == "not_met"
    facts["url"] = "https://zh.wikipedia.org/wiki/Alan_Turing"
    assert evaluate_facts(task(4), facts, [])["outcome"] == "not_met"


def test_baidu_requires_submitted_query_and_no_login_click():
    facts = {"url": "https://www.baidu.com/s?wd=%E5%8D%8E%E4%B8%BA%E5%AE%98%E7%BD%91", "baidu_results": 3}
    assert evaluate_facts(task(5), facts, [])["outcome"] == "met"
    assert evaluate_facts(task(5), facts, [{"kind": "click", "action": "登录"}])["outcome"] == "not_met"
    facts["url"] = "https://www.baidu.com/"
    assert evaluate_facts(task(5), facts, [])["outcome"] == "not_met"


def test_books_second_page_requires_products():
    facts = {
        "url": "https://books.toscrape.com/catalogue/page-2.html",
        "pagination": "Page 2 of 50",
        "books_visible": 3,
    }
    assert evaluate_facts(task(6), facts, [])["outcome"] == "met"
    facts["books_visible"] = 0
    assert evaluate_facts(task(6), facts, [])["outcome"] == "not_met"


def test_checkboxes_boolean_types_and_second_toggle_are_strict():
    facts = {"url": task(7)["url"], "checkboxes": [{"checked": True, "node": 1}, {"checked": True, "node": 2}]}
    assert evaluate_facts(task(7), facts, [{"kind": "click", "node": 1}])["outcome"] == "met"
    facts["checkboxes"][0]["checked"] = "false"
    assert evaluate_facts(task(7), facts, [])["outcome"] == "not_met"
    facts["checkboxes"][0]["checked"] = True
    toggles = [{"kind": "click", "node": 2}, {"kind": "click", "node": 2}]
    assert evaluate_facts(task(7), facts, toggles)["outcome"] == "not_met"


def test_hotel_filled_but_not_submitted_fails():
    facts = {
        "url": task(8)["url"] + "#casa-flora",
        "stay_visible": True,
        "stay_heading": "Casa Flora",
        "stay_text": "LISBON A quiet courtyard",
        "travel": {"searched": True, "query": "Lisbon", "category": "Design", "free": True},
    }
    assert evaluate_facts(task(8), facts, [])["outcome"] == "met"
    facts["travel"]["searched"] = False
    assert evaluate_facts(task(8), facts, [])["outcome"] == "not_met"
    facts["travel"].update(searched=True, free=False)
    assert evaluate_facts(task(8), facts, [])["outcome"] == "not_met"


def test_loading_requires_visible_finish_and_single_start():
    facts = {"url": task(9)["url"], "loading_finish_visible": True, "loading_finish_text": "Hello World!"}
    history = [{"kind": "click", "action": "Start"}]
    assert evaluate_facts(task(9), facts, history)["outcome"] == "met"
    assert evaluate_facts(task(9), facts, history * 2)["outcome"] == "not_met"
    facts["loading_finish_visible"] = False
    assert evaluate_facts(task(9), facts, history)["outcome"] == "not_met"


def test_ctrip_results_and_all_constraints_not_just_form_values():
    facts = {
        "url": "https://flights.ctrip.com/online/list/oneway-bjs-sha?depdate=2026-12-20",
        "ctrip_controls": "北京 上海 单程 经济舱 2026-12-20",
        "flight_rows": [{"direct": True}],
    }
    assert evaluate_facts(task(10), facts, [])["outcome"] == "met"
    # Accept both observed routing conventions without trusting a URL as sufficient proof.
    facts["url"] = "https://flights.ctrip.com/online/list/oneway/bjs-sha?depdate=2026-12-20"
    assert evaluate_facts(task(10), facts, [])["outcome"] == "met"
    facts["flight_rows"] = []
    assert evaluate_facts(task(10), facts, [])["outcome"] != "met"
    facts["flight_rows"] = [{"direct": False}]
    assert evaluate_facts(task(10), facts, [])["outcome"] == "not_met"
    facts["flight_rows"] = [{"direct": True}]
    facts["ctrip_controls"] = "北京 上海 单程 经济舱 2027-12-20"
    facts["url"] = facts["url"].replace("2026-12-20", "2027-12-20")
    assert evaluate_facts(task(10), facts, [])["outcome"] == "not_met"
    assert evaluate_facts(task(10), facts, [{"kind": "click", "action": "预订"}])["violations"]


@pytest.mark.parametrize(
    ("date_query", "visible_date", "expected"),
    [
        ("2026-12-20", "2027-12-20", False),
        ("2027-12-20", "2026-12-20", False),
        ("2026-12-20", "2026-11-20", False),
        ("2026-12-20", "2026-12-20 2027-12-20", False),
        ("2026-12-20", "2026年12月20日", True),
        ("2026-12-20", "12月20日", True),
        ("2027-12-20", "12月20日", False),
    ],
)
def test_ctrip_rejects_conflicting_visible_and_submitted_dates(date_query, visible_date, expected):
    facts = {
        "url": "https://flights.ctrip.com/online/list/oneway/bjs-sha?depdate=" + date_query,
        "ctrip_controls": "北京 上海 单程 经济舱 " + visible_date,
        "flight_rows": [{"direct": True}],
    }
    result = evaluate_facts(task(10), facts, [])
    assert result["checks"]["date"] is expected
    assert (result["outcome"] == "met") is expected


@pytest.mark.parametrize("facts", [{}, {"url": None}])
def test_missing_facts_are_unknown(facts):
    assert evaluate_facts(task(1), facts, [])["outcome"] == "unknown"


def test_access_block_is_separate_from_strategy_failure():
    facts = {"url": task(10)["url"], "environment_block": "Visible CAPTCHA challenge"}
    assert evaluate_facts(task(10), facts, [])["outcome"] == "environment_blocked"


def test_independent_inspection_rejects_changed_page():
    page = observed()
    changed = deepcopy(page)
    changed["text"] = "new page"
    browser = Mock(evaluate=Mock(return_value={"page": changed, "facts": {"url": page["url"]}}))
    result = inspect(task(7), browser, page, [])
    assert result["outcome"] == "unknown"
    assert "changed" in result["evidence"]["error"]
    assert browser.evaluate.call_count == 1


def test_javascript_probe_rejects_hidden_results_and_reads_real_checkbox_booleans():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the read-only DOM probe contract")
    script = r"""
const fs = require('node:fs'), vm = require('node:vm');
const element = (text, visible, checked) => ({innerText:text, checked,
  closest:()=>null, checkVisibility:()=>visible,
  getBoundingClientRect:()=>({width:100,height:30,left:0,right:100,top:0,bottom:30})});
const finish = element('Hello World!', false);
const boxes = [element('', true, false), element('', true, true)];
const context = {
  page:{text:'Start'}, innerWidth:1120, innerHeight:780,
  location:{href:'https://the-internet.herokuapp.com/dynamic_loading/2',
    protocol:'https:',hostname:'the-internet.herokuapp.com',pathname:'/dynamic_loading/2'},
  window:{__jevFast:{ids:new WeakMap([[boxes[0],1],[boxes[1],2]])}},
  document:{
    querySelector:s=>s==='#finish h4'?finish:null,
    querySelectorAll:s=>s==='#checkboxes input[type="checkbox"]'?boxes:[]
  }
};
const source = fs.readFileSync(process.argv[1], 'utf8');
const hidden = vm.runInNewContext(source, context);
finish.checkVisibility = () => true;
const shown = vm.runInNewContext(source, context);
process.stdout.write(JSON.stringify({hidden,shown}));
"""
    probe = Path(__file__).parents[1] / "jev_ultrafast" / "baseline_probe.js"
    result = subprocess.run([node, "-e", script, str(probe)], check=True, capture_output=True, text=True)
    facts = json.loads(result.stdout)
    assert facts["hidden"]["loading_finish_visible"] is False
    assert facts["hidden"]["loading_finish_text"] == ""
    assert facts["shown"]["loading_finish_visible"] is True
    assert facts["shown"]["loading_finish_text"] == "Hello World!"
    assert facts["hidden"]["checkboxes"] == [{"checked": False, "node": 1}, {"checked": True, "node": 2}]


def test_inspection_keeps_forbidden_toggle_across_later_observations():
    page = observed()
    facts = {"url": page["url"], "checkboxes": [{"checked": False, "node": 1}, {"checked": True, "node": 2}]}
    browser = Mock(evaluate=Mock(return_value={"page": page, "facts": facts}))
    inspect(task(7), browser, page, [])
    history = [{"kind": "click", "node": 2}]
    facts["checkboxes"] = [{"checked": True, "node": 3}, {"checked": True, "node": 4}]
    assert inspect(task(7), browser, page, history)["violations"]
    assert inspect(task(7), browser, page, history)["violations"]


def test_prepare_really_opens_article_and_does_not_forge_model_history():
    browser = Mock(evaluate=Mock(side_effect=[task(2)["url"], True]))
    result = prepare(task(2), browser)
    assert result["scored_actions"] == 0
    assert "a.click()" in browser.evaluate.call_args.args[0]
    browser.evaluate.side_effect = ["https://example.org", True]
    with pytest.raises(ValueError, match="fixture"):
        prepare(task(2), browser)


@pytest.mark.parametrize("states", [[False, True], ["false", "true"], [True, True], []])
def test_prepare_checkbox_start_state_is_checked_not_mutated(states):
    browser = Mock(evaluate=Mock(return_value=states))
    if states == [False, True]:
        prepare(task(7), browser)
    else:
        with pytest.raises(ValueError, match="initial state"):
            prepare(task(7), browser)
    assert "click" not in browser.evaluate.call_args.args[0]
