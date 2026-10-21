# -*- coding: utf-8 -*-
"""CI 集成：score 走完整评测主干、JUnit XML 与摘要、模板里的命令真的能跑。

示例数据在 examples/ 下，是按真实事故的形态合成的：
  token-expired/  每个请求都拿到 HTTP 200 + {"code": 401}
  healthy/        环境正常，混着一条真缺陷、一条断言写错、一条异常用例发了合法请求
两份 results.json 故意不带 data_type——真实导出的执行结果常常就是这样。
"""
import json
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from trustharness.cli import build_parser, main

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
TEMPLATES = ROOT / "ci"


def _paths(example):
    return str(EXAMPLES / example / "cases.json"), str(EXAMPLES / example / "results.json")


def _score(tmp_path, example, *extra):
    cases, results = _paths(example)
    junit, summary = tmp_path / "junit.xml", tmp_path / "summary.md"
    code = main(["score", "--cases", cases, "--results", results,
                 "--junit", str(junit), "--summary", str(summary), *extra])
    return code, ET.parse(junit).getroot(), summary.read_text(encoding="utf-8")


def _suite(root, name):
    return {c.get("name").split(" ")[0]: c
            for s in root.findall("testsuite") if s.get("name") == name
            for c in s.findall("testcase")}


def _outcome(testcase):
    for kind in ("failure", "error", "skipped"):
        if testcase.find(kind) is not None:
            return kind
    return "passed"


