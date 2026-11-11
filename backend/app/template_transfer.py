"""审批定义规范化与配置迁移。外部读取先完成，再进入短写事务。"""
import hashlib
import json
import re
from collections import Counter
from datetime import timezone
from urllib.parse import parse_qs, urlparse

from . import schemas as s
from . import template_schemas as t
from .config import settings
from .db import begin_write
from .integrations import DomainError, FeishuGateway
from .models import ApprovalTemplate, User, now
from .template_config import apply_template_config


FIELD_LABELS = {
    "amount": "申请金额", "company_tax_id": "主体税号", "expense_type": "费用类型",
    "description": "申请事由", "attachment_ids": "票据附件", "company": "主体名称",
    "department": "部门", "project": "所属项目", "payment_method": "付款方式",
    "expected_payment_date": "期望付款日期", "payee_account": "收款账户",
    "related_approvals": "关联审批", "remarks": "备注",
}
GROUP_TYPES = {"fieldlist", "mutablegroup", "leavegroupv2", "workgroup", "tripgroup", "outgroup"}
DISPLAY_TYPES = {"text", "document", "serialnumber"}
ATTACHMENT_TYPES = {"attachment", "attachmentv2", "image", "imagev2"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def template_reference(reference: str) -> str:
    """只从链接提取编码，再调用固定飞书 API；绝不请求用户提供的 URL。"""
    value = reference.strip()
    if "://" in value:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "feishu.cn" or host.endswith(".feishu.cn")):
            raise DomainError("请使用飞书审批后台的 HTTPS 模板编辑链接，或直接填写 approval_code")
        codes = parse_qs(parsed.query).get("definitionCode", [])
        if len(codes) != 1:
            raise DomainError("模板链接中缺少唯一的 definitionCode，请复制审批后台的模板编辑链接")
        value = codes[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise DomainError("模板编码格式不正确，请填写 approval_code 或含 definitionCode 的飞书链接")
    # 飞书编码是 UUID；规范大小写，避免相同远端模板被当成两个本地模板。
    return value.upper() if re.fullmatch(r"[0-9a-fA-F-]{36}", value) else value


def normalize_definition(code: str, raw: dict, *, demo: bool = False) -> t.ApprovalDefinition:
    """递归展开 children 中的明细/控件组，只导出描述，不保留默认值或选项数据。"""
    form = raw.get("form", [])
    try:
        form = json.loads(form) if isinstance(form, str) else form
    except (ValueError, TypeError):
        raise DomainError("飞书审批定义的表单格式无法解析，请检查模板")
    if not isinstance(form, list):
        raise DomainError("飞书审批定义的表单应为控件数组")
    fields: list[t.DefinitionField] = []
    seen = set()

    def visit(items: list, path: list[str], depth: int = 0):
        if depth > 32:
            raise DomainError("审批表单明细层级过深，无法生成控件映射")
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            name = str(item.get("name") or "")
            children = item.get("children")
            if not isinstance(children, list) and kind.lower() in GROUP_TYPES:
                children = item.get("value")
            if isinstance(children, list):
                visit(children, path + [name or "未命名明细"], depth + 1)
            field_id = str(item.get("id") or "")
            if not field_id or isinstance(children, list) or kind.lower() in GROUP_TYPES | DISPLAY_TYPES:
                continue
            if field_id in seen:
                raise DomainError("审批定义含重复控件 ID，无法可靠映射")
            seen.add(field_id)
            fields.append(t.DefinitionField(id=field_id, custom_id=str(item.get("custom_id") or ""),
                                            name=name, type=kind, path=path))

    visit(form, [])
    # 部分飞书模板将发起/结束也返回为 AND，不能靠名称推定节点身份。
    # 仅过滤明确的 START/END；其余展示名称和 ID，由管理员确认专用节点并用样本验证。
    nodes = [
        t.DefinitionNode(id=str(item.get("node_id") or ""), custom_id=str(item.get("custom_node_id") or ""),
                         name=str(item.get("name") or ""), type=str(item.get("node_type") or ""))
        for item in (raw.get("node_list") or []) if isinstance(item, dict) and item.get("node_id")
        and str(item.get("node_type") or "").upper() not in {"START", "END"}
    ]
    result = t.ApprovalDefinition(code=code, name=str(raw.get("approval_name") or code),
                                 status=str(raw.get("status") or ""), fields=fields, nodes=nodes, demo=demo)
    # 排序只影响摘要，不改变下拉中的表单顺序；API 改变返回顺序不应造成误报。
    content = result.model_dump(exclude={"fingerprint"})
    content["fields"] = sorted(content["fields"], key=lambda item: item["id"])
    content["nodes"] = sorted(content["nodes"], key=lambda item: item["id"])
    result.fingerprint = digest(content)
    return result


def read_definition(db, reference: str) -> t.ApprovalDefinition:
    code = template_reference(reference)
    if settings().app_mode == "demo":
        row = db.get(ApprovalTemplate, code)
        if not row:
            raise DomainError("演示模式只能读取已有演示模板；目标公司的真实模板请在真实接入环境读取")
        form = []
        demo_types = {"amount": "amount", "expense_type": "radioV2", "description": "textarea",
                      "department": "department", "expected_payment_date": "date",
                      "payee_account": "account", "related_approvals": "connect"}
        seen = set()
        for key, value in (row.field_mapping or {}).items():
            for field_id in value if isinstance(value, list) else [value]:
                if field_id and field_id not in seen:
                    seen.add(field_id)
                    form.append({"id": field_id, "name": FIELD_LABELS.get(key, key),
                                 "type": "attachmentV2" if key == "attachment_ids" else demo_types.get(key, "input")})
        # 演示定义来自固定演示配置，不伪装成真实飞书的联调结果。
        raw = {"approval_name": row.name, "status": "ACTIVE", "form": form,
               "node_list": [{"node_id": node_id, "name": "AI 预审", "node_type": "AND"} for node_id in row.node_ids]}
        return normalize_definition(code, raw, demo=True)
    try:
        raw = FeishuGateway().definition(code)
    except DomainError:
        raise
    except Exception as exc:
        # SDK/网络异常原文可能含请求上下文，不传到浏览器和迁移文件。
        raise DomainError("暂时无法读取飞书审批定义，请检查网络、应用权限和模板可见范围") from exc
    return normalize_definition(code, raw)


def unique_match(source, candidates, *, field: bool = True):
    """仅唯一候选才推荐；自定义 ID 冲突时也不能回退为名称猜测。"""
    compatible = [item for item in candidates if item.type.lower() == source.type.lower()]
    if source.custom_id:
        custom = [item for item in candidates if item.custom_id == source.custom_id]
        if custom:
            return custom[0] if len(custom) == 1 and custom[0] in compatible else None
    named = [item for item in compatible if item.name == source.name and source.name
             and (not field or item.path == source.path)]
    return named[0] if len(named) == 1 else None


def recommendations(source: t.PortableTemplate | None, definition: t.ApprovalDefinition) -> tuple[s.FieldMapping, list[str], list[str]]:
    """推荐可唯一映射的字段与节点，多个源节点指向同一目标时留给管理员确认。"""
    mapping = s.FieldMapping()
    if source is None:
        return mapping, [], []
    source_fields = {item.id: item for item in source.fields}
    unresolved = []
    for key, value in source.field_mapping.model_dump().items():
        if not value:
            continue
        selected = []
        for source_id in value if isinstance(value, list) else [value]:
            descriptor = source_fields.get(source_id)
            matched = unique_match(descriptor, definition.fields) if descriptor else None
            if matched:
                selected.append(matched.id)
        expected = len(value) if isinstance(value, list) else 1
        if len(selected) != expected:
            unresolved.append(key)
        setattr(mapping, key, selected if isinstance(value, list) else (selected[0] if selected else ""))
    source_nodes = source.nodes or ([source.node] if source.node else [])
    candidates = [matched.id for node in source_nodes if (matched := unique_match(node, definition.nodes, field=False))]
    counts = Counter(candidates)
    node_ids = [node_id for node_id in candidates if counts[node_id] == 1]
    if not node_ids or len(node_ids) != len(source_nodes):
        unresolved.append("node_ids")
    return mapping, node_ids, unresolved


def config_revision(row: ApprovalTemplate | None) -> str:
    """仅检查会影响管理员决定的配置；同步游标推进不使预览无故过期。"""
    if row is None:
        return ""
    return digest(s.TemplateOut.model_validate(row).model_dump(mode="json", exclude={"sync_cursor"}))


def validate_bundle(bundle: t.TemplateBundle):
    if bundle.schema_version not in {1, 2}:
        raise DomainError("不支持此配置包版本，当前支持 schema_version=1 或 2")
    codes = [item.source_code for item in bundle.templates]
    if len(codes) != len(set(codes)):
        raise DomainError("配置包中的源模板编码重复")
    for item in bundle.templates:
        if (item.prompt_source == "OVERRIDE") != bool(item.agent_prompt.strip()):
            raise DomainError(f"模板「{item.name}」的提示词与继承状态不一致")
        if len({field.id for field in item.fields}) != len(item.fields):
            raise DomainError(f"模板「{item.name}」含重复源控件 ID")
        if bundle.schema_version == 2 and not item.nodes:
            raise DomainError(f"模板「{item.name}」缺少源专用预审节点")
        if len({node.id for node in item.nodes}) != len(item.nodes):
            raise DomainError(f"模板「{item.name}」含重复源节点 ID")


def export_bundle(db, codes: list[str]) -> t.TemplateBundle:
    if len(codes) != len(set(codes)):
        raise DomainError("请勿重复选择导出的模板")
    exported = []
    for code in codes:
        row = db.get(ApprovalTemplate, code)
        if not row:
            raise DomainError("要导出的模板不存在，请刷新列表")
        # 先复制本地配置；远端读取失败仍能交付配置，源 ID 只作为匹配索引。
        config = s.TemplateOut.model_validate(row)
        fields, definition_nodes, warning = [], [], ""
        try:
            definition = read_definition(db, code)
            fields, definition_nodes = definition.fields, definition.nodes
        except DomainError as exc:
            warning = f"源定义未读取：{exc}；导入时请手工绑定"
        nodes = {}
        for node_id in config.node_ids:
            node = next((item for item in definition_nodes if node_id in {item.id, item.custom_id}), None)
            if node is None:
                node = t.DefinitionNode(id=node_id, name="")
                warning = warning or "部分源预审节点未在当前定义中找到，请手工选择对应的目标节点"
            nodes[node.id] = node
        exported.append(t.PortableTemplate(
            source_code=config.code, name=config.name, business_type=config.business_type,
            field_mapping=config.field_mapping, rules=config.rules, agent_prompt=config.agent_prompt,
            prompt_source="OVERRIDE" if config.agent_prompt.strip() else "INHERIT",
            fields=fields, nodes=list(nodes.values()), definition_warning=warning))
    return t.TemplateBundle(schema_version=2, exported_at=now().replace(tzinfo=timezone.utc), templates=exported)


def checked_bindings(data: t.TemplatePreviewInput) -> dict[str, t.TemplateBinding]:
    validate_bundle(data.bundle)
    codes = {item.source_code for item in data.bundle.templates}
    bindings = {item.source_code: item for item in data.bindings}
    if len(bindings) != len(data.bindings):
        raise DomainError("同一个源模板不能重复提交绑定")
    if set(bindings) - codes:
        raise DomainError("目标绑定引用了配置包中不存在的源模板")
    target_codes = [template_reference(item.target_reference) for item in data.bindings
                    if item.selected and item.target_reference.strip()]
    if len(set(target_codes)) != len(target_codes):
        raise DomainError("多个条目绑定了同一个目标模板编码，请分别指定目标模板或取消选择")
    return bindings


def preview_bundle(db, data: t.TemplatePreviewInput) -> t.TemplatePreviewOut:
    bindings = checked_bindings(data)
    items = []
    cache = {}
    for source in data.bundle.templates:
        binding = bindings.get(source.source_code, t.TemplateBinding(source_code=source.source_code)).model_copy(deep=True)
        result = t.TemplatePreviewItem(source_code=source.source_code, binding=binding)
        if source.definition_warning:
            result.warnings.append(source.definition_warning)
        if source.prompt_source == "INHERIT":
            result.warnings.append("此模板继承目标系统的默认审核要求，请确认下方目标默认提示词")
        if binding.target_reference.strip():
            try:
                code = template_reference(binding.target_reference)
                if code not in cache:
                    cache[code] = read_definition(db, code)
                definition = cache[code]
                row = db.get(ApprovalTemplate, code)
                binding.target_reference = code
                binding.field_mapping, node_ids, result.unresolved_fields = recommendations(source, definition)
                # 旧版单节点向导可继续编辑 v1 预览中的单值，新版提交时可显式提供整个列表。
                binding.node_ids = node_ids if data.bundle.schema_version == 2 else None
                binding.node_id = node_ids[0] if len(node_ids) == 1 else ""
                binding.node_confirmed = False
                binding.definition_fingerprint = definition.fingerprint
                binding.expected_revision = config_revision(row)
                result.definition, result.conflict = definition, row is not None
                if "node_ids" in result.unresolved_fields:
                    result.warnings.append("请核对并选择目标模板中的预审节点，部分源节点无法唯一匹配")
                if not binding.name:
                    binding.name = source.name
                if row is not None and binding.conflict_action == "SKIP":
                    result.warnings.append("目标模板已存在，默认跳过；覆盖前请明确选择覆盖")
                if definition.status and definition.status.upper() != "ACTIVE":
                    result.warnings.append("目标飞书模板当前未启用，请在飞书后台确认状态")
            except DomainError as exc:
                result.error = str(exc)
                binding.definition_fingerprint = ""
                binding.expected_revision = None
        items.append(result)
    return t.TemplatePreviewOut(items=items)


def validate_target(source: t.PortableTemplate, binding: t.TemplateBinding, definition: t.ApprovalDefinition) -> s.TemplateInput:
    """校验最终人工绑定，而非再次把源 ID 当作目标值；清除可选字段必须显式确认。"""
    if not binding.definition_fingerprint or binding.definition_fingerprint != definition.fingerprint:
        raise DomainError(f"模板「{source.name}」的目标定义已变化或尚未预览，请重新读取并匹配")
    if binding.expected_revision is None:
        raise DomainError(f"模板「{source.name}」尚未完成目标预览")
    nodes = {node.id for node in definition.nodes}
    node_ids = binding.node_ids if binding.node_ids is not None else [binding.node_id.strip()]
    if not binding.node_confirmed or not node_ids or not set(node_ids) <= nodes:
        raise DomainError(f"模板「{source.name}」需要确认目标专用预审节点")
    reviewer = binding.reviewer_open_id.strip()
    valid_reviewer = re.fullmatch(r"ou_[A-Za-z0-9]+", reviewer)
    if settings().app_mode == "demo":
        valid_reviewer = valid_reviewer or reviewer.startswith("demo-")
    if not valid_reviewer:
        raise DomainError(f"模板「{source.name}」请重新填写目标应用中的审批人 open_id")
    fields = {field.id: field for field in definition.fields}
    mapping = binding.field_mapping.model_dump()
    source_mapping = source.field_mapping.model_dump()
    cleared = set(binding.cleared_fields)
    if cleared - set(FIELD_LABELS) or "amount" in cleared:
        raise DomainError("只能明确清除可选映射，申请金额必须绑定")
    if not mapping["amount"]:
        raise DomainError(f"模板「{source.name}」必须映射申请金额控件")
    for key, value in mapping.items():
        if value and key in cleared:
            raise DomainError(f"模板「{source.name}」的{FIELD_LABELS[key]}不能同时绑定和清除")
        if source_mapping[key] and not value and key not in cleared:
            raise DomainError(f"模板「{source.name}」的{FIELD_LABELS[key]}需要重新绑定或明确清除")
        for target_id in value if isinstance(value, list) else ([value] if value else []):
            if target_id not in fields:
                raise DomainError(f"模板「{source.name}」的{FIELD_LABELS[key]}不属于当前目标定义")
            if key == "attachment_ids" and fields[target_id].type.lower() not in ATTACHMENT_TYPES:
                raise DomainError("票据附件只能绑定附件或图片控件")
        if isinstance(value, list) and len(value) != len(set(value)):
            raise DomainError("票据附件控件不能重复选择")
    # 部分附件匹配成功也不能悄悄丢掉其余源附件；减少数量需明确确认重新选择范围。
    if (source_mapping["attachment_ids"] and mapping["attachment_ids"]
            and len(mapping["attachment_ids"]) < len(source_mapping["attachment_ids"])
            and not binding.attachment_scope_confirmed):
        raise DomainError("源模板的部分附件尚未匹配，请补齐附件控件，或明确改为读取全部附件")
    return s.TemplateInput(code=definition.code, name=binding.name.strip() or source.name,
                           business_type=source.business_type, node_ids=node_ids,
                           reviewer_open_id=reviewer, field_mapping=binding.field_mapping, rules=source.rules,
                           agent_prompt=source.agent_prompt, mode="OBSERVE", active=False)


def import_bundle(db, data: t.TemplatePreviewInput, actor: str) -> t.TemplateImportOut:
    bindings = checked_bindings(data)
    sources = {item.source_code: item for item in data.bundle.templates}
    prepared, skipped, cache = [], [], {}
    for code, source in sources.items():
        binding = bindings.get(code)
        if binding is None or not binding.selected:
            skipped.append(code)
            continue
        target_code = template_reference(binding.target_reference)
        row = db.get(ApprovalTemplate, target_code)
        if row is not None and binding.conflict_action == "SKIP":
            skipped.append(code)
            continue
        if target_code not in cache:
            cache[target_code] = read_definition(db, target_code)
        config = validate_target(source, binding, cache[target_code])
        user = db.get(User, config.reviewer_open_id)
        if user is not None and not user.enabled:
            raise DomainError("选定的目标审批人账号已停用，请重新选择")
        prepared.append((binding, config))
    if not prepared:
        return t.TemplateImportOut(templates=[], skipped=skipped)
    # 此前所有飞书读取已经完成。拿写锁后重新核对本地配置，整批原子保存。
    begin_write(db)
    saved = []
    for binding, config in prepared:
        row = db.get(ApprovalTemplate, config.code)
        if row is not None and binding.conflict_action == "SKIP":
            skipped.append(binding.source_code)
            continue
        if config_revision(row) != binding.expected_revision:
            raise DomainError("目标模板配置已被修改，请重新预览后再导入")
        user = db.get(User, config.reviewer_open_id)
        if user is not None and not user.enabled:
            raise DomainError("选定的目标审批人账号已停用，请重新选择")
        saved.append(apply_template_config(db, config, actor, importing=True))
    db.commit()
    return t.TemplateImportOut(templates=[s.TemplateOut.model_validate(row) for row in saved], skipped=skipped)
