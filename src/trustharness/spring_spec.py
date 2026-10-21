# -*- coding: utf-8 -*-
"""从 Spring MVC 的 Java 源码推导 OpenAPI 3 规范。

为什么需要：很多项目导出的接口文档（YApi 之类）只有路径没有参数，生成器只能瞎编请求体，
用例一大半是「名不副实」的。而后端源码里其实写得清清楚楚：

    @RequestMapping(Constant.FINANCE + "/receivable")      类级前缀（可引用常量）
    @PreAuthorize("hasAuthority('finance:otherReceive:page')")   权限码
    @PostMapping("/page")                                        方法与路径
    public X page(@RequestBody @Validated XxxPageRequest request) 请求体类
    class XxxPageRequest extends BasePageRequest {               继承的字段也算
        /** 应收单号 */ @NotBlank private String billNo;           必填、类型、说明

不引入 Java 解析器依赖，用正则 + 括号配对处理常见写法；认不出来的地方宁可少写也不瞎写
（字段类型认不出就不给 type，而不是猜成 string）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REQUIRED_ANNOTATIONS = {"NotNull", "NotBlank", "NotEmpty"}
_MAPPING_METHODS = {"GetMapping": "get", "PostMapping": "post", "PutMapping": "put",
                    "DeleteMapping": "delete", "PatchMapping": "patch"}
_SKIP_PARAM_TYPES = {"HttpServletRequest", "HttpServletResponse", "HttpSession", "Principal",
                     "Authentication", "BindingResult", "Model", "ModelMap", "Locale"}

_INTEGER_TYPES = {"int": "int32", "Integer": "int32", "short": "int32", "Short": "int32",
                  "byte": "int32", "Byte": "int32", "long": "int64", "Long": "int64",
                  "BigInteger": "int64"}
_NUMBER_TYPES = {"double", "Double", "float", "Float", "BigDecimal", "Number"}
_BOOLEAN_TYPES = {"boolean", "Boolean"}
_STRING_TYPES = {"String", "char", "Character", "CharSequence", "UUID"}
_DATETIME_TYPES = {"Date", "LocalDateTime", "ZonedDateTime", "OffsetDateTime", "Timestamp", "Instant"}
_DATE_TYPES = {"LocalDate"}
_COLLECTION_TYPES = {"List", "Set", "Collection", "ArrayList", "LinkedList", "HashSet", "LinkedHashSet"}
_MAP_TYPES = {"Map", "HashMap", "LinkedHashMap", "TreeMap", "JSONObject"}

_BLOCK_COMMENT = re.compile(r"/\*(?!\*)[\s\S]*?\*/")
_LINE_COMMENT = re.compile(r"(?<![:\"])//[^\n]*")
# 常量名不一定全大写；只认全大写时可能丢失控制器前缀。
_STRING_CONST = re.compile(r"\b(?:public\s+|static\s+|final\s+)*String\s+([A-Za-z_]\w*)\s*=\s*([^;]+);")
_CLASS_DECL = re.compile(
    r"\b(class|interface|enum)\s+(\w+)(?:\s*<[^{]*?>)?(?:\s+extends\s+([\w.]+)(?:\s*<[^{]*?>)?)?[^{]*\{")
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT = re.compile(r"^\s*import\s+([\w.]+)\s*;", re.M)
_ANNOTATION = re.compile(r"@(\w+)(\s*\()?")


@dataclass
class JavaClass:
    name: str
    package: str
    kind: str
    extends: str | None
    body: str
    imports: dict[str, str]
    path: Path
    in_scope: bool = True   # False：只来自 classpath（公共模块），不导出其中的控制器


@dataclass
class Operation:
    method: str
    path: str
    controller: str
    handler: str
    summary: str = ""
    permission: str | None = None
    body_type: str | None = None
    body_validated: bool = True
    query_params: list[dict[str, Any]] = field(default_factory=list)
    path_params: list[str] = field(default_factory=list)
    multipart: bool = False
    file_response: bool = False   # 处理方法直接往 HttpServletResponse 写文件（导出/下载）
    writes: bool = False          # 处理方法体里调用了写库方法（update/save/delete…）
    return_type: str | None = None  # 处理方法的返回类型，如 BaseResponse<ListVo<List<X>>>


def _strip_comments_keep_javadoc(text: str) -> str:
    text = _BLOCK_COMMENT.sub("", text)
    return _LINE_COMMENT.sub("", text)


def _match_paren(text: str, open_idx: int, open_ch: str = "(", close_ch: str = ")") -> int:
    """返回与 text[open_idx] 配对的右括号下标；跳过字符串字面量。"""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


class SpringProject:
    def __init__(self, root: str | Path, classpath: list[str | Path] | None = None):
        """root 是要导出接口的服务；classpath 是它依赖的公共模块源码目录。

        公共模块中的分页基类也需要纳入扫描，否则分页请求可能丢失字段。
        """
        self.root = Path(root)
        self.classpath = [Path(p) for p in (classpath or [])]
        self.classes: dict[str, list[JavaClass]] = {}
        self.constants: dict[str, str] = {}
        self._raw_constants: dict[str, tuple[str, str]] = {}
        self.missing_parents: dict[str, set[str]] = {}
        self.unresolved_controllers: list[str] = []
        self._load()

    # ---------- 加载 ----------
    def _java_files(self):
        seen: set[Path] = set()
        for base, in_scope in [(self.root, True)] + [(p, False) for p in self.classpath]:
            for path in base.rglob("*.java"):
                resolved = path.resolve()
                if resolved in seen or any(part in {"target", "build", "test"} for part in path.parts):
                    continue
                seen.add(resolved)
                yield path, in_scope

    def _load(self) -> None:
        for path, in_scope in self._java_files():
            try:
                text = path.read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            pkg = (_PACKAGE.search(text) or [None, ""])[1]
            imports = {imp.rsplit(".", 1)[-1]: imp for imp in _IMPORT.findall(text)}
            clean = _strip_comments_keep_javadoc(text)
            m = _CLASS_DECL.search(clean)
            if not m:
                continue
            start = clean.index("{", m.end() - 1)
            end = _match_paren(clean, start, "{", "}")
            cls = JavaClass(name=m.group(2), package=pkg, kind=m.group(1),
                            extends=(m.group(3) or "").rsplit(".", 1)[-1] or None,
                            body=clean[start + 1:end], imports=imports, path=path, in_scope=in_scope)
            self.classes.setdefault(cls.name, []).append(cls)
            self._register_nested(cls)
            for const, expr in _STRING_CONST.findall(clean):
                self._raw_constants[f"{cls.name}.{const}"] = (cls.name, expr.strip())
        for key in self._raw_constants:
            self._resolve_constant(key, set())

    def _register_nested(self, outer: JavaClass) -> None:
        """登记类体里的嵌套类（public static class ProductPriceRequest …）。

        嵌套类不登记时，列表元素类型可能无法识别，导致请求体类型错误。
        """
        for header, body in _members(outer.body):
            code = re.sub(r"/\*\*[\s\S]*?\*/", " ", header)
            m = re.search(r"\b(class|interface|enum)\s+(\w+)(?:\s*<[^>]*>)?(?:\s+extends\s+([\w.]+))?", code)
            if not m:
                continue
            nested = JavaClass(name=m.group(2), package=outer.package, kind=m.group(1),
                               extends=(m.group(3) or "").rsplit(".", 1)[-1] or None,
                               body=body, imports=outer.imports, path=outer.path, in_scope=outer.in_scope)
            self.classes.setdefault(nested.name, []).append(nested)
            self._register_nested(nested)

    def _resolve_constant(self, key: str, seen: set[str]) -> str | None:
        if key in self.constants:
            return self.constants[key]
        if key in seen or key not in self._raw_constants:
            return None
        seen.add(key)
        owner, expr = self._raw_constants[key]
        value = self.eval_string_expr(expr, owner, seen)
        if value is not None:
            self.constants[key] = value
        return value

    def eval_string_expr(self, expr: str, owner: str | None = None,
                         seen: set[str] | None = None) -> str | None:
        """求值 `Constant.FINANCE + "/x"` 这类拼接；有认不出的部分返回 None。"""
        seen = seen if seen is not None else set()
        parts, buf, in_str, i = [], "", False, 0
        while i < len(expr):
            ch = expr[i]
            if ch == '"':
                in_str = not in_str
            if ch == "+" and not in_str:
                parts.append(buf.strip())
                buf = ""
            else:
                buf += ch
                if in_str and ch == "\\" and i + 1 < len(expr):
                    buf += expr[i + 1]
                    i += 1
            i += 1
        parts.append(buf.strip())
        out = []
        for part in parts:
            if len(part) >= 2 and part[0] == part[-1] == '"':
                out.append(part[1:-1])
                continue
            name = part.split(".")[-2:] if "." in part else [owner or "", part]
            key = ".".join(name)
            value = self._resolve_constant(key, seen)
            if value is None and "." not in part:
                # 接口里的常量可能被实现类直接引用：按名字全局唯一匹配
                hits = [k for k in self._raw_constants if k.endswith("." + part)]
                if len(hits) == 1:
                    value = self._resolve_constant(hits[0], seen)
            if value is None:
                return None
            out.append(value)
        return "".join(out)

    def find_class(self, name: str, context: JavaClass | None = None) -> JavaClass | None:
        candidates = self.classes.get(name) or []
        if len(candidates) <= 1 or context is None:
            return candidates[0] if candidates else None
        fqcn = context.imports.get(name)
        for c in candidates:
            if fqcn and f"{c.package}.{c.name}" == fqcn:
                return c
        for c in candidates:
            if c.package == context.package:
                return c
        return candidates[0]

    # ---------- 注解 ----------
    @staticmethod
    def _annotations_before(text: str) -> list[tuple[str, str]]:
        """解析一段文本里依次出现的注解，返回 [(名字, 括号内参数)]。"""
        result, i = [], 0
        while True:
            m = _ANNOTATION.search(text, i)
            if not m:
                return result
            args = ""
            if m.group(2):
                close = _match_paren(text, m.end() - 1)
                args = text[m.end():close]
                i = close + 1
            else:
                i = m.end()
            result.append((m.group(1), args))

    def _mapping_paths(self, args: str, owner: str) -> list[str]:
        args = args.strip()
        if not args:
            return [""]
        named = re.search(r"\b(?:value|path)\s*=\s*", args)
        if named:
            expr = args[named.end():]
        elif re.match(r"^\w+\s*=", args):
            return [""]
        else:
            expr = args
        if expr.startswith("{"):
            inner = expr[1:_match_paren(expr, 0, "{", "}")]
            items = [s for s in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", inner) if s.strip()]
        else:
            items = [re.split(r",\s*\w+\s*=", expr, maxsplit=1)[0]]
        paths = []
        for item in items:
            value = self.eval_string_expr(item.strip(), owner)
            if value is not None:
                paths.append(value)
        return paths

    @staticmethod
    def _request_methods(args: str) -> list[str]:
        return [m.lower() for m in re.findall(r"RequestMethod\.(\w+)", args)]

    # ---------- 控制器 ----------
    def operations(self) -> list[Operation]:
        ops: list[Operation] = []
        for group in self.classes.values():
            for cls in group:
                if cls.kind != "class" or not cls.in_scope:
                    continue
                header = self._class_header(cls)
                if "RestController" not in header and "Controller" not in re.findall(r"@(\w+)", header):
                    continue
                prefixes, class_methods = [""], []
                unresolved = False
                for name, args in self._annotations_before(header):
                    if name == "RequestMapping":
                        prefixes = self._mapping_paths(args, cls.name)
                        class_methods = self._request_methods(args)
                        if not prefixes and args.strip() and not re.match(r"^\s*(method|produces|consumes)\s*=", args):
                            unresolved = True
                        prefixes = prefixes or [""]
                if unresolved:
                    # 前缀求值失败时宁可不导出：以前退化成空前缀，导出了 /page、/count 这种不存在的路径
                    self.unresolved_controllers.append(cls.name)
                    continue
                ops.extend(self._controller_operations(cls, prefixes, class_methods))
        return ops

    @staticmethod
    def _class_header(cls: JavaClass) -> str:
        text = cls.path.read_bytes().decode("utf-8", "replace")
        text = _strip_comments_keep_javadoc(text)
        m = re.search(r"\b(class|interface)\s+" + re.escape(cls.name) + r"\b", text)
        if not m:
            return ""
        prev = text[:m.start()]
        last_import = max(prev.rfind("import "), prev.rfind("package "))
        return prev[prev.find(";", last_import) + 1:] if last_import >= 0 else prev

    def _controller_operations(self, cls: JavaClass, prefixes: list[str],
                               class_methods: list[str]) -> list[Operation]:
        ops = []
        # 按类体顶层成员逐个看（包级可见的 `BaseResponse<X> foo(...)` 也要认，不能只找 public）
        for header, method_body in _members(cls.body):
            docs = re.findall(r"/\*\*([\s\S]*?)\*/", header)
            code = re.sub(r"/\*\*[\s\S]*?\*/", " ", header)
            m = re.search(r"(\w+)\s*\(", _strip_annotations(code))
            if not m:
                continue
            anns = self._annotations_before(code)
            method_paths, http_methods = None, []
            permission = None
            summary = _doc_summary(docs[-1]) if docs else ""
            for name, args in anns:
                if name in _MAPPING_METHODS:
                    method_paths = self._mapping_paths(args, cls.name)
                    http_methods = [_MAPPING_METHODS[name]]
                elif name == "RequestMapping":
                    method_paths = self._mapping_paths(args, cls.name)
                    http_methods = self._request_methods(args) or class_methods or ["post"]
                elif name == "PreAuthorize":
                    pm = re.search(r"has(?:Any)?Authority\(\s*'([^']+)'", args)
                    permission = pm.group(1) if pm else None
                elif name in {"ApiOperation", "Operation", "Log", "OperationLog", "SysLog"}:
                    sm = re.search(r'"([^"]+)"', args)
                    summary = sm.group(1) if sm else summary
            if method_paths is None:
                continue
            # 参数要从带注解的原文里取（@RequestBody 在参数上）
            code_open = _find_call_paren(code, m.group(1))
            params = self._parse_params(code[code_open + 1:_match_paren(code, code_open)], cls)
            signature = _strip_annotations(code)
            rt = re.search(r"([\w.]+(?:\s*<.*?>)?(?:\s*\[\s*\])*)\s+" + re.escape(m.group(1)) + r"\s*\(",
                           re.sub(r"\b(public|protected|private|static|final|synchronized|abstract)\b", " ", signature),
                           re.S)
            params["return_type"] = re.sub(r"\s+", "", rt.group(1)) if rt else None
            if re.search(r"(\bvoid|ResponseEntity\s*<\s*byte\s*\[\s*\]\s*>|\bResource|InputStreamResource)\s+"
                         + re.escape(m.group(1)) + r"\s*\(", signature):
                # 都是 RequestUtils.getResponse() 拿响应写 xlsx，参数里并没有 HttpServletResponse
                params["file_response"] = True
            if _WRITE_CALL.search(method_body):
                # 只看 URL 的写保护把 check 当成了查询
                params["writes"] = True
            for prefix in prefixes:
                for mp in method_paths or [""]:
                    full = "/" + "/".join(s.strip("/") for s in (prefix, mp) if s.strip("/"))
                    full = re.sub(r"\{(\w+):[^}]*\}", r"{\1}", full)
                    full = re.sub(r"/{2,}", "/", full)
                    for http in http_methods:
                        ops.append(Operation(method=http, path=full, controller=cls.name,
                                             handler=m.group(1), summary=summary,
                                             permission=permission, **params))
        return ops

    def _parse_params(self, text: str, owner: JavaClass) -> dict[str, Any]:
        out: dict[str, Any] = {"body_type": None, "body_validated": True, "query_params": [],
                               "path_params": [], "multipart": False, "file_response": False, "writes": False}
        depth, buf, pieces = 0, "", []
        for ch in text:
            if ch in "(<":
                depth += 1
            elif ch in ")>":
                depth -= 1
            if ch == "," and depth == 0:
                pieces.append(buf)
                buf = ""
            else:
                buf += ch
        if buf.strip():
            pieces.append(buf)
        for piece in pieces:
            anns = dict(self._annotations_before(piece))
            plain = re.sub(r"@\w+(\s*\((?:[^()]|\([^()]*\))*\))?", " ", piece).split()
            plain = [p for p in plain if p != "final"]
            if len(plain) < 2:
                continue
            java_type, var = " ".join(plain[:-1]), plain[-1]
            base_type = re.sub(r"<.*", "", java_type).strip()
            if base_type == "HttpServletResponse":
                out["file_response"] = True
            if base_type in _SKIP_PARAM_TYPES:
                continue
            if "MultipartFile" in java_type:
                out["multipart"] = True
                continue
            if "RequestBody" in anns:
                out["body_type"] = java_type
                out["body_validated"] = "Validated" in anns or "Valid" in anns
            elif "PathVariable" in anns:
                out["path_params"].append(_annotation_name(anns["PathVariable"]) or var)
            elif "RequestParam" in anns or base_type in _STRING_TYPES | set(_INTEGER_TYPES) | _NUMBER_TYPES | _BOOLEAN_TYPES:
                args = anns.get("RequestParam", "")
                required = "required = false" not in args.replace("required=false", "required = false") \
                    and "defaultValue" not in args and "RequestParam" in anns
                out["query_params"].append({"name": _annotation_name(args) or var,
                                            "type": java_type, "required": required})
        return out

    # ---------- 模型 ----------
    def fields_of(self, cls: JavaClass, _seen: set[str] | None = None) -> list[dict[str, Any]]:
        seen = _seen if _seen is not None else set()
        if cls.name in seen:
            return []
        seen.add(cls.name)
        fields: list[dict[str, Any]] = []
        if cls.extends:
            parent = self.find_class(cls.extends, cls)
            if parent:
                fields.extend(self.fields_of(parent, seen))
            else:
                # 父类的字段拿不到，请求结构就是残的——记下来，由调用方报给用户
                self.missing_parents.setdefault(cls.extends, set()).add(cls.name)
        # 按类体顶层成员逐个看：成员扫描器跳过注释与字符串，Javadoc 里的分号不会把字段切断
        for stmt, member_body in _members(cls.body):
            no_doc = re.sub(r"/\*\*[\s\S]*?\*/", " ", stmt)
            # 只看等号左边：`private List<X> items = new ArrayList<>();` 是字段，不是方法
            no_ann = re.sub(r"@\w+\s*\((?:[^()]|\([^()]*\))*\)", "", no_doc)
            left = no_ann.split("=")[0]
            # 关键字也要在去掉注解后判断：@EnumValid(target = WeightUnitEnum.class) 里有 class
            if member_body or re.search(r"\b(static|class|interface|enum)\b", no_ann) or "(" in left:
                continue
            doc = re.findall(r"/\*\*([\s\S]*?)\*/", stmt)
            code = re.sub(r"/\*\*[\s\S]*?\*/", "", stmt)
            anns = self._annotations_before(code)
            decl = re.sub(r"@\w+(\s*\((?:[^()]|\([^()]*\))*\))?", " ", code)
            decl = re.sub(r"\b(private|protected|public|final|transient|volatile)\b", " ", decl)
            decl = decl.split("=")[0].strip()
            m = re.match(r"^([\w.]+(?:\s*<[\w\s,<>?.\[\]]*>)?(?:\s*\[\s*\])*)\s+(\w+)$", decl, re.S)
            if not m:
                continue
            description = ""
            if doc:
                description = " ".join(line.strip(" *\t") for line in doc[-1].splitlines()).strip()
            for name, args in anns:
                if name in {"ApiModelProperty", "Schema"} and not description:
                    dm = re.search(r'"([^"]*)"', args)
                    description = dm.group(1) if dm else ""
            json_name = m.group(2)
            for name, args in anns:
                if name in {"JsonProperty", "JSONField"}:
                    jm = re.search(r'(?:value\s*=\s*|name\s*=\s*|^\s*)"([^"]+)"', args)
                    json_name = jm.group(1) if jm else json_name
                if name in {"JsonIgnore"}:
                    json_name = None
            if json_name is None:
                continue
            fields = [f for f in fields if f["name"] != json_name]
            fields.append({"name": json_name, "type": re.sub(r"\s+", "", m.group(1)),
                           "annotations": anns, "description": description})
        return fields

    def schema_for(self, java_type: str, context: JavaClass | None,
                   components: dict[str, Any], depth: int = 0) -> dict[str, Any]:
        java_type = re.sub(r"\s+", "", java_type)
        if java_type.endswith("[]"):
            return {"type": "array", "items": self.schema_for(java_type[:-2], context, components, depth)}
        generic = re.match(r"^([\w.]+)<(.*)>$", java_type)
        base = (generic.group(1) if generic else java_type).rsplit(".", 1)[-1]
        if base in _COLLECTION_TYPES:
            inner = generic.group(2) if generic else "Object"
            return {"type": "array", "items": self.schema_for(inner, context, components, depth)}
        if base in _MAP_TYPES:
            return {"type": "object", "additionalProperties": True}
        if base in _INTEGER_TYPES:
            return {"type": "integer", "format": _INTEGER_TYPES[base]}
        if base in _NUMBER_TYPES:
            return {"type": "number"}
        if base in _BOOLEAN_TYPES:
            return {"type": "boolean"}
        if base in _STRING_TYPES:
            return {"type": "string"}
        if base in _DATETIME_TYPES:
            return {"type": "string", "format": "date-time"}
        if base in _DATE_TYPES:
            return {"type": "string", "format": "date"}
        cls = self.find_class(base, context)
        if cls is None:
            return {}  # 认不出来就不给类型，别瞎猜
        if cls.kind == "enum":
            values = _enum_values(cls.body)
            return {"type": "string", "enum": values} if values else {"type": "string"}
        if depth > 6:
            return {"type": "object"}
        if base not in components:
            components[base] = {}  # 先占位，防止自引用递归
            components[base] = self.object_schema(cls, components, depth + 1)
        return {"$ref": f"#/components/schemas/{base}"}

    # ---------- 返回结构 ----------
    def _json_response(self, op: Operation, ctrl: JavaClass | None) -> dict[str, Any]:
        if not op.return_type or op.return_type in ("void", "Object", "ResponseEntity"):
            return {"200": {"description": "OK"}}
        schema = self.response_schema(op.return_type, ctrl)
        if not schema:
            return {"200": {"description": "OK"}}
        schema = _nullable(schema)
        ok = {"description": "OK", "content": {"application/json": {"schema": schema}},
              "x-return-type": op.return_type}
        # x-derived-from 标明这份结构来自源码、可以拿来断言。只给返回对象的接口标：
        # 直接返回 List/String 的处理方法，出错时会被统一包成 {code,message,data}
        if "object" in (schema.get("type") or []):
            ok["x-derived-from"] = "source"
        return {"200": ok}

    def _type_params(self, cls: JavaClass) -> list[str]:
        text = cls.path.read_bytes().decode("utf-8", "replace")
        m = re.search(r"\b(?:class|interface)\s+" + re.escape(cls.name) + r"\s*<([^>{]*)>", text)
        if not m:
            return []
        return [p.strip().split()[0] for p in _split_top(m.group(1)) if p.strip()]

    def _string_serializer(self, args: str, context: JavaClass) -> bool:
        """识别将字段序列化为字符串的自定义 @JsonSerialize 实现。"""
        m = re.search(r"using\s*=\s*(\w+)\s*\.\s*class", args)
        if not m:
            return False
        if m.group(1) == "ToStringSerializer":
            return True
        serializer = self.find_class(m.group(1), context)
        return bool(serializer and "writeString" in serializer.body)

    def response_schema(self, java_type: str, context: JavaClass | None,
                        bindings: dict[str, str] | None = None, depth: int = 0,
                        stack: tuple = ()) -> dict[str, Any]:
        """返回结构：泛型展开、全部内联、字段一律可选。

        只写能确定的：日期类型的输出格式受全局与字段配置影响，不给 type；认不出的类不给 type。
        """
        bindings = bindings or {}
        java_type = re.sub(r"\s+", "", java_type or "")
        if java_type in bindings:
            return self.response_schema(bindings[java_type], context, None, depth, stack)
        if java_type.endswith("[]"):
            return {"type": "array", "items": self.response_schema(java_type[:-2], context, bindings, depth, stack)}
        generic = re.match(r"^([\w.]+)<(.*)>$", java_type)
        base = (generic.group(1) if generic else java_type).rsplit(".", 1)[-1]
        args = _split_top(generic.group(2)) if generic else []
        args = [bindings.get(a, a) for a in args]
        if base in _COLLECTION_TYPES:
            return {"type": "array", "items": self.response_schema(args[0] if args else "Object", context,
                                                                   bindings, depth, stack)}
        if base in _MAP_TYPES:
            return {"type": "object"}
        if base in _INTEGER_TYPES:
            return {"type": "integer"}
        if base in _NUMBER_TYPES:
            return {"type": "number"}
        if base in _BOOLEAN_TYPES:
            return {"type": "boolean"}
        if base in _STRING_TYPES:
            return {"type": "string"}
        if base in _DATETIME_TYPES or base in _DATE_TYPES or base in ("Object", "T", "?"):
            return {}
        cls = self.find_class(base, context)
        if cls is None:
            return {}
        if cls.kind == "enum":
            return {"type": "string"}
        key = java_type
        if depth > 6 or key in stack:
            return {"type": "object"}
        params = self._type_params(cls)
        local = dict(zip(params, args))
        props = {}
        for f in self.fields_of(cls):
            if f["name"].startswith("_") or f["name"] == "serialVersionUID":
                continue
            if any(name == "JsonIgnore" for name, _ in f["annotations"]):
                continue
            ftype = re.sub(r"\s+", "", f["type"])
            schema = self.response_schema(ftype, cls, {**bindings, **local}, depth + 1, stack + (key,))
            for name, a in f["annotations"]:
                if name == "JsonSerialize" and self._string_serializer(a, cls):
                    schema = {"type": "string"}
                elif name == "JsonFormat" and re.search(r"pattern\s*=|shape\s*=\s*JsonFormat\.Shape\.STRING", a):
                    schema = {"type": "string"}
            if f["description"]:
                schema = {**schema, "description": f["description"]}
            props[f["name"]] = schema
        return {"type": "object", "properties": props}

    def object_schema(self, cls: JavaClass, components: dict[str, Any], depth: int = 0) -> dict[str, Any]:
        props, required = {}, []
        for f in self.fields_of(cls):
            if f["name"].startswith("_") or f["name"] == "serialVersionUID":
                continue
            schema = dict(self.schema_for(f["type"], cls, components, depth))
            for name, args in f["annotations"]:
                num = lambda key: (re.search(key + r"\s*=\s*(-?\d+)", args) or [None, None])[1]  # noqa: E731
                if name in _REQUIRED_ANNOTATIONS:
                    required.append(f["name"])
                    if name in {"NotBlank", "NotEmpty"} and schema.get("type") == "string":
                        schema.setdefault("minLength", 1)
                    if name == "NotEmpty" and schema.get("type") == "array":
                        schema.setdefault("minItems", 1)
                elif name in {"Size", "Length"}:
                    lo, hi = num("min"), num("max")
                    key_lo, key_hi = ("minItems", "maxItems") if schema.get("type") == "array" else ("minLength", "maxLength")
                    if lo is not None:
                        schema[key_lo] = int(lo)
                    if hi is not None:
                        schema[key_hi] = int(hi)
                elif name in {"Min", "Max"}:
                    val = num("value") or (re.match(r"\s*(-?\d+)", args) or [None, None])[1]
                    if val is not None:
                        schema["minimum" if name == "Min" else "maximum"] = int(val)
                elif name in {"DecimalMin", "DecimalMax"}:
                    dm = re.search(r'"(-?[\d.]+)"', args)
                    if dm:
                        schema["minimum" if name == "DecimalMin" else "maximum"] = float(dm.group(1))
                elif name == "Pattern":
                    pm = re.search(r'regexp\s*=\s*"((?:[^"\\]|\\.)*)"', args)
                    if pm:
                        schema["pattern"] = pm.group(1).encode().decode("unicode_escape")
                elif name == "Email":
                    schema["format"] = "email"
                elif name in {"EnumValid", "EnumValue", "InEnum"}:
                    em = re.search(r"(\w+)\s*\.\s*class", args)
                    enum_cls = self.find_class(em.group(1), cls) if em else None
                    if enum_cls is not None and enum_cls.kind == "enum":
                        values = _enum_values(enum_cls.body)
                        if values:
                            schema["enum"] = values
            if f["description"]:
                schema["description"] = f["description"]
            if "$ref" in schema and len(schema) > 1:
                schema = {"allOf": [{"$ref": schema.pop("$ref")}], **schema}
            props[f["name"]] = schema
        result: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            result["required"] = sorted(set(required), key=required.index)
        return result

    # ---------- 输出 ----------
    def to_openapi(self, title: str = "Spring 源码推导", server_prefix: str = "") -> dict[str, Any]:
        components: dict[str, Any] = {}
        paths: dict[str, dict[str, Any]] = {}
        for op in sorted(self.operations(), key=lambda o: (o.path, o.method)):
            ctrl = self.find_class(op.controller)
            item: dict[str, Any] = {
                "summary": op.summary or f"{op.controller}.{op.handler}",
                "operationId": f"{op.controller}_{op.handler}_{op.method}",
                "tags": [op.controller],
                "responses": ({"200": {"description": "文件", "content": {"application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}}}}} if op.file_response
                    else self._json_response(op, ctrl)),
                "x-source": {"controller": op.controller, "handler": op.handler},
            }
            if op.permission:
                item["x-permission"] = op.permission
            if op.writes:
                item["x-side-effect"] = "write"
            parameters = [{"name": p, "in": "path", "required": True, "schema": {"type": "string"}}
                          for p in op.path_params]
            for q in op.query_params:
                parameters.append({"name": q["name"], "in": "query", "required": q["required"],
                                   "schema": self.schema_for(q["type"], ctrl, components)})
            if parameters:
                item["parameters"] = parameters
            if op.multipart:
                item["requestBody"] = {"content": {"multipart/form-data": {"schema": {
                    "type": "object", "properties": {"file": {"type": "string", "format": "binary"}}}}}}
            elif op.body_type:
                schema = self.schema_for(op.body_type, ctrl, components)
                if not op.body_validated:
                    # 参数上没有 @Validated/@Valid，DTO 上的 @NotBlank 等注解根本不会执行。
                    # 照写 required 的话，「缺必填应被拒」的用例必然失败，而那不是被测系统的缺陷。
                    schema = _strip_validation(inline_refs(schema, components))
                    item["x-validation-enforced"] = False
                item["requestBody"] = {"required": True, "content": {"application/json": {
                    "schema": schema}}}
            paths.setdefault(server_prefix + op.path, {})[op.method] = item
        return {"openapi": "3.0.3", "info": {"title": title, "version": "source"},
                "paths": paths, "components": {"schemas": components}}


def _nullable(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_nullable(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: (_nullable(v) if k in ("items",) else
               {n: _nullable(s) for n, s in v.items()} if k == "properties" and isinstance(v, dict) else v)
           for k, v in schema.items()}
    if isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    return out


def _split_top(text: str) -> list[str]:
    """按顶层逗号切分泛型参数：Map<String,List<X>>,Y -> ["Map<String,List<X>>", "Y"]。"""
    parts, buf, depth = [], [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf).strip())
    return parts


def _enum_values(body: str) -> list[str]:
    """枚举常量：类体里第一个顶层分号之前、按顶层逗号分开的每一项的名字。

    常量可以写在一行（KG, LBS;），也可以带构造参数与注释（DRAFT(0, "草稿"),）。
    """
    text = re.sub(r"/\*[\s\S]*?\*/", " ", body)
    items, buf, depth = [], [], 0
    for ch in text:
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        if depth == 0 and ch in ",;":
            items.append("".join(buf))
            buf = []
            if ch == ";":
                break
            continue
        buf.append(ch)
    else:
        items.append("".join(buf))
    names = []
    for item in items:
        m = re.match(r"\s*(?:@\w+(?:\([^)]*\))?\s*)*([A-Z][A-Za-z0-9_]*)\b", item)
        if m:
            names.append(m.group(1))
    return names


def _strip_annotations(text: str) -> str:
    out, i = [], 0
    while True:
        m = _ANNOTATION.search(text, i)
        if not m:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:m.start()])
        i = _match_paren(text, m.end() - 1) + 1 if m.group(2) else m.end()


def _find_call_paren(code: str, name: str) -> int:
    """方法名后面那个左括号（跳过注解参数里的同名文本）。"""
    i = 0
    while True:
        m = _ANNOTATION.search(code, i)
        target = re.compile(r"\b" + re.escape(name) + r"\s*\(")
        t = target.search(code, i)
        if not m or (t and t.start() < m.start()):
            return code.index("(", t.start())
        i = _match_paren(code, m.end() - 1) + 1 if m.group(2) else m.end()


# 处理方法体里的写库调用：MyBatis-Plus / 常见 service 命名
_WRITE_CALL = re.compile(
    r"\b\w*(?:Service|Mapper|Dao|Repository|Manager)\s*\.\s*"
    r"(save|saveBatch|saveOrUpdate|insert|update|updateById|updateBatchById|delete|deleteById|remove|removeById|"
    r"removeByIds|batchDelete|batchUpdate|add|create|audit|approve|submit|cancel|withdraw|confirm|"
    r"enable|disable|import|sync)\w*\s*\(")


def _member_headers(body: str) -> list[str]:
    return [header for header, _ in _members(body)]


def _members(body: str) -> list[tuple[str, str]]:
    """类体顶层每个成员：(头, 方法体)。头是方法体 `{...}` 之前 / 字段 `;` 之前的文本，含 Javadoc 与注解。"""
    headers, buf, depth, i, n = [], [], 0, 0, len(body)
    while i < n:
        ch = body[i]
        if body.startswith("/**", i):
            end = body.find("*/", i + 3)
            end = n if end < 0 else end + 2
            buf.append(body[i:end])
            i = end
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and body[j] != ch:
                j += 2 if body[j] == chr(92) else 1
            buf.append(body[i:j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and ch in "{;":
            method_body = ""
            if ch == "{":
                end = _match_paren(body, i, "{", "}")
                method_body = body[i + 1:end]
            headers.append(("".join(buf), method_body))
            buf = []
            if ch == "{":
                i = end
        else:
            buf.append(ch)
        i += 1
    return headers


def _doc_summary(doc: str) -> str:
    for line in doc.splitlines():
        line = line.strip(" *\t")
        if line and not line.startswith("@"):
            return line[:80]
    return ""


def _last_statement_end(text: str) -> int:
    """最后一个处于括号外、字符串外的 `;` 或 `}` 的位置；注解参数里的 {"a","b"} 不算。"""
    depth, last, i, n = 0, -1, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == chr(92) else 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch in ";}" and depth <= 0:
            last = i
        i += 1
    return last


_VALIDATION_KEYWORDS = {"required", "minLength", "maxLength", "minItems", "maxItems",
                        "minimum", "maximum", "pattern"}


def _strip_validation(schema: Any) -> Any:
    """去掉校验类关键字，保留类型、结构与说明。"""
    if isinstance(schema, list):
        return [_strip_validation(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in _VALIDATION_KEYWORDS:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _strip_validation(sub) for name, sub in value.items()}
        else:
            out[key] = _strip_validation(value)
    return out


def _annotation_name(args: str) -> str | None:
    m = re.search(r'(?:value|name)\s*=\s*"([^"]+)"', args) or re.match(r'\s*"([^"]+)"', args)
    return m.group(1) if m else None


def inline_refs(schema: Any, components: dict[str, Any], _stack: tuple[str, ...] = ()) -> Any:
    """把 $ref 展开成内联 schema（循环引用处截断为 object），给只认内联的生成器用。"""
    if isinstance(schema, list):
        return [inline_refs(s, components, _stack) for s in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        name = ref.rsplit("/", 1)[-1]
        if name in _stack:
            return {"type": "object"}
        return inline_refs(components.get(name, {}), components, _stack + (name,))
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and len(all_of) == 1:
        # 「引用 + 说明」为了合规写成了 allOf，内联时直接合并，免得下游只看 type 的生成器认不出
        merged = dict(inline_refs(all_of[0], components, _stack))
        merged.update({k: inline_refs(v, components, _stack) for k, v in schema.items() if k != "allOf"})
        return merged
    return {k: inline_refs(v, components, _stack) for k, v in schema.items()}
