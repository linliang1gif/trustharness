# -*- coding: utf-8 -*-
"""数据串联：从查询接口的真实返回里取标识，填进需要它的请求。

照 RESTler 的「生产者—消费者」依赖和 Schemathesis 的资源池做法：
`/finance/receivable/page` 返回的列表里有真实的 billNo、uuid，
`/finance/receivable/info` 需要 billNo——把前者产出的值喂给后者，
而不是发一个生成器编的 'AY8mbM'，然后收到「记录不存在」再算成用例失败。

只做三件事，都不碰网络：
- ValuePool.harvest   从成功响应里收集标识类字段的真实值
- fill_request        给正向用例的请求填上真实值，并记下来源
- producer_urls       为某个接口找能产出数据的查询接口

判定成功与否由调用方给出业务码约定；这里不猜。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 标识类字段：uuid / id / billNo / orderNumber / customerCode / xxxUuids ...
_EXACT_IDS = {"uuid", "id", "uuids", "ids"}
# 太泛的名字不做跨资源匹配：别的资源的 uuid 填进来一定是错的
_GENERIC = {"uuid", "id", "code", "no", "number", "uuids", "ids", "codes"}
_PAGING_KEYS = {"pageNum", "pageSize", "current", "size", "page", "limit", "offset",
                "pageNo", "pageIndex"}
_LIST_KEYS = ("list", "records", "rows", "items", "content", "data", "result")
_ACTION = re.compile(
    r"/(page|list|pageList|listAll|allList|detail|get|info|query|save|add|create|insert|update|edit|"
    r"modify|delete|remove|batchDelete|enabled|enable|disable|audit|undo|cancel|export|\w+Export|"
    r"pageGroupCount|check\w*)$", re.I)
_PRODUCER_SUFFIXES = ("/page", "/list", "/pageList", "/allList", "/listAll")
_PAGE_SUFFIX = re.compile(r"/(page|pageList)$", re.I)
_PAGING_DIALECTS = (
    ("pageNum", "pageSize"),
    ("current", "size"),
    ("pageNo", "pageSize"),
    ("pageIndex", "pageSize"),
    ("page", "limit"),
    ("offset", "limit"),
)


def is_identifier_field(name: str) -> bool:
    if not isinstance(name, str) or name in _PAGING_KEYS:
        return False
    if name.lower() in _EXACT_IDS:
        return True
    # 驼峰后缀：billNo / dealingsTypeUuid / sellOrderNumber / customerCode / shopOrderIds
    return bool(re.search(r"[a-z0-9](Uuid|Id|No|Number|Code)s?$", name)) or \
        bool(re.search(r"_(uuid|id|no|code)s?$", name, re.I))


def resource_prefix(url: str) -> str:
    """去掉动作段：/finance/receivable/info -> /finance/receivable

    路径模板里的参数段也去掉：/orders/{uuid}/craftInfo -> /orders
    """
    path = (url or "").split("?", 1)[0].rstrip("/")
    if "/{" in path:
        return path[:path.index("/{")]
    stripped = _ACTION.sub("", path)
    return stripped if stripped != path else path.rsplit("/", 1)[0]


def _singular(name: str) -> str:
    return name[:-1] if name.endswith("s") and len(name) > 2 else name


def _fk_resource(name: str) -> Optional[Tuple[str, str]]:
    """dealingsTypeUuid -> ("dealingsType", "uuid")；billNo 这类不是外键，返回 None。"""
    m = re.match(r"^([a-z][A-Za-z0-9]*?)(Uuid|Id)s?$", name)
    if not m:
        return None
    return m.group(1), m.group(2).lower()


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass
class Harvested:
    value: Any
    source: str


@dataclass
class ValuePool:
    """线程安全的真实值池：资源前缀 -> 字段 -> 首个真实值。"""
    success_codes: Tuple[Any, ...] = (200, 0, "200", "0")
    code_field: str = "code"
    _by_prefix: Dict[str, Dict[str, Harvested]] = field(default_factory=dict)
    # 每个资源的第一条完整记录（只留标量字段）：同一条记录的配套字段要一起用
    _records: Dict[str, Tuple[Dict[str, Any], str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def is_success(self, body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        if self.code_field not in body:
            return True  # 不走业务码的接口：由调用方的 HTTP 判定兜底
        return body.get(self.code_field) in self.success_codes

    def harvest(self, url: str, body: Any) -> int:
        """从一次成功响应里收集标识字段。返回新收集的字段数。"""
        if not self.is_success(body):
            return 0
        record = _first_record(body.get("data") if isinstance(body, dict) and "data" in body else body)
        if not record:
            return 0
        prefix = resource_prefix(url)
        added = 0
        with self._lock:
            if prefix not in self._records:
                self._records[prefix] = ({k: v for k, v in record.items()
                                          if v not in (None, "") and not isinstance(v, (dict, list))}, url)
            slot = self._by_prefix.setdefault(prefix, {})
            for key, value in record.items():
                if not is_identifier_field(key) or value in (None, "", [], {}):
                    continue
                if isinstance(value, (dict, list)):
                    continue
                if key not in slot:
                    slot[key] = Harvested(value, url)
                    added += 1
        return added

    def record_for(self, url: str) -> Optional[Tuple[Dict[str, Any], str]]:
        with self._lock:
            return self._records.get(resource_prefix(url))

    def prefixes(self) -> List[str]:
        with self._lock:
            return list(self._by_prefix)

    def has(self, prefix: str) -> bool:
        with self._lock:
            return bool(self._by_prefix.get(prefix))

    def lookup(self, url: str, name: str) -> Optional[Harvested]:
        prefix = resource_prefix(url)
        want = _singular(name) if name.endswith(("Uuids", "Ids", "Nos", "Codes", "uuids", "ids")) else name
        with self._lock:
            own = self._by_prefix.get(prefix, {})
            # 1 同资源、同名字段
            if want in own:
                return own[want]
            # 2 外键：dealingsTypeUuid -> 找以 dealingsType 结尾的资源的 uuid
            fk = _fk_resource(want)
            if fk:
                resource, key = fk
                for other, fields in self._by_prefix.items():
                    if _normalize(other.rsplit("/", 1)[-1]).endswith(_normalize(resource)) and key in fields:
                        return fields[key]
            # 3 足够具体的名字（sellOrderNumber）可以跨资源取
            if want.lower() not in _GENERIC:
                for fields in self._by_prefix.values():
                    if want in fields:
                        return fields[want]
        return None


def _first_record(data: Any) -> Optional[Dict[str, Any]]:
    """分页包装 {list:[...]}、裸数组、详情对象都认；取第一条记录。"""
    if isinstance(data, list):
        first = next((x for x in data if isinstance(x, dict)), None)
        return first
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            inner = data.get(key)
            if isinstance(inner, list):
                return _first_record(inner)
            if isinstance(inner, dict) and key in ("data", "result"):
                return _first_record(inner)
        # 详情对象本身
        if any(is_identifier_field(k) for k in data):
            return data
    return None


def wants_real_data(data_type: Optional[str], expected_behavior: Optional[str]) -> bool:
    """只给预期成功的正常流程与边界用例填真实标识。

    异常用例的标识是故意的（不存在的 id、缺字段、类型错），填了就不是那条用例了。
    边界用例测的是其他字段的长度/取值边界：标识也造成「长度 1 的随机串」，
    否则接口可能只返回「数据不存在」，无法测试目标字段的边界。
    """
    kind = data_type or "valid"
    if kind == "boundary":
        # L2 的「边界值: id=-1」没有声明预期，测的正是标识本身的边界，不能替换
        return expected_behavior == "success"
    return (expected_behavior or "success") == "success" and kind == "valid"


def _coerce(current: Any, value: Any) -> Tuple[bool, Any]:
    """真实值能不能放进这个字段：按生成值的类型判断。

    例如整数标识字段不能直接填入非十进制字符串。
    """
    if isinstance(current, bool):
        return (isinstance(value, bool), value)
    if isinstance(current, (int, float)):
        if isinstance(value, bool):
            return (False, value)
        if isinstance(value, (int, float)):
            return (True, value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return (True, int(value))
        return (False, value)
    if isinstance(current, str):
        return (True, value if isinstance(value, str) else str(value))
    return (True, value)


@dataclass
class FillResult:
    body: Any
    params: Dict[str, Any]
    filled: Dict[str, Dict[str, Any]]
    unresolved: List[str]

    def provenance(self) -> Dict[str, Any]:
        return {"filled": self.filled, "unresolved": self.unresolved}


@dataclass
class QueryDefaultsResult:
    """正向查询请求的确定性默认值，以及可写进执行证据的变更记录。"""
    body: Any
    params: Dict[str, Any]
    applied: Dict[str, Any]

    def provenance(self) -> Dict[str, Any]:
        return {"defaults": self.applied}


def apply_query_defaults(method: str, url: str, body: Any,
                         params: Optional[Dict[str, Any]]) -> QueryDefaultsResult:
    """为正向分页查询补最小分页参数，不覆盖用例明确给出的值。

    GET 放 query string；其他方法优先沿用用例已经选择的位置，没有线索时放 body。
    只识别明确的分页动作，普通 list/detail 请求不做任何猜测。
    """
    new_body = dict(body) if isinstance(body, dict) else body
    new_params = dict(params or {})
    path = (url or "").split("?", 1)[0].rstrip("/")
    if not _PAGE_SUFFIX.search(path):
        return QueryDefaultsResult(new_body, new_params, {})

    body_keys = set(new_body) if isinstance(new_body, dict) else set()
    param_keys = set(new_params)
    existing = body_keys | param_keys
    # pageSize belongs to several dialects, so choose by the page-position key
    # first.  Otherwise {current: 3, pageSize: 50} would incorrectly gain pageNum.
    dialect = next((pair for pair in _PAGING_DIALECTS if pair[0] in existing), None)
    if dialect is None:
        dialect = next((pair for pair in _PAGING_DIALECTS if pair[1] in existing),
                       _PAGING_DIALECTS[0])

    if str(method or "GET").upper() == "GET":
        target, where = new_params, "params"
    elif param_keys.intersection(_PAGING_KEYS) and not body_keys.intersection(_PAGING_KEYS):
        target, where = new_params, "params"
    else:
        if not isinstance(new_body, dict):
            new_body = {}
        target, where = new_body, "body"

    applied: Dict[str, Any] = {}
    for key in dialect:
        if key not in target:
            value = 0 if key == "offset" else (10 if key in {"pageSize", "size", "limit"} else 1)
            target[key] = value
            applied[f"{where}.{key}"] = value
    return QueryDefaultsResult(new_body, new_params, applied)


def fill_request(url: str, body: Any, params: Optional[Dict[str, Any]], pool: ValuePool,
                 siblings: bool = False, keep: Iterable[str] = ()) -> FillResult:
    """把请求里的标识字段换成池里的真实值；换不了的记为 unresolved。

    siblings=True 时，同资源记录里的配套字段也一起填（只用于正常流程用例）：
    例如请求同时需要记录编号和版本号时，两者必须来自同一条记录。
    """
    filled: Dict[str, Dict[str, Any]] = {}
    unresolved: List[str] = []
    own = pool.record_for(url)
    keep = set(keep)        # 用例正在测的字段（「边界值: id=0」里的 id），保持原值

    def _fill(container: Dict[str, Any], where: str) -> Dict[str, Any]:
        out = dict(container)
        used_own_record = False
        for key, current in container.items():
            if key in keep or not is_identifier_field(key) or isinstance(current, dict):
                continue
            hit = pool.lookup(url, key)
            if hit is None:
                unresolved.append(f"{where}.{key}")
                continue
            sample = current[0] if isinstance(current, list) and current else current
            ok, value = _coerce(sample, hit.value)
            if not ok:
                unresolved.append(f"{where}.{key}")
                continue
            out[key] = [value] if isinstance(current, list) else value
            filled[f"{where}.{key}"] = {"value": value, "from": hit.source}
            used_own_record = used_own_record or (own is not None and hit.source == own[1])
        if siblings and used_own_record:
            record, source = own
            for key, current in container.items():
                if key in keep or key in _PAGING_KEYS or is_identifier_field(key) or key not in record:
                    continue
                if isinstance(current, (dict, list)):
                    continue
                ok, value = _coerce(current, record[key])
                if ok:
                    out[key] = value
                    filled[f"{where}.{key}"] = {"value": value, "from": source}
        return out

    new_body = _fill(body, "body") if isinstance(body, dict) else body
    new_params = _fill(params or {}, "params") if params else dict(params or {})
    return FillResult(new_body, new_params, filled, unresolved)


_FIELD_UNDER_TEST = re.compile(r"(?:边界值|超长字符串|空值|类型错误|必填缺失|非法枚举)\s*[:：]\s*(\w+)")


def render_path(template: str, values: Dict[str, Any]) -> str:
    out = template
    for name, value in (values or {}).items():
        out = out.replace("{" + name + "}", str(value))
    return out


def fields_under_test(title: Optional[str]) -> List[str]:
    """从 L2 变异用例标题里取出被测字段：「边界值: id=0」→ ["id"]。"""
    return _FIELD_UNDER_TEST.findall(title or "")


def needs_data(body: Any, params: Optional[Dict[str, Any]]) -> bool:
    containers = [c for c in (body, params) if isinstance(c, dict)]
    return any(is_identifier_field(k) for c in containers for k in c)


def producer_urls(consumer_url: str, known_urls: Iterable[str]) -> List[str]:
    """同资源下的查询接口（page 优先），以及外键指向的资源的查询接口由调用方按需再找。"""
    prefix = resource_prefix(consumer_url)
    known = set(known_urls)
    ordered = [prefix + s for s in _PRODUCER_SUFFIXES]
    return [u for u in ordered if u in known and u != consumer_url.split("?", 1)[0]]


def fk_producer_urls(body: Any, params: Optional[Dict[str, Any]], known_urls: Iterable[str]) -> List[str]:
    """dealingsTypeUuid 这类外键字段指向的资源的查询接口。"""
    known = list(known_urls)
    out: List[str] = []
    for container in (body, params):
        if not isinstance(container, dict):
            continue
        for key in container:
            fk = _fk_resource(_singular(key) if key.endswith("s") else key)
            if not fk:
                continue
            target = _normalize(fk[0])
            for url in known:
                if not url.endswith(_PRODUCER_SUFFIXES):
                    continue
                if _normalize(resource_prefix(url).rsplit("/", 1)[-1]).endswith(target) and url not in out:
                    out.append(url)
    return out


def producer_body(template: Any, url: Optional[str] = None) -> Any:
    """准备数据用的查询请求：只留分页字段。

    生成器造的「正常」分页请求会把每个可选过滤条件都填上随机串，
    查出来一定是空列表——拿它当数据来源等于没有来源。
    """
    if not isinstance(template, dict):
        return {"pageNum": 1, "pageSize": 10}
    existing = set(template)
    dialect = next((pair for pair in _PAGING_DIALECTS if pair[0] in existing), None)
    if dialect is None:
        dialect = next((pair for pair in _PAGING_DIALECTS if pair[1] in existing), None)
    minimal: Dict[str, Any] = {}
    if dialect:
        for key in dialect:
            minimal[key] = 0 if key == "offset" else (10 if key in {"pageSize", "size", "limit"} else 1)
    elif url and _PAGE_SUFFIX.search(url.split("?", 1)[0].rstrip("/")):
        minimal.update(pageNum=1, pageSize=10)
    return minimal
