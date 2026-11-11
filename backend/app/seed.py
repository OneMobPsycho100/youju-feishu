"""独立演示数据。仅在 APP_MODE=demo 下创建，原件为标明演示用途的 XML。"""
from datetime import timedelta
from decimal import Decimal
from xml.sax.saxutils import escape
from sqlalchemy import select
from .config import settings
from .audit_policy import default_builtin_rules
from .db import begin_write
from .models import Approval, ApprovalTemplate, Company, User, now
from .services import archive_approval, sync_instance
from .storage import save_original

DEMO_COMPANIES = [
    ("demo-cloud", "云序科技有限公司", "91310000DEMO00001X"),
    ("demo-info", "云序信息技术有限公司", "91310000DEMO00002X"),
    ("demo-digital", "云序数字科技有限公司", "91310000DEMO00003X"),
]


def seed_demo(db):
    if settings().app_mode != "demo":
        return
    if db.get(ApprovalTemplate, "DEMO-EXPENSE"):
        from .reimbursement_metadata import ensure_demo_metadata
        ensure_demo_metadata(db)
        return
    begin_write(db)
    for role, name in [("admin", "林予安"), ("finance", "陈予宁"), ("viewer", "周清和")]:
        db.add(User(id=f"demo-{role}", name=name, role=role))
    for company_id, name, tax_id in DEMO_COMPANIES:
        db.add(Company(id=company_id, name=name, tax_id=tax_id))
    template = ApprovalTemplate(code="DEMO-EXPENSE", name="日常费用报销", node_id="demo-audit-node", node_ids=["demo-audit-node"],
        reviewer_open_id="demo-reviewer", field_mapping={"amount": "amount", "company_tax_id": "buyer", "expense_type": "expense", "description": "reason", "attachment_ids": []},
        rules={"version": "1.0", "amount_tolerance": "0.01", "require_semantic": False,
               "policy_schema_version": 2, "builtin_rules": default_builtin_rules(), "custom_rules": []},
        subscribed=True, capabilities={"definition_read": True, "details_read": True, "files_downloaded": True, "node_matched": True})
    db.add(template)
    db.flush()
    specs = [
        ("上海客户拜访 · 差旅报销", "林知夏", "商务拓展部", "差旅费", [3820], [0], "APPROVED", 18),
        ("产品设计协作工具续费", "沈亦舟", "产品设计部", "软件服务费", [1280], [0], "PENDING", 0),
        ("北京项目交付 · 住宿及交通", "许言", "项目交付部", "差旅费", [6200], [1], "PENDING", 1),
        ("十月办公用品集中采购", "江楠", "行政管理部", "办公费", [865], [3], "PENDING", 0),
        ("多主体研发设备耗材采购", "顾云帆", "技术研发部", "办公费", [1180, 890], [0, 2], "PENDING", 0),
        ("客户拜访补充费用报销", "陆可", "商务拓展部", "差旅费", [3820], [0], "PENDING", 0),
        ("团队季度交流活动餐费", "陈沐", "人力资源部", "业务招待费", [1560], [1], "PENDING", 2),
        ("研发环境云资源采购", "周清和", "技术研发部", "软件服务费", [9600, 2400], [0, 2], "APPROVED", 7),
        ("深圳客户项目交通费用", "温舒", "项目交付部", "差旅费", [740], [1], "PENDING", 0),
        ("年度财务资料装订采购", "陈予宁", "财务管理部", "办公费", [426], [0], "APPROVED", 12),
        ("项目宣传物料制作", "白予", "市场品牌部", "市场推广费", [5680], [2], "APPROVED", 25),
        ("办公空间网络服务费", "江楠", "行政管理部", "软件服务费", [1890], [1], "APPROVED", 4),
    ]
    for index, (title, applicant, department, expense, amounts, subjects, status, days) in enumerate(specs):
        approval_id = f"demo-approval-{index+1:03d}"
        total = sum(amounts) + (380 if index == 2 else 0)
        files = []
        for item_index, (amount, subject) in enumerate(zip(amounts, subjects)):
            buyer = DEMO_COMPANIES[subject] if subject < 3 else ("unknown", "云序创意工作室", "91310000DEMO99999X")
            serial = 1 if index == 5 else (index+1)*10+item_index
            if index == 0:
                serial = 1
            number = f"26{serial:018d}"
            issue = (now() - timedelta(days=days+1)).date().isoformat()
            fields = {"InvoiceNumber": number, "InvoiceCode": "", "BuyerName": buyer[1], "BuyerTaxID": buyer[2],
                      "SellerName": "演示供应商 · " + expense, "IssueDate": issue, "TotalAmountWithTax": f"{amount:.2f}"}
            if index == 6:
                fields["BuyerTaxID"] = ""  # 用于演示人工纠正与重审。
            if index == 5:
                fields["IssueDate"] = (now() - timedelta(days=19)).date().isoformat()
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n<DemoInvoice demo="true">' + ''.join(
                f"<{key}>{escape(value)}</{key}>" for key, value in fields.items()) + "<Remark>仅用于财务工作台功能演示，不具备票据效力</Remark></DemoInvoice>"
            saved = save_original(xml.encode(), f"演示电子发票-{number}.xml")
            files.append({"key": f"demo-{item_index}", "filename": saved["filename"], "demo_storage_key": saved["storage_key"]})
        form = [{"id": "amount", "value": str(total)}, {"id": "reason", "value": title}, {"id": "expense", "value": expense}]
        form.extend({"id": "buyer", "value": DEMO_COMPANIES[subject][2]} for subject in subjects if subject < 3)
        snapshot = {"approval_code": template.code, "instance_code": approval_id, "serial_number": f"BX-{now():%Y%m}-{index+1:04d}",
            "approval_name": title, "applicant_name": applicant, "department_name": department,
            "status": status, "form": form, "demo_files": files,
            "task_list": [{"id": f"task-{index+1}", "node_id": template.node_id, "open_id": template.reviewer_open_id,
                           "status": "APPROVED" if status == "APPROVED" else "PENDING"}]}
        db.add(Approval(id=approval_id, template_code=template.code, serial_number=snapshot["serial_number"],
            title=title, applicant=applicant, department=department, amount=Decimal(total), external_snapshot=snapshot,
            created_at=now()-timedelta(days=days, hours=index)))
    db.commit()
    # 新模板默认由财务人工预审；仍同步演示原件和审批状态供操作。
    from .reimbursement_metadata import ensure_demo_metadata
    ensure_demo_metadata(db)
    for index, spec in enumerate(specs):
        approval_id = f"demo-approval-{index+1:03d}"
        if spec[6] == "APPROVED":
            archive_approval(db, approval_id)
        else:
            sync_instance(db, approval_id)
