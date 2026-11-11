/** 飞书控件的 text 还可能是 JSON 字符串；只解码对象、数组及带引号的文本，保留长账号原文。 */
function decodeField(value: unknown): unknown {
  for (let depth = 0; depth < 5 && typeof value === "string"; depth += 1) {
    const text = value.trim();
    if (!text.startsWith("{") && !text.startsWith("[") && !text.startsWith('"')) break;
    try {
      value = JSON.parse(text) as unknown;
    } catch {
      return undefined;
    }
  }
  return value;
}

/** 优先读取显示名称，递归处理地区列表及嵌套控件；未知对象不输出 JSON 或对象字符串。 */
function fieldText(value: unknown, keys: readonly string[] = ["name", "text", "label", "value"], depth = 0): string {
  if (depth > 10) return "";
  const decoded = decodeField(value);
  if (typeof decoded === "string") return decoded.trim();
  if (typeof decoded === "number") return String(decoded);
  if (Array.isArray(decoded)) return decoded.map(item => fieldText(item, keys, depth + 1)).filter(Boolean).join("、");
  if (decoded && typeof decoded === "object") {
    const record = decoded as Record<string, unknown>;
    for (const key of keys) {
      const text = fieldText(record[key], keys, depth + 1);
      if (text) return text;
    }
  }
  return "";
}

/** 将飞书个人／对公账户控件转换为带中文标签的多行文本，不修改接口保存的原始账户信息。 */
export function paymentAccountText(value: unknown): string {
  const account = decodeField(value);
  if (account == null || account === "") return "—";
  if (typeof account !== "object") return fieldText(account) || "—";
  if (Array.isArray(account)) return account.map(paymentAccountText).filter(text => text !== "—").join("\n\n") || "—";

  const record = account as Record<string, unknown>;
  const fields = [
    { key: "widgetAccountName", label: "户名" },
    { key: "widgetAccountNumber", label: "账号" },
    { key: "widgetAccountBankName", label: "开户银行", keys: ["bankNameZh", "name", "text", "label", "bankNameEn", "value"] },
    { key: "widgetAccountBankBranch", label: "开户支行", keys: ["name", "bankBranchNameZh", "text", "label", "bankBranchNameEn", "value"] },
    { key: "widgetAccountBankArea", label: "开户地区" },
    { key: "widgetAccountType", label: "账户类型", keys: ["text", "name", "label", "value"] },
  ];
  const lines = fields.flatMap(({ key, label, keys }) => {
    const text = fieldText(record[key], keys);
    return text ? [`${label}：${text}`] : [];
  });
  return lines.join("\n") || fieldText(account) || (Object.keys(record).length ? "收款信息格式暂无法识别，请到飞书核对" : "—");
}
