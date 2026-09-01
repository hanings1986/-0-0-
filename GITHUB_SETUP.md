# GitHub 部署指南 - 足球比分监控

## 快速设置脚本（Windows PowerShell）

```powershell
# 1. 进入项目文件夹
cd "c:\Users\28957\WorkBuddy\20260330092731"

# 2. 初始化 git（如果还没做）
git init

# 3. 添加所有文件
git add .

# 4. 提交
git commit -m "部署足球比分监控"

# 5. 连接到你的 GitHub 仓库（替换 USERNAME）
git remote add origin https://github.com/YOUR_USERNAME/soccer-monitor.git

# 6. 推送
git push -u origin main
```

---

## 如果遇到错误...

### 错误1：提示"Authentication failed"
```powershell
# 生成 Personal Access Token (PAT)：
# 1. 登录 GitHub → Settings → Developer settings → Personal access tokens
# 2. 点击 Generate new token
# 3. 勾选 "repo" 权限
# 4. 复制生成的 token（比如 ghp_xxxxxxxxxxxxx）

# 用 token 重新连接
git remote set-url origin https://ghp_xxxxxxxxxxxxx@github.com/YOUR_USERNAME/soccer-monitor.git
git push
```

### 错误2：提示"main branch does not exist"
```powershell
# 如果你创建仓库时勾选了 "Initialize with README"
git pull origin main --allow-unrelated-histories
git push
```

---

## 配置 Secrets（关键一步）

1. 打开浏览器，登录 GitHub
2. 进入你的仓库：`https://github.com/YOUR_USERNAME/soccer-monitor`
3. 点击 **Settings** (顶部选项卡)
4. 左侧选择 **Secrets and variables** → **Actions**
5. 点击 **New repository secret**
6. 填写：
   - **Name**: `DINGTALK_WEBHOOK`
   - **Value**: `https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN_HERE`
7. 点击 **Add secret**

---

## 测试运行

1. 进入仓库 → **Actions** (顶部选项卡)
2. 点击 **Soccer Score Monitor**
3. 点击 **Run workflow** → **Run workflow**
4. 等待 30-60 秒查看结果

**成功标志**：
- ✅ 看到绿色对勾
- ✅ 日志显示 "足球比分监控（三级优先）"
- ✅ 没有红色错误

---

## 运行时间说明

脚本会在以下时间自动运行：
- **北京时间 06:00 - 23:59**：每5分钟运行一次
- **凌晨 00:00 - 05:59**：不运行（让你好好睡觉）

实际运行时间表：
- 06:00, 06:05, 06:10, ... 22:55, 23:00
- 如果有比赛进行到 70/80 分钟仍 0:0，你会收到钉钉通知

---

## 检查运行状态

1. 随时进入仓库的 **Actions** 页面
2. 点击最新的运行记录
3. 查看 "运行足球比分监控" 步骤的日志
4. 日志会显示：
   - 今天有多少场比赛
   - 有多少场正在进行
   - 凌晨赛事排除情况
   - Tier 分布情况

---

## 问题排查

### 1. 收不到钉钉通知？
- 检查 Secrets 配置是否正确
- 确认钉钉机器人还在线
- 查看 Actions 日志，是否显示"钉钉通知已发送"

### 2. 运行频率不对？
- 等待 GitHub Actions 触发（可能有几分钟延迟）
- 可以手动运行一次测试

### 3. 想修改监控时间？
编辑 `.github/workflows/soccer-monitor.yml` 文件中的 `cron` 设置。

---

## GitHub 支持邮箱
如果遇到 GitHub 平台问题：`support@github.com`

## 钉钉机器人问题
如果钉钉通知异常，检查机器人 Webhook URL 是否有效。