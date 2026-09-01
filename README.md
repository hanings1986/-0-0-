# ⚽ Soccer Score Monitor

ESPN 足球 0:0 预警机器人 — 运行于 GitHub Actions，通知经由钉钉推送。

---

## 功能说明

| 触发条件 | 动作 |
|----------|------|
| 比赛进行至第 **70 分钟**，比分仍 **0:0** | 发第一条钉钉通知 |
| 比赛进行至第 **80 分钟**，比分仍 **0:0** | 发第二条钉钉通知 |
| 中间已有进球（比分不再是 0:0）| 自动跳过，不打扰 |

监控时段：北京时间 **06:00 ~ 23:00**

---

## 三级联赛优先级

**核心逻辑：有高级别赛事时，忽略低级别赛事，避免信息轰炸。**

```
有 Tier 1 比赛？
  ├─ YES → 只监控 Tier 1
  └─ NO ──▶ 有 Tier 2 比赛？
              ├─ YES → 只监控 Tier 2
              └─ NO ──▶ 监控所有 Tier 3
```

### Tier 1 — 顶级（始终优先）
**欧洲五大联赛：** 英超 / 西甲 / 德甲 / 意甲 / 法甲

**欧洲杯赛：** UEFA 冠军联赛 / 欧联杯 / 欧协联杯 / 欧洲杯 / 世界杯 / UEFA 国家联赛

**亚洲顶级：** 中超 / 日本 J1 联赛 / 韩国 K 联赛 1 / 澳大利亚 A-League

**亚洲最高级别杯赛：** AFC 亚冠 / AFC 亚洲杯

### Tier 2 — 次主流（Tier 1 空档期启用）
葡超 / 荷甲 / 苏超 / 土超 / 比甲 / 俄超

CONMEBOL 美洲杯 / CONCACAF 金杯赛 / 非洲杯 / 国际友谊赛

日本 J2 / 韩国 K 联赛 2（次级联赛兜底）

### Tier 3 — 其他（两者皆无时兜底）
所有其他联赛

> **凌晨赛事过滤：** 北京时间 00:00 ~ 05:59 开赛的比赛，无论什么级别，一律不纳入监控，不会打扰你睡眠。

---

## 🚀 GitHub 部署（详细步骤）

### 第一步：创建仓库
1. 登录 GitHub → 点击右上角 **"+"** → **New repository**
2. 仓库名：`soccer-monitor`（或其他你喜欢的名字）
3. **不要**勾选 "Initialize with README"
4. 点击 **Create repository**

### 第二步：上传文件
在你的电脑上打开 PowerShell：

```powershell
# 1. 进入项目文件夹
cd "c:\Users\28957\WorkBuddy\20260330092731"

# 2. 初始化 git（如果还没做）
git init

# 3. 添加所有文件
git add .

# 4. 提交
git commit -m "部署足球比分监控"

# 5. 连接到你的 GitHub 仓库（替换 YOUR_USERNAME）
git remote add origin https://github.com/YOUR_USERNAME/soccer-monitor.git

# 6. 推送
git push -u origin main
```

### 第三步：配置钉钉 Webhook
1. 进入仓库 → **Settings** → **Secrets and variables** → **Actions**
2. 点击 **New repository secret**
3. 填写：
   - **Name**: `DINGTALK_WEBHOOK`
   - **Value**: `https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN_HERE`
4. 点击 **Add secret**

### 第四步：测试运行
1. 进入仓库 → **Actions**（顶部选项卡）
2. 点击 **Soccer Score Monitor**
3. 点击 **Run workflow** → **Run workflow**
4. 等待 30 秒查看绿色对勾 ✅

> 详细问题排查见 [GITHUB_SETUP.md](GITHUB_SETUP.md)

---

## 本地测试

```bash
pip install requests
python soccer_monitor.py
```

---

## 文件结构

```
soccer-monitor/
├── soccer_monitor.py              ← 主监控脚本（含三级优先级）
├── requirements.txt               ← 依赖（仅 requests）
├── README.md                      ← 本文件
└── .github/workflows/
    └── soccer-monitor.yml         ← GitHub Actions 定时触发（每5分钟）
```
