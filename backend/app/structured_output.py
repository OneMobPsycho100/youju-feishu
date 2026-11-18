"""模型 JSON 输出的有限容错；只处理格式，不推断字段值或业务结论。"""
import json
import math
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ValidationError

MAX_OUTPUT_CHARACTERS = 200000
MAX_JSON_CANDIDATES = 32
MAX_JSON_DEPTH = 64
JSON_WHITESPACE = " \t\r\n"
ModelT = TypeVar("ModelT", bound=BaseModel)
Repair = Literal["bom", "trailing_comma"]


class ModelOutputError(ValueError):
    """程序生成、可公开的模型校验错误；不得包含原始字段值或供应商异常正文。"""


class ModelOutputEmptyError(ModelOutputError):
    """模型未返回有效文本。"""


class ModelOutputJSONError(ModelOutputError):
    """无法取得完整、无重复键且符合标准语法的 JSON 对象。"""


class ModelOutputSchemaError(ModelOutputError):
    """JSON 对象的字段不符合调用方指定的 Pydantic 模型。"""


class ModelOutputAmbiguousError(ModelOutputError):
    """多份不同的 JSON 对象均通过字段校验，不能自动选择。"""


class ModelOutputLimitError(ModelOutputError):
    """回复长度、候选数量或嵌套深度超过解析上限。"""


@dataclass(frozen=True)
class StructuredOutput(Generic[ModelT]):
    """解析结果：value 已通过 Schema 校验，mode 和 repairs 仅用于记录格式处理过程。"""
    value: ModelT
    mode: Literal["direct", "extracted"]
    repairs: tuple[Repair, ...] = ()


def _candidate_spans(content: str) -> list[tuple[int, int]]:
    """扫描最外层容器的起止位置；不将数组或无效对象内的子对象独立采纳。"""
    spans, stack = [], []
    start, count = 0, 0
    in_string = escaped = False
    for index, character in enumerate(content):
        if not stack:
            if character not in "{[":
                continue
            count += 1
            if count > MAX_JSON_CANDIDATES:
                raise ModelOutputLimitError("模型回复的 JSON 候选超过 32 个，已保留人工复核")
            start = index
            stack.append(character)
            continue
        # 字符串中的括号和转义引号均属于原始值，不能影响容器边界。
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append(character)
            if len(stack) > MAX_JSON_DEPTH:
                raise ModelOutputLimitError("模型回复的 JSON 嵌套超过 64 层，已保留人工复核")
        elif character in "}]":
            if stack[-1] != ("{" if character == "}" else "["):
                raise ModelOutputJSONError("模型返回的 JSON 括号不匹配，已保留人工复核")
            stack.pop()
            if not stack:
                spans.append((start, index + 1))
    if stack:
        raise ModelOutputJSONError("模型返回的 JSON 不完整，已保留人工复核")
    return spans


