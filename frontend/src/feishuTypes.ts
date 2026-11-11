/** 接入配置的脱敏视图，三个密钥字段只有已设置标记，不返回明文或密文。 */
export interface FeishuConfiguration {
  version: number;
  connection_version: number;
  status: "UNCONFIGURED" | "ACTIVE";
  configured: boolean;
  source: "database" | "imported";
  app_id: string;
  tenant_key: string;
  public_url: string;
  event_transport: "websocket" | "http";
  app_secret_configured: boolean;
  encrypt_key_configured: boolean;
  verification_token_configured: boolean;
  credential_error: string;
  credential_verified_at: string | null;
  receiver_status: string;
  receiver_applied_version: number;
  receiver_heartbeat_at: string | null;
  receiver_error: string;
  last_event_at: string | null;
  oauth_callback_url: string;
  event_callback_url: string;
}

/** 密钥留空保持已有值；版本必须与读取时一致，App ID 在绑定后只读。 */
export interface FeishuConfigurationInput {
  version: number;
  app_id: string;
  app_secret: string;
  public_url: string;
  event_transport: "websocket" | "http";
  encrypt_key: string;
  verification_token: string;
}

export interface FeishuTestResult {
  credential_valid: boolean;
  message: string;
  checked_at: string;
}

/** 初始化会话仅能操作接入配置，不代表拥有工作台业务权限。 */
export interface SetupStatus {
  mode: "demo" | "live";
  setup_required: boolean;
  unlocked: boolean;
  purpose: "" | "INIT" | "RECOVERY";
  expires_at: string | null;
}
