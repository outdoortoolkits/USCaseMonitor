# USCaseMonitor

面向移民中介和律所的服务器端 USCIS 案件进度与签约节点提醒工具。企业员工录入客户及多个 Receipt Number，系统通过 USCIS 官方 Case Status API 定期查询，保存历史快照，并将状态变化或时间节点邮件发送给企业内部收件人。

## 已实现

- 客户及一对多申请案件管理
- Receipt Number 格式校验、搜索和手动查询
- USCIS OAuth 2.0 Client Credentials 及 Case Status API
- 原始响应、规范化状态和历史快照保存
- 首次查询建立基线，后续变化才触发通知
- 状态变化、指定状态、相对日期、单次日期、周期提醒
- 事件幂等，避免服务重启造成重复邮件
- 163 SMTP SSL 发信及最多三次尝试
- 独立定时 Worker、批量容错及查询日志
- FastAPI 管理页面、管理员登录
- Web 系统配置页面，API/SMTP 密钥加密保存且不回显
- SQLite 本地开发及 PostgreSQL Docker 部署

## 安全准备

之前在聊天中出现过的 USCIS Secret、邮箱密码和 SMTP 授权码必须先撤销并重新生成。项目中只能使用新凭据。邮箱网页登录密码不需要配置。

```bash
cp .env.example .env
```

编辑 `.env`，至少修改：

```env
APP_ENV=production
SECRET_KEY=使用 openssl rand -hex 32 生成
ADMIN_USERNAME=admin
ADMIN_PASSWORD=强密码
POSTGRES_PASSWORD=数据库强密码

USCIS_BASE_URL=https://api-int.uscis.gov
USCIS_CLIENT_ID=新的ClientID
USCIS_CLIENT_SECRET=新的Secret

SMTP_HOST=smtp.163.com
SMTP_PORT=465
SMTP_USE_SSL=true
SMTP_USERNAME=发件邮箱
SMTP_AUTH_CODE=新生成的SMTP授权码
DEFAULT_NOTIFY_EMAILS=企业收件人1,企业收件人2
```

Sandbox 使用 `https://api-int.uscis.gov` 和官方测试编号。通过 USCIS 生产审核后，将 `USCIS_BASE_URL` 更换为官方发放的生产地址。

## Docker 服务器部署

服务器需安装 Docker Engine 与 Compose 插件：

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

应用仅监听服务器回环地址 `127.0.0.1:8000`。生产环境应在前方配置 Nginx/Caddy、域名和 HTTPS，不能直接把 8000 端口暴露到公网。

查看日志：

```bash
docker compose logs -f web worker
```

备份数据库：

```bash
chmod +x scripts/backup.sh
./scripts/backup.sh
```

建议使用系统定时任务每天执行备份，并将加密备份复制到独立存储。

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

另开终端运行调度器：

```bash
source .venv/bin/activate
python -m app.worker
```

打开 <http://127.0.0.1:8000>，使用 `.env` 中的管理员账号登录。

登录后进入“系统配置”，可填写 USCIS API、163 SMTP 和企业收件人。Client Secret 与 SMTP 授权码使用 `SECRET_KEY` 派生的密钥加密保存在数据库；保存后不会在页面回显。请勿在保存过配置后随意修改 `SECRET_KEY`，否则旧配置将无法解密。

## 提醒规则说明

- `STATUS_CHANGED`：官方状态文本发生变化时触发。
- `STATUS_MATCHED`：变化后的标准状态匹配 `APPROVED/RFE/DENIED` 等标签时触发。
- `RELATIVE_DATE`：签约日或收据日加指定天数，仅触发一次。
- `ONE_TIME_DATE`：在指定日期触发一次。
- `RECURRING`：从基准日期和偏移日开始，每 N 天触发。

模板变量：`name`、`form_type`、`receipt_number`、`status_title`、`status_description`、`status_tag`、`receipt_date`、`sign_date`。

## 上线检查

1. 重置所有曾暴露的凭据。
2. 修改默认管理员密码及 `SECRET_KEY`。
3. 配置 HTTPS、防火墙和服务器时区。
4. 先用 USCIS Sandbox 测试编号验证成功及错误响应。
5. 给发件邮箱自身发送一封测试邮件。
6. 验证首次查询不发信、第二次相同状态不发信、变化后只发一次。
7. 完成 USCIS 生产审核后再接入真实客户编号。