# ── score 必须走完整主干 ──────────────────────────────────
def test_score_caps_a_token_expired_run(capsys):
    """以前 score 直接 分诊→评分，不判「这一轮打没打进去」：令牌过期照样拿高分。

    而 score 恰恰是 CI 里最常用的入口——带着自己执行器的结果来评估。
    """
    cases, results = _paths("token-expired")
    code = main(["score", "--cases", cases, "--results", results, "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, "没设阈值就不做门禁"
    assert payload["execution"]["verdict"] == "unusable"
    assert payload["overall"] <= payload["execution"]["validity_percent"]
    assert payload["raw_overall"] >= 60, "用例本身写得不差，是环境让这一轮不算数"
    assert {f["category"] for f in payload["failures"]} == {"auth_expired"}


def test_results_without_intent_borrow_it_from_cases():
    """结果文件里没有意图时从用例文件补。不补的话一条正向请求都认不出来，
    令牌过期的一轮会被判成「无从判断」而放过去。"""
    rows = json.loads((EXAMPLES / "token-expired" / "results.json").read_text(encoding="utf-8"))
    assert all("data_type" not in row for row in rows), "示例前提变了，这条测试就测不到东西"

    cases, results = _paths("token-expired")
    code = main(["score", "--cases", cases, "--results", results, "--min-score", "60"])
    assert code == 1


# ── JUnit：CI 页面上看到的不能还是通过率 ─────────────────
def test_token_expired_run_fails_the_gate(tmp_path):
    code, root, summary = _score(tmp_path, "token-expired", "--min-score", "60")
    assert code == 1

    gate = _suite(root, "trust-gate")
    assert _outcome(gate["execution-validity"]) == "failure"
    assert "不算数" in gate["execution-validity"].find("failure").get("message")
    assert _outcome(gate["trust-score"]) == "failure"

    cases = _suite(root, "execution")
    # 只断言状态码的正向用例「通过」了，但请求被挡在鉴权层——不能是 passed
    assert {_outcome(cases[f"ORD-{i:03d}"]) for i in range(1, 17)} == {"skipped"}
    # 断言了业务码的正向用例失败了：归环境，不算被测系统的缺陷
    assert {_outcome(cases[f"ORD-{i:03d}"]) for i in range(17, 21)} == {"error"}
    # 「没带令牌应被拒」的用例拿到 401：测到了它要测的
    assert {_outcome(cases[f"ORD-{i:03d}"]) for i in range(29, 33)} == {"passed"}

    assert "门禁未通过" in summary and "不算数" in summary


def test_healthy_run_passes_and_separates_defects_from_test_bugs(tmp_path):
    code, root, summary = _score(tmp_path, "healthy", "--min-score", "60")
    assert code == 0
    assert {k: _outcome(v) for k, v in _suite(root, "trust-gate").items()} == {
        "execution-validity": "passed", "trust-score": "passed"}

    cases = _suite(root, "execution")
    assert _outcome(cases["ORD-017"]) == "failure"
    assert cases["ORD-017"].find("failure").get("type") == "likely_defect"
    for case_id in ("ORD-018", "ORD-021"):     # 断言写错 / 异常用例发了合法请求
        assert _outcome(cases[case_id]) == "skipped"
        assert cases[case_id].find("skipped").get("message").startswith("[测试缺陷]")
    assert [k for k, v in cases.items() if _outcome(v) == "failure"] == ["ORD-017"]
    assert "门禁通过" in summary


def test_junit_counters_match_the_elements(tmp_path):
    for example in ("token-expired", "healthy"):
        _, root, _ = _score(tmp_path, example, "--min-score", "60")
        suites = root.findall("testsuite")
        for suite in suites:
            cases = suite.findall("testcase")
            assert int(suite.get("tests")) == len(cases)
            for kind, attr in (("failure", "failures"), ("error", "errors"),
                               ("skipped", "skipped")):
                assert int(suite.get(attr)) == sum(1 for c in cases if c.find(kind) is not None)
        for attr in ("tests", "failures", "errors", "skipped"):
            assert int(root.get(attr)) == sum(int(s.get(attr)) for s in suites)


def test_control_characters_do_not_break_the_xml():
    """标题、响应体原样进报告。带控制字符时整份 XML 解析失败，CI 只会说报告无效。"""
    from trustharness.ci import junit_xml
    from trustharness.pipeline import assess

    rows = [{"case_id": "C1", "status": "failed", "data_type": "valid",
             "expected_behavior": "success", "error_message": "boom\x00\x1b[31m",
             "response_snapshot": {"status_code": 500, "body": {"message": "x\x07"}},
             "assertion_details": []}]
    selfcheck = {"C1": []}
    out = assess(selfcheck=selfcheck, rows=rows)
    xml = junit_xml(score=out["score"], cases=[{"id": "C1", "title": "标题\x07"}],
                    selfcheck=selfcheck, rows=rows, verdicts=out["verdicts"],
                    execution=out["execution"])
    ET.fromstring(xml.encode("utf-8"))


def test_static_score_lists_selfcheck(tmp_path):
    junit = tmp_path / "junit.xml"
    cases, _ = _paths("healthy")
    assert main(["score", "--cases", cases, "--junit", str(junit)]) == 0
    root = ET.parse(junit).getroot()
    assert [s.get("name") for s in root.findall("testsuite")] == ["trust-gate", "selfcheck"]


# ── run 的请求头 ─────────────────────────────────────────
def test_shared_headers_skip_cases_that_must_be_rejected():
    from trustharness.pipeline import headers_for

    token = {"Authorization": "Bearer t"}
    assert headers_for({"data_type": "valid"}, token) == token
    assert headers_for({"data_type": "auth_missing", "expected_behavior": "client_error",
                        "headers": {}}, token) == {}
    # 用例自己的请求头优先
    assert headers_for({"data_type": "valid", "headers": {"Authorization": "own"}},
                       token) == {"Authorization": "own"}


def test_bad_header_is_rejected_without_echoing_it(capsys):
    cases, _ = _paths("healthy")          # 随便一个 JSON 当 spec，报错发生在执行之前
    code = main(["run", "--spec", cases, "--header", "Bearer-tokenvalue-without-colon"])
    assert code == 2
    assert "tokenvalue" not in capsys.readouterr().err


# ── 模板 ─────────────────────────────────────────────────
def _template_commands(text):
    """从模板里抠出 trustharness 命令，注释掉的另一种接法一起抠。"""
    commands, current = [], None
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        # 后面跟小写子命令才算命令（标题注释「trustharness × GitLab CI」不算）；
        # 子命令拼错照样会被下面的 parse_args 抓住
        if re.match(r"trustharness [a-z]", line):
            current = [line]
            commands.append(current)
        elif current is not None and line.startswith("--"):
            current.append(line)
        else:
            current = None
    return [" ".join(parts).split(" > ")[0] for parts in commands]


@pytest.mark.parametrize("name", ["gitlab-ci.yml", "github-actions.yml"])
def test_template_commands_use_flags_the_cli_accepts(name):
    """CLI 改了参数名，模板会悄悄过期——直到有人照着抄进流水线才发现。"""
    commands = _template_commands((TEMPLATES / name).read_text(encoding="utf-8"))
    assert {c.split()[1] for c in commands} == {"score", "run"}, commands

    parser = build_parser()
    for command in commands:
        # 变量替换成数字：它既是合法路径/URL 片段，也能过 --min-score 的 float 校验
        placeholder = re.sub(r"\$\{\{[^}]*\}\}|\$[A-Z_]+", "70", command)
        args = parser.parse_args(shlex.split(placeholder)[1:])
        assert args.junit == "trust-junit.xml" and args.summary == "trust-summary.md"
        assert args.min_score is not None, "模板不设阈值就等于没有门禁"


@pytest.mark.parametrize("name", ["gitlab-ci.yml", "github-actions.yml"])
def test_templates_are_valid_yaml(name):
    yaml = pytest.importorskip("yaml")
    assert isinstance(yaml.safe_load((TEMPLATES / name).read_text(encoding="utf-8")), dict)


def test_readme_example_output_is_what_the_cli_prints(capsys):
    """README 里「令牌过期」那段输出是本包最核心的主张。数字对不上，文档就在撒谎。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"--results examples/token-expired/results\.json --min-score 60\n```\n\n"
                      r"```\n(.*?)```", readme, flags=re.S)
    assert match, "README 里找不到令牌过期示例的输出块"

    cases, results = _paths("token-expired")
    main(["score", "--cases", cases, "--results", results, "--min-score", "60"])
    printed = capsys.readouterr().out
    normalize = lambda text: [line.rstrip() for line in text.strip().splitlines() if line.strip()]
    assert normalize(match.group(1)) == normalize(printed)


def test_gitlab_template_publishes_the_junit_it_writes():
    text = (TEMPLATES / "gitlab-ci.yml").read_text(encoding="utf-8")
    assert "junit: trust-junit.xml" in text
    assert "when: always" in text, "门禁失败时报告更要留下"
