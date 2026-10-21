"""
Executor V2 - JSON Schema 校验器

使用标准 jsonschema 库进行校验；如果未安装则使用内置轻量实现。
"""

from typing import Any, Dict, List


class SchemaValidator:
    """JSON Schema 校验器"""

    def __init__(self):
        self._has_jsonschema = False
        try:
            import jsonschema
            self._has_jsonschema = True
        except ImportError:
            pass

    def validate(self, data: Any, schema: Dict[str, Any]) -> List[str]:
        """
        校验 data 是否符合 schema。

        Returns:
            错误信息列表，空列表表示校验通过。
        """
        if self._has_jsonschema:
            return self._validate_with_lib(data, schema)
        return self._validate_builtin(data, schema)

    # ------------------------------------------------------------------

    def _validate_with_lib(self, data: Any, schema: Dict[str, Any]) -> List[str]:
        import jsonschema
        errors = []
        validator = jsonschema.Draft7Validator(schema)
        for error in validator.iter_errors(data):
            path = ".".join(str(p) for p in error.absolute_path) or "<root>"
            errors.append(f"{path}: {error.message}")
        return errors

    def _validate_builtin(self, data: Any, schema: Dict[str, Any]) -> List[str]:
        """轻量校验：只检查 type / required / properties"""
        errors: List[str] = []
        self._check_node(data, schema, "", errors)
        return errors

    def _check_node(self, data: Any, schema: Dict[str, Any], path: str, errors: List[str]):
        # type check
        expected_type = schema.get("type")
        if expected_type:
            if not self._type_match(data, expected_type):
                errors.append(f"{path or '<root>'}: 期望类型 {expected_type}，实际 {type(data).__name__}")
                return

        # required
        if isinstance(data, dict):
            for field in schema.get("required", []):
                if field not in data:
                    errors.append(f"{path}.{field}: 必填字段缺失")

            # properties
            props = schema.get("properties", {})
            for field, sub_schema in props.items():
                if field in data:
                    self._check_node(data[field], sub_schema, f"{path}.{field}", errors)

        # items (array)
        if isinstance(data, list) and "items" in schema:
            for i, item in enumerate(data[:10]):  # 最多检查10项
                self._check_node(item, schema["items"], f"{path}[{i}]", errors)

    @staticmethod
    def _type_match(data: Any, expected: str) -> bool:
        mapping = {
            "object": dict,
            "array": list,
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "null": type(None),
        }
        t = mapping.get(expected)
        if t is None:
            return True
        return isinstance(data, t)