def _remove_trailing_commas(content: str) -> str:
    """仅删除完整值之后、闭括号之前的尾逗号；字符串内每个字符原样保留。"""
    characters = []
    in_string = escaped = False
    previous = ""
    for index, character in enumerate(content):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character == "," and previous and previous not in "{[,:":
            following = index + 1
            while following < len(content) and content[following] in JSON_WHITESPACE:
                following += 1
            if following < len(content) and content[following] in "}]":
                # 不更新 previous，保留被删除逗号之前的原始值边界。
                continue
        characters.append(character)
        if character not in JSON_WHITESPACE:
            previous = character
    return "".join(characters)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """在任意嵌套层级拒绝重复键，避免标准解码器静默使用最后一个值。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelOutputJSONError("模型返回的 JSON 包含重复字段，已保留人工复核")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ModelOutputJSONError("模型返回的 JSON 包含非标准数值，已保留人工复核")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ModelOutputJSONError("模型返回的 JSON 数值超出有效范围，已保留人工复核")
    return number


def _decode(content: str):
    """沿用标准 JSON 类型，额外拒绝重复键、NaN、Infinity 及浮点溢出。"""
    try:
        return json.loads(content, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except (json.JSONDecodeError, ModelOutputError):
        raise
    except RecursionError:
        raise ModelOutputLimitError("模型回复的 JSON 嵌套超过解析上限，已保留人工复核") from None
    except ValueError:
        raise ModelOutputJSONError("模型返回的 JSON 数字格式无效，已保留人工复核") from None


def _decode_candidate(content: str) -> tuple[object, tuple[Repair, ...]]:
    try:
        return _decode(content), ()
    except json.JSONDecodeError:
        repaired = _remove_trailing_commas(content)
        if repaired != content:
            try:
                return _decode(repaired), ("trailing_comma",)
            except json.JSONDecodeError:
                pass
        raise ModelOutputJSONError("模型返回的 JSON 语法错误，有限格式修复后仍无法解析，已保留人工复核") from None


def _validate(value: object, output_model: type[ModelT]) -> ModelT:
    if not isinstance(value, dict):
        raise ModelOutputJSONError("模型必须返回顶层 JSON 对象，已保留人工复核")
    try:
        # 使用调用方模型自身的 strict、extra 及字段约束，解析层不转换或补写业务值。
        return output_model.model_validate(value)
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False)[:3]:
            location = list(error["loc"])
            if error["type"] == "extra_forbidden" and location:
                location[-1] = "<未声明字段>"
            path = ".".join(str(part)[:40] for part in location[:6]) or "<根对象>"
            details.append(f"{path}: {error['type']}")
        # 仅展示有限的字段路径和错误类别，不拼接 Pydantic 的 input、msg 或 ctx。
        description = "；".join(details)[:350]
        raise ModelOutputSchemaError(f"模型返回字段未通过校验（{description}），已保留人工复核") from None


def parse_structured_output(content: str, output_model: type[ModelT]) -> StructuredOutput[ModelT]:
    """从最终回复取得唯一有效对象，并按指定 Schema 校验。

    优先解析整段 JSON，再扫描说明文字或代码围栏内的最外层容器；仅修复开头 BOM
    和字符串外尾逗号。不同有效对象视为歧义，业务判断由调用方在成功返回后执行。
    失败均抛出可公开的 ModelOutputError 子类，原始回复由调用方独立留存。
    """
    if len(content) > MAX_OUTPUT_CHARACTERS:
        raise ModelOutputLimitError("模型回复超过 200000 字符，已保留人工复核")
    text = content.strip()
    repairs: tuple[Repair, ...] = ()
    if text.startswith("\ufeff"):
        text = text[1:].strip()
        repairs = ("bom",)
    if not text:
        raise ModelOutputEmptyError("模型未返回有效内容，已保留人工复核")

    # 先线性扫描边界以限制深度和数量，再交给解码器，避免超深数据触发递归耗尽。
    spans = _candidate_spans(text)
    try:
        value = _decode(text)
    except (json.JSONDecodeError, ModelOutputJSONError):
        pass
    else:
        # 整段本身为 JSON 时必须校验根对象，不能绕过坏字段去提取其嵌套对象。
        return StructuredOutput(_validate(value, output_model), "direct", repairs)

    valid: dict[str, StructuredOutput[ModelT]] = {}
    schema_error = None
    json_error = None
    for start, end in spans:
        try:
            value, candidate_repairs = _decode_candidate(text[start:end])
            validated = _validate(value, output_model)
        except ModelOutputSchemaError as exc:
            if schema_error is None:
                schema_error = exc
            continue
        except ModelOutputJSONError as exc:
            if json_error is None:
                json_error = exc
            continue
        # 按解码后的完整对象去重，保留 Schema 忽略的额外字段差异，避免静默选错结果。
        identity = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        valid.setdefault(identity, StructuredOutput(validated, "direct" if (start, end) == (0, len(text))
                                                    else "extracted", repairs + candidate_repairs))
    if len(valid) > 1:
        raise ModelOutputAmbiguousError("模型返回多份内容不同且均符合结构的 JSON，无法确定唯一结果，已保留人工复核")
    if valid:
        return next(iter(valid.values()))
    if schema_error is not None:
        raise schema_error
    if json_error is not None:
        raise json_error
    raise ModelOutputJSONError("未能从模型回复中提取完整的 JSON 对象，已保留人工复核")
