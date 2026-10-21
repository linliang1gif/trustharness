# trustharness

**回答一个通过率回答不了的问题：这套测试跑出来的结论，有多可信？**

一套用例可以 100% 通过而什么都没验证到。

例如，异常用例如果只断言 HTTP 2xx，服务端返回 200 就可能被判为通过，
即使目标错误分支根本没有执行。令牌过期时，接口也可能统一返回
`HTTP 200 + {"code": 401, "message": "Access token expired"}`；
因此评分需要先检查请求是否真正进入业务逻辑。

这不是那个项目独有的问题。AI 让测试的产量变免费，让测试的可信度变昂贵。

## 不替换你的工具链

trustharness 不是执行器。你继续用 Postman、pytest、newman、k6 或任何东西，
它叠加在上面，**零迁移成本**。

```bash
pip install .                     # 在本仓库根目录安装

trustharness score --cases cases.json --results results.json
```

示意输出（数字仅用于说明格式）：

```
信任度评分  72.0 / 100    等级 B

  用例健全度         80.0  8/10 条用例无 error 级问题
  断言适配度         70.0  7/10 条用例的断言方式与接口约定匹配
  结论诚实度         70.0  需检查假通过用例
  信号纯度          68.0  需区分测试缺陷与产品缺陷

  需要注意:
    · 部分用例含 error 级问题，建议隔离后再执行。
```

执行结果里没有用例意图（`data_type` / `expected_behavior`）也没关系，会按用例 id
从用例文件补上。

## 一条命令跑完

```bash
pip install ".[run]"               # 在本仓库根目录安装执行依赖

trustharness run --spec swagger.json --base-url https://uat.example.com \
    --header "Authorization: Bearer $TOKEN"
```

接入 → 生成 → **自检** → 执行 → 分诊 → 评分。

自检发生在**执行之前**：含 error 级问题的用例不会进入执行队列——
它们要么根本跑不起来，要么结果必然误判，放进去只会污染信号。

默认只发 `GET/HEAD/OPTIONS`。本工具会被指向任意目标环境，
默认就能删数据是不可接受的；写操作需显式 `--allow-mutations`。

`--header` 带在每个请求上，只有 `auth_missing` 用例例外——它们测的就是「没令牌会不会被拒」。

```
接口错误约定  http_status  置信度 0.90  来源 spec
              326/356 个接口声明了错误响应

生成用例      129
自检拦截        0   （含 error 级问题，默认不进入执行）
实际执行       28
失败           25

  · 101 条用例涉及 DELETE/PATCH/POST/PUT 等写操作，默认不执行

信任度评分  91.2 / 100    等级 A
```

## 四件事

### 1. 执行有效性 —— 这次执行算不算数

其余三件都假设「请求真的走到了业务逻辑」。这个前提不成立，算出来的分全无意义，
所以它最先判，并且给分数**封顶**，而不是当成又一个维度去平均。

判据只看**正向用例**：一条应当成功的请求拿到鉴权错误、网关错误，或者连接失败，
就是没进去。反向用例拿到鉴权错误可能完全正确，不参与判定。
一条请求都没发过的记录（skipped、手工用例、没有 HTTP 层的 Web UI 步骤）不算证据，
既不说它进去了，也不说它没进去。

```bash
trustharness score --cases examples/token-expired/cases.json \
                   --results examples/token-expired/results.json --min-score 60
```

```
信任度评分  0.0 / 100    等级 F
请求真正到达 0.0%   封顶前 80.0

  用例健全度        100.0  ████████████████████████  32/32 条用例无 error 级问题
  断言适配度        100.0  ████████████████████████  32/32 条用例的断言方式与接口约定匹配
  结论诚实度        100.0  ████████████████████████  无假通过用例
  信号纯度           0.0  ························  0/4 条失败归类为疑似真实缺陷

  需要注意:
    · 这次执行不算数：32 条请求卡在鉴权层，没走到业务逻辑，其中 24 条还被判成了通过。先修环境再看结论。
    · 24 条被判「通过」的用例，请求根本没走到业务逻辑——它们不该计入通过率。

低于阈值 60.0，判定不通过。
```

用例写得无可挑剔（封顶前 80），但这一轮不算数。同样的用例换成环境正常的结果
（`examples/healthy/`）是 86.7 B+。

### 2. 用例自检 —— 执行前拦住生成侧缺陷

```bash
trustharness inspect --cases cases.json
```

发现「异常用例实际发送了合法请求」「标题声称缺少必填参数但请求体非空」
「断言期望值有重复项」这类问题。它们不会让用例报错，只会让结论没有意义。

### 3. 失败分诊 —— 回答「这条失败该谁看」

```bash
trustharness triage --results results.json
```

把失败归入：用例生成无效 / 断言配置错误 / 环境不可用 / 鉴权失效 / 证据不足 / **疑似真实缺陷**。
只有最后一类值得提缺陷。

核心判据是「用例意图 × 实际结果」。同样是接口返回业务错误码：
意图 `valid` 说明正常输入被拒（疑似缺陷），意图 `invalid` 说明接口正确地
拒绝了非法输入（是断言配错了）。**单看结果分不出这两者。**

意图以 `expected_behavior`（应当成功 / 应当被拒）为准，`data_type` 兜底——
`boundary` 描述的是数据形态，不表达预期。没声明预期的边界值用例被业务拒绝时，
归「证据不足」而不是「疑似缺陷」：越界值被拒是正确行为，合法边界值被拒才是缺陷。
词表与判定只有一份，在 `trustharness.intent`；自检、分诊、执行有效性、断言生成都从那里取。

生成用例时只知道 `data_type`，用 `default_behavior(data_type)` 补预期，别一律写 `success`——
声明比类型权威，「异常测试」配上 `success` 就成了应当成功。

整轮不算数时，鉴权类失败一律归环境，不再怪用例：令牌过期会让每条反向用例
看起来都像「断言写错了」。

### 4. 错误约定探测 —— 断言不该硬编码

```bash
trustharness convention --spec swagger.json --results results.json
```

接口用 `HTTP 4xx` 还是 `HTTP 200 + body.code` 表达错误？
搞错了，所有异常用例的断言都会失配。

例如，若一份 OpenAPI 文档只声明 HTTP 200，而生成器给异常用例
硬编码 `expected: [400, 422, 500]`，同一条错误规则就会让大量用例误报失败。

本包从两个源推断：spec 声明（离线）+ 历史执行快照（在线）。
实际行为优先于文档。

## 嵌进 CI

```bash
trustharness score --cases cases.json --results results.json \
    --min-score 70 --junit trust-junit.xml --summary trust-summary.md --json > trust.json
```

低于阈值退出码为 `1`，输入有误为 `2`。执行有效性封顶之后，
一轮请求没打进去的执行过不了任何像样的阈值——通过率再高也一样。

| 产物 | 给谁看 |
|---|---|
| `--junit` | GitLab「测试」标签页 / MR 挂件、Jenkins、各类 JUnit reporter |
| `--summary` | 一页 Markdown：分数、各维度、失败该找谁。可追加到 `$GITHUB_STEP_SUMMARY` 或贴进 MR |
| `--json` | 完整评分与逐条失败分诊 |

JUnit 里不照搬通过率：

| 执行结果 | JUnit |
|---|---|
| 通过，请求走到了业务逻辑 | passed |
| 通过，但请求卡在鉴权 / 网关 / 没拿到响应 | **skipped**——没有验证任何东西 |
| 失败：疑似真实缺陷、证据不足需人工复核 | failure |
| 失败：环境不可达、鉴权失效 | error——这轮结果不作数 |
| 失败：断言配置错误、用例生成无效 | skipped，标 `[测试缺陷]`——不让被测系统背锅，也不藏起来 |
| 自检拦截、没有执行结果 | skipped |

另有 `trust-gate` 套件放两道门禁：`execution-validity`（这次执行算不算数）
和 `trust-score`（是否达到阈值）。CI 该不该红看它们，不看逐条用例。

现成模板：

- `ci/gitlab-ci.yml` —— `include` 进来即可，JUnit 报告和摘要随任务保留
- `ci/github-actions.yml` —— 复制到 `.github/workflows/`，摘要写进运行页面

两份模板都有两种接法：已有执行结果用 `score`；只有 OpenAPI 和测试环境用 `run`。
模板里的每条命令都有测试对照当前 CLI 解析，参数改名时会先在本包的测试里失败。

## 作为库使用

有执行结果时用 `assess`：它按固定顺序先判执行有效性，再带着整轮上下文分诊，
最后评分并封顶——顺序本身就是要点。

```python
from trustharness import inspect_cases
from trustharness.pipeline import assess

selfcheck = inspect_cases(cases)
out = assess(selfcheck=selfcheck, rows=results)

score = out["score"]
print(score.overall, score.grade, out["execution"]["verdict"])
for verdict in out["verdicts"]:
    print(verdict["case_id"], verdict["category"], verdict["reason"])
```

只有用例时：

```python
from trustharness import inspect_cases, trust_score

score = trust_score(selfcheck_results=inspect_cases(cases))    # 静态分
```

生成 CI 产物：`trustharness.ci.junit_xml(...)`、`trustharness.ci.markdown_summary(...)`。

## 示例

`examples/` 下两套合成数据，形态照真实事故构造，同一套 32 条用例：

- `token-expired/` —— 每个请求都拿到 `HTTP 200 + {"code": 401}`
- `healthy/` —— 环境正常，混着一条真缺陷、一条断言写错、一条异常用例发了合法请求

两份 `results.json` 故意不带 `data_type`，真实导出的执行结果常常就是这样。

## 设计约束

- **零运行时依赖** —— 核心（自检/分诊/约定/评分/CI 产物）纯标准库，
  装进任何项目都不会引入冲突。执行能力是可选 extra：
  `pip install ".[run]"` 才引入 httpx 与 jsonschema
- **生成的用例要能被任何执行器跑** —— 只产出标准断言类型
  （status_code / json_path / field_exists / response_time…），
  靠 operator 表达差异，不发明私有词汇
- **可复现** —— 同样的输入必然得到同样的分数，不依赖时间、随机性或外部服务
- **可复算** —— 每个维度都是一个可数的比值，报告同时给出分子分母
- **不蒙混** —— 没测到的维度不打分，如实降级并在 caveats 写明原因；
  执行了但一条都没失败时，信号纯度不打分，而不是给满分

## 状态

Alpha。评分维度与权重仍可能调整；升级前请核对版本变更说明。

开发验证：在仓库根目录执行 `pip install ".[dev]"`，然后运行
`python -m pytest tests -q`。

## License

MIT
