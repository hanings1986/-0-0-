"""
ESPN 足球比分监控脚本 - 全赛事监控版本（含低级别联赛与友谊赛）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GitHub Actions 部署版

修正点：
  1. 移除 URL 默认值中的反引号（致命 bug，导致请求失败、钉钉收不到）
  2. 凭据不再硬编码，强制从 GitHub Secrets 注入
  3. 状态文件使用 repo 内固定路径，配合 actions/cache 持久化
  4. 推荐 cron 每 10 分钟运行一次（与 70→80 分钟窗口匹配）
  5. 半场/终场/加时状态解析改进
  6. 80 分钟预警逻辑修正：与 70 分钟结果解耦，避免"错过 70 分钟后 80 分钟也错过"
  7. 状态 TTL：12 小时未更新的记录自动清理
  8. 每条记录附带 last_seen，方便排查

监控逻辑（不变）：
  - 仅监控北京时间 06:00 - 23:00 内开赛的比赛（凌晨赛事完全排除）
  - 三级联赛标签仅用于显示，所有进行中比赛全部纳入监控
  - 第 70 分钟，比分仍 0:0 → 发第一条钉钉 + AI 分析
  - 第 80 分钟，比分仍 0:0 → 发第二条钉钉 + AI 分析
  - 状态持久化，避免重复发送
"""

import requests
import sys
import json
import os
import re
import math
import time
from datetime import datetime, timedelta, timezone

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────
# 配置（凭据从环境变量读取，GitHub Actions 通过 secrets 注入）
# ──────────────────────────────────────────────────────────────

DINGTALK_WEBHOOK = os.environ.get("DINGTALK_WEBHOOK", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"

# ESPN 会拦截数据中心 IP（GitHub Actions），采用多端点回退链 + 完整浏览器请求头
# ⚠️ all/scoreboard 默认只返回 100 场（接口硬上限），必须带 limit 参数，否则当天比赛多时会静默漏掉
#    低级别联赛（实测某日默认100场 vs limit=400 共233场，其中进行中比赛漏掉23场）
SCOREBOARD_URLS = [
    ("site.api", "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?limit=400"),
    ("site.web.api", "https://site.web.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?limit=400"),
    ("cdn", "https://cdn.espn.com/core/soccer/scoreboard?limit=400"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Referer": "https://www.espn.com/soccer/scoreboard",
    "Origin": "https://www.espn.com",
    "Connection": "keep-alive",
}

ALERT_MINUTE_1 = 70
ALERT_MINUTE_2 = 80
# 情报搜集节点：65 分钟仍 0:0 → 后台搜集联赛/球队/比赛数据，供 70 分钟预警一并推送
INTEL_COLLECT_MINUTE = 65

# 比赛详情 summary 端点（含实时统计 / 联赛信息 / 两队近 5 场）
SUMMARY_URLS = [
    "https://site.web.api.espn.com/apis/site/v2/sports/soccer/all/summary?event={eid}",
    "https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary?event={eid}",
]

# 联赛进球画像在单次运行内的缓存（同一联赛多场比赛只抓一次）
_LEAGUE_PROFILE_CACHE = {}

# 大学生联赛剔除名单：通过 event.uid（如 s:600~l:5487~e:401886390）提取联赛 ID 匹配
# 5487 = NCAA Men's Soccer (usa.ncaa.m.1) | 5499 = NCAA Women's Soccer (usa.ncaa.w.1)
EXCLUDED_LEAGUE_IDS = {"5487", "5499"}

ACTIVE_HOUR_START = 6
ACTIVE_HOUR_END = 23

# 状态文件路径：使用 repo 根目录下的 state 子目录，配合 actions/cache 持久化
_STATE_DIR = os.environ.get("SOCCER_STATE_DIR", os.path.join(os.getcwd(), "state"))
try:
    os.makedirs(_STATE_DIR, exist_ok=True)
except Exception:
    pass
STATE_FILE = os.environ.get("SOCCER_STATE_FILE", os.path.join(_STATE_DIR, "soccer_state.json"))
# 赛果回溯样本库（联赛命中率统计）与健康自检状态
RESULTS_FILE = os.path.join(_STATE_DIR, "soccer_results.json")
HEALTH_FILE = os.path.join(_STATE_DIR, "health.json")

STATE_TTL_HOURS = 12

# ──────────────────────────────────────────────────────────────
# 三级联赛优先级配置
# ──────────────────────────────────────────────────────────────

TIER_1_LEAGUES = {
    "english premier league", "premier league",
    "spanish primera division", "la liga", "primera division",
    "german bundesliga", "bundesliga",
    "italian serie a", "serie a",
    "french ligue 1", "ligue 1",
    "uefa champions league", "champions league",
    "uefa europa league", "europa league",
    "uefa europa conference league", "conference league",
    "fifa world cup", "world cup",
    "uefa european championship", "euro ",
    "uefa nations league",
    "chinese super league", "china super league", "csl",
    "j1 league", "j.league", "明治安田生命j1",
    "k league 1", "k-league 1", "korean k league",
    "a-league", "a league", "australian a-league",
    "afc champions league elite", "afc champions league", "afc champions",
    "afc asian cup", "asian cup",
}

TIER_2_LEAGUES = {
    "portuguese primeira liga", "primeira liga",
    "dutch eredivisie", "eredivisie",
    "scottish premiership",
    "turkish super lig", "super lig",
    "belgian first division", "jupiler pro league",
    "russian premier", "российская премьер-лига",
    "conmebol copa america", "copa america",
    "concacaf gold cup", "gold cup",
    "africa cup of nations", "afcon",
    "international friendly",
    "j2 league", "k league 2",
}


def classify_league(league_name: str) -> int:
    if not league_name:
        return 3
    name_lower = league_name.lower()
    for kw in TIER_1_LEAGUES:
        if kw in name_lower:
            return 1
    for kw in TIER_2_LEAGUES:
        if kw in name_lower:
            return 2
    return 3


# ──────────────────────────────────────────────────────────────
# 钉钉通知
# ──────────────────────────────────────────────────────────────

def send_dingtalk(title: str, message: str) -> bool:
    if not DINGTALK_WEBHOOK:
        print("[ERROR] 未配置 DINGTALK_WEBHOOK 环境变量，无法发送钉钉")
        return False
    payload = {
        "msgtype": "text",
        "text": {"content": f"{title}\n{message}"}
    }
    try:
        resp = requests.post(DINGTALK_WEBHOOK, json=payload, timeout=10)
        result = resp.json()
        if result.get("errcode") == 0:
            print(f"[OK] 钉钉通知已发送: {title}")
            return True
        else:
            print(f"[ERROR] 钉钉错误: {result.get('errmsg')} | code={result.get('errcode')}")
            return False
    except Exception as e:
        print(f"[ERROR] 钉钉发送异常: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# DeepSeek AI 比赛分析
# ──────────────────────────────────────────────────────────────

TIER_LABEL = {1: "⭐⭐⭐ 顶级联赛", 2: "⭐⭐ 主流联赛", 3: "⭐ 一般联赛"}


def build_analysis_prompt(game: dict, intel: dict | None = None,
                          base_rate: str | None = None) -> str:
    """构造 AI 分析 prompt。

    注意：原脚本用 f-string 内嵌 "{1-2句综合判断}" 字面量会触发 Python 解析错误。
    本版改用字符串拼接，避免此类语法陷阱。
    """
    home = game["home_team"]
    away = game["away_team"]
    # "all" 比分板不带联赛名，优先用情报包里的联赛名
    league = game["league"] or (intel or {}).get("league", {}).get("name") or "未知联赛"
    score = f"{game['home_score']}:{game['away_score']}"
    minute = game["display_clock"] or f"{game['minute']}'"
    tier_label = TIER_LABEL.get(game.get("tier", 3), "")

    parts = [
        "你是一名足球赛事数据分析师，专精「特殊概念比赛」识别与概率评估。",
        "",
        "当前比赛信息：",
        f"- 联赛：{league}（{tier_label}）",
        f"- 对阵：{home} vs {away}",
        f"- 当前比分：{score}",
        f"- 比赛时间：第 {minute}",
        "- 赛事状态：进行中，当前仍为 0:0",
    ]

    # 注入系统自动搜集的实时情报
    if intel:
        parts.append("")
        parts.append("## 0. 系统自动搜集的实时数据情报（可信度高于你的记忆，请优先基于这些数据推理）")
        ms = intel.get("match_stats")
        if ms:
            parts.append("本场实时统计：")
            for name, s in ms.items():
                items = ", ".join(f"{k} {v}" for k, v in s.items())
                parts.append(f"- {name}: {items}")
        lgp = intel.get("league_goals")
        if lgp:
            parts.append(f"- 联赛进球画像: 近期 {lgp['matches']} 场完赛, 场均 {lgp['avg_goals']} 球, "
                         f"大2.5球比率 {lgp['over25_rate']}%（{lgp['tag']}）")
        tf_map = intel.get("team_form")
        if tf_map:
            label = {"home": "主队", "away": "客队"}
            for ha in ("home", "away"):
                tf = tf_map.get(ha)
                if not tf:
                    continue
                rows_str = "; ".join(
                    f"{r['date'][5:]} {r['res']} {r['gf']}-{r['ga']} vs {r['opp']}" for r in tf["rows"]
                )
                parts.append(f"- {label.get(ha, ha)} {tf['team_name']} 近5场: "
                             f"{tf['W']}胜{tf['D']}平{tf['L']}负, 场均进{tf['gf_avg']}失{tf['ga_avg']}, "
                             f"零封{tf['clean_sheets']}场, 有进球{tf['scored']}/5场 ({rows_str})")
        if intel.get("threat"):
            parts.append(f"- {intel['threat']}")
        if intel.get("tempo"):
            parts.append(f"- 节奏变化: {intel['tempo']}")
        if intel.get("incidents"):
            parts.append(f"- 关键事件: {'; '.join(intel['incidents'])}")
    if base_rate:
        parts.append(f"- {base_rate}")

    parts += [
        "",
        "请针对这场 0:0 比赛，按以下固定流程输出分析：",
        "",
        "## 1. 赛程扫描",
        "列出本场对阵、联赛、当前时间。",
        "",
        "## 2. 概念挖掘",
        "分析本场是否存在以下特殊统计概念：",
        "- 球队连胜/连败/连续不胜纪录",
        "- 球队连续多场进球≥2、连续被零封数据",
        "- 球队主场/客场连续固定赛果走势",
        "- 核心球员连续进球、助攻纪录",
        "- 两队历史交锋极端统计规律",
        "- 积分榜关键节点：争冠、保级、欧战资格线附近",
        "",
        "## 3. 多维数据验证",
        "基于你的知识分析：",
        "- 主客场战绩",
        "- 近期 5 场走势",
        "- 交锋历史",
        "- 场地环境、伤病/体能/多线作战隐患",
        "",
        "## 4. 概率评估",
        "- 各赛果概率占比（主胜/平局/客胜）",
        "- 总进球区间判断",
        "",
        "## 5. 赛事走势参考",
        "- 胜负格局倾向",
        "- 比分区间参考",
        "",
        "## 6. 多场组合推演（本场独推）",
        "针对本场比赛提供：",
        "- 【常规基本面组合推演】",
        "- 【冷门走势组合推演】",
        "",
        "## 7. 联赛专属分析提醒",
        f"结合 {league} 的联赛特点给出针对性赛事变量提醒。",
        "",
        "注意：所有分析仅作赛事数据、球队基本面推演参考，不涉及任何投注、购彩相关引导内容。输出格式结构化、分点清晰。",
        "",
        "最后给出 1-2 句综合判断。",
    ]
    return "\n".join(parts)


def get_ai_analysis(game: dict, intel: dict | None = None,
                    base_rate: str | None = None) -> str | None:
    if not DEEPSEEK_API_KEY:
        print("[AI] 未配置 DEEPSEEK_API_KEY，跳过 AI 分析")
        return None

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
        )

        prompt = build_analysis_prompt(game, intel, base_rate)

        print(f"[AI] 正在请求 DeepSeek 分析: {game['home_team']} vs {game['away_team']}...")

        response = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=2000,
        )

        analysis = response.choices[0].message.content
        print(f"[AI] DeepSeek 分析完成，长度: {len(analysis)} 字符")
        return analysis

    except ImportError:
        print("[AI] openai 库未安装，跳过 AI 分析")
        return None
    except Exception as e:
        print(f"[AI] DeepSeek 调用异常: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# 状态管理（带 TTL 清理）
# ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print("[WARN] 状态文件结构异常，重置为空状态")
            return {}
        return data
    except Exception as e:
        print(f"[WARN] 读取状态文件失败: {e}，使用空状态")
        return {}


def save_state(state: dict):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] 保存状态失败: {e}")


def cleanup_stale_state(state: dict) -> bool:
    """清理过期记录（> TTL 未更新）。返回是否有清理。"""
    now = datetime.now(timezone.utc)
    removed = []
    for eid in list(state.keys()):
        rec = state[eid]
        ts_str = rec.get("last_seen") or rec.get("alert_80_time") or rec.get("alert_70_time")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (now - ts).total_seconds() / 3600
            if age_hours > STATE_TTL_HOURS:
                removed.append(eid)
        except Exception:
            continue

    for eid in removed:
        del state[eid]

    if removed:
        print(f"[CLEANUP] 已清理 {len(removed)} 条过期记录: {removed}")
        return True
    return False


# ──────────────────────────────────────────────────────────────
# ESPN API & 比赛解析
# ──────────────────────────────────────────────────────────────

def get_scoreboard() -> dict | None:
    """依次尝试多个 ESPN 端点，返回第一个含 events 的 JSON"""
    for name, url in SCOREBOARD_URLS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"[ESPN] 端点 {name} 返回 {resp.status_code}，尝试下一个...")
                continue
            data = resp.json()
            if not isinstance(data, dict) or "events" not in data:
                print(f"[ESPN] 端点 {name} 响应缺少 events 字段，尝试下一个...")
                continue
            print(f"[ESPN] 使用端点 {name}，获取 {len(data.get('events', []))} 场比赛")
            return data
        except Exception as e:
            print(f"[ESPN] 端点 {name} 异常: {e}，尝试下一个...")
    print("[ERROR] 所有 ESPN 端点均失败")
    return None


def _parse_minute(clock_str: str, period: int, state: str = "") -> int:
    """更稳健的分钟数解析。"""
    s = (clock_str or "").strip()
    s_lower = s.lower()

    if state in ("post", "final"):
        return 90
    if s_lower in ("ht", "halftime") or "halftime" in s_lower:
        return 45
    # period: 1=上半场 2=下半场 3=加时上半 4=加时下半 5=点球
    if period and period >= 3:
        return 105 if period == 3 else 120

    if not s:
        return 45 if period and period >= 2 else 0

    # 去除所有撇号，处理 "45'+3'" 这类格式
    s = s.replace("'", "").strip()

    if "+" in s:
        try:
            parts = s.split("+")
            base = int(parts[0])
            added = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            return base + added
        except Exception:
            pass

    if ":" in s:
        try:
            return int(s.split(":")[0])
        except Exception:
            pass

    try:
        return int(s)
    except Exception:
        return 0 if not period or period <= 1 else 45


def _extract_league_id(event: dict) -> str:
    """从 event.uid（如 s:600~l:5487~e:401886390）提取联赛 ID"""
    m = re.search(r"~l:(\d+)~", event.get("uid") or "")
    return m.group(1) if m else ""


def parse_game(event: dict) -> dict:
    league_name = ""
    if isinstance(event.get("league"), dict):
        league_name = (
            event["league"].get("shortName")
            or event["league"].get("name")
            or ""
        )
    comps = event.get("competitions", [])
    if not league_name and comps:
        comp_league = comps[0].get("league", {}) if comps else {}
        if isinstance(comp_league, dict):
            league_name = comp_league.get("shortName") or comp_league.get("name") or ""

    result = {
        "event_id": event.get("id", ""),
        "event_name": event.get("name", ""),
        "state": "",
        "display_clock": "",
        "minute": 0,
        "home_score": 0,
        "away_score": 0,
        "home_team": "",
        "away_team": "",
        "league": league_name,
        "league_id": _extract_league_id(event),
        "tier": classify_league(league_name),
        "start_time_bj": None,
    }

    date_str = event.get("date", "")
    if date_str:
        try:
            dt_utc = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=timezone.utc)
            result["start_time_bj"] = dt_utc.astimezone(timezone(timedelta(hours=8)))
        except Exception:
            pass

    if not comps:
        return result

    comp = comps[0]
    status = comp.get("status", {})
    status_type = status.get("type", {})

    result["state"] = status_type.get("state", "")
    result["display_clock"] = status.get("displayClock", "") or ""
    result["minute"] = _parse_minute(
        result["display_clock"],
        status.get("period", 1),
        result["state"],
    )

    for c in comp.get("competitors", []):
        home_away = c.get("homeAway", "")
        team_name = (
            c.get("team", {}).get("shortDisplayName")
            or c.get("team", {}).get("displayName")
            or c.get("team", {}).get("name")
            or "?"
        )
        try:
            score = int(c.get("score", 0) or 0)
        except Exception:
            score = 0

        if home_away == "home":
            result["home_team"] = team_name
            result["home_score"] = score
        else:
            result["away_team"] = team_name
            result["away_score"] = score

    return result


def is_zero_zero(game: dict) -> bool:
    return game["home_score"] == 0 and game["away_score"] == 0


def is_in_active_hours(game: dict) -> bool:
    if game["start_time_bj"] is None:
        return True
    start_hour = game["start_time_bj"].hour
    return ACTIVE_HOUR_START <= start_hour < ACTIVE_HOUR_END


# ──────────────────────────────────────────────────────────────
# 情报搜集（65 分钟 0:0 触发，一次性）
# ──────────────────────────────────────────────────────────────

# 需要提取的实时统计项：ESPN 字段名 → 中文标签
_STAT_MAP = {
    "totalShots": "射门", "shotsOnTarget": "射正", "wonCorners": "角球",
    "possessionPct": "控球%", "foulsCommitted": "犯规", "yellowCards": "黄牌",
    "redCards": "红牌", "saves": "扑救", "offsides": "越位",
}


def _espn_first(urls: list[str]):
    """依次尝试多个 ESPN URL，返回第一个 200 的 JSON"""
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            print(f"[ESPN] {resp.status_code} ← {url[:90]}")
        except Exception as e:
            print(f"[ESPN] 异常 ← {url[:90]}: {e}")
    return None


def _match_stats_from_summary(summ: dict) -> dict | None:
    """从 summary 提取当前比赛实时统计（射门/射正/角球/控球等）"""
    teams = (summ.get("boxscore") or {}).get("teams") or []
    if not teams:
        return None
    out = {}
    for t in teams:
        name = (t.get("team") or {}).get("shortDisplayName") \
            or (t.get("team") or {}).get("displayName") or "?"
        stats = {s.get("name"): s.get("displayValue") for s in t.get("statistics", []) if s.get("name")}
        picked = {cn: stats[en] for en, cn in _STAT_MAP.items() if stats.get(en) not in (None, "")}
        if picked:
            out[name] = picked
    return out or None


def _team_form_from_summary(side: dict, team_id: str) -> dict | None:
    """从 lastFiveGames 提取单队近 5 场：胜负/进失球/零封/均场数据"""
    events = side.get("events", [])
    if not events:
        return None
    rows = []
    for e in events[:5]:
        try:
            h_id = str(e.get("homeTeamId"))
            is_home = h_id == str(team_id)
            gf = int(e.get("homeTeamScore") if is_home else e.get("awayTeamScore") or 0)
            ga = int(e.get("awayTeamScore") if is_home else e.get("homeTeamScore") or 0)
            opp = (e.get("opponent") or {}).get("displayName") or "?"
            rows.append({
                "date": (e.get("gameDate") or "")[:10],
                "opp": opp,
                "res": e.get("gameResult") or "?",   # W/D/L
                "gf": gf, "ga": ga,
                "comp": e.get("leagueName") or e.get("competitionName") or "",
            })
        except Exception:
            continue
    if not rows:
        return None
    return {
        "rows": rows,
        "W": sum(1 for r in rows if r["res"] == "W"),
        "D": sum(1 for r in rows if r["res"] == "D"),
        "L": sum(1 for r in rows if r["res"] == "L"),
        "gf_avg": round(sum(r["gf"] for r in rows) / len(rows), 1),
        "ga_avg": round(sum(r["ga"] for r in rows) / len(rows), 1),
        "clean_sheets": sum(1 for r in rows if r["ga"] == 0),
        "scored": sum(1 for r in rows if r["gf"] > 0),
        "btts": sum(1 for r in rows if r["gf"] > 0 and r["ga"] > 0),
    }


def _league_goals_profile(slug: str) -> dict | None:
    """近 12 天该联赛已完赛场次的进球画像：场均进球 + 大2.5球比率 → 大球/小球属性"""
    cache_key = (slug, datetime.now(timezone.utc).strftime("%Y%m%d"))
    if cache_key in _LEAGUE_PROFILE_CACHE:
        return _LEAGUE_PROFILE_CACHE[cache_key]

    totals = []
    for d in range(1, 13):
        day = datetime.now(timezone.utc) - timedelta(days=d)
        ds = day.strftime("%Y%m%d")
        url = f"https://site.web.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard?dates={ds}"
        data = _espn_first([url])
        if not data:
            continue
        for e in data.get("events", []):
            try:
                comp = e.get("competitions", [{}])[0]
                if comp.get("status", {}).get("type", {}).get("state") != "post":
                    continue
                scores = {c.get("homeAway"): c.get("score") for c in comp.get("competitors", [])}
                totals.append(int(scores.get("home") or 0) + int(scores.get("away") or 0))
            except Exception:
                continue
        if len(totals) >= 25:
            break

    if len(totals) < 5:
        return None
    n = len(totals)
    avg = sum(totals) / n
    over25 = sum(1 for t in totals if t >= 3) / n
    if avg >= 2.7 or over25 >= 0.55:
        tag = "🔥 大球联赛"
    elif avg <= 2.3 or over25 <= 0.35:
        tag = "🧊 小球联赛"
    else:
        tag = "⚖️ 中性联赛"
    profile = {
        "matches": n, "avg_goals": round(avg, 2),
        "over25_rate": round(over25 * 100), "tag": tag,
    }
    _LEAGUE_PROFILE_CACHE[cache_key] = profile
    return profile


def collect_intel(game: dict) -> dict:
    """主入口：为一场 0:0 比赛搜集完整情报包（单次调用）"""
    eid = game["event_id"]
    intel: dict = {}

    summ = _espn_first([u.format(eid=eid) for u in SUMMARY_URLS])
    if summ:
        intel["match_stats"] = _match_stats_from_summary(summ)
        intel["incidents"] = _incidents_from_summary(summ) or None

        lg = (summ.get("header") or {}).get("league") or {}
        intel["league"] = {"name": lg.get("shortName") or lg.get("name"), "slug": lg.get("slug")}

        # 两队近 5 场（lastFiveGames 顺序与 boxscore.teams 一致，用 team.id 对齐主/客）
        bteams = (summ.get("boxscore") or {}).get("teams") or []
        form = {}
        for side in (summ.get("lastFiveGames") or []):
            tid = str((side.get("team") or {}).get("id") or "")
            if not tid:
                continue
            ha = "away"
            for bt in bteams:
                if str((bt.get("team") or {}).get("id")) == tid:
                    ha = bt.get("homeAway") or "away"
                    break
            tf = _team_form_from_summary(side, tid)
            if tf:
                tf["team_name"] = (side.get("team") or {}).get("shortDisplayName") \
                    or (side.get("team") or {}).get("displayName") or tid
                form[ha] = tf
        intel["team_form"] = form or None

    slug = (intel.get("league") or {}).get("slug")
    if slug:
        intel["league_goals"] = _league_goals_profile(slug)

    threat = build_threat_line(intel.get("match_stats"), game.get("minute") or 65)
    if threat:
        intel["threat"] = threat

    intel["collected_at"] = datetime.now(timezone.utc).isoformat()
    return intel


def build_intel_text(intel: dict | None) -> str:
    """把情报包格式化为钉钉消息文本块（含威胁指数/节奏/关键事件）"""
    if not intel:
        return ""
    parts = []

    threat = intel.get("threat")
    if threat:
        parts.append(threat)

    ms = intel.get("match_stats")
    if ms:
        parts.append("📋 当前比赛实时数据")
        for name, s in ms.items():
            items = " ".join(f"{k} {v}" for k, v in s.items())
            parts.append(f"  {name}: {items}")

    tempo = intel.get("tempo")
    if tempo:
        parts.append(tempo)

    inc = intel.get("incidents")
    if inc:
        parts.append("⚠️ 关键事件（红牌/点球）")
        for x in inc:
            parts.append(f"  {x}")

    lg = intel.get("league") or {}
    lgp = intel.get("league_goals")
    if lg.get("name") or lgp:
        parts.append(f"🏟️ 联赛: {lg.get('name') or '?'}")
    if lgp:
        parts.append(f"  近期 {lgp['matches']} 场完赛: 场均进球 {lgp['avg_goals']} "
                     f"| 大2.5球比率 {lgp['over25_rate']}% → {lgp['tag']}")

    tf_map = intel.get("team_form")
    if tf_map:
        label = {"home": "主队", "away": "客队"}
        for ha in ("home", "away"):
            tf = tf_map.get(ha)
            if not tf:
                continue
            parts.append(
                f"📊 {label.get(ha, ha)} {tf['team_name']} 近5场: "
                f"{tf['W']}胜{tf['D']}平{tf['L']}负 | 场均进{tf['gf_avg']}失{tf['ga_avg']} "
                f"| 零封{tf['clean_sheets']} | 有进球场次 {tf['scored']}/5"
            )
            detail = " ".join(f"{r['res']}{r['gf']}-{r['ga']}{r['opp'][:6]}" for r in tf["rows"])
            parts.append(f"   明细: {detail}")

    return "\n".join(parts)


# ── 威胁指数：用实时统计估算累积进攻威胁（xG 简化代理） ──

def _stat_num(stats: dict, key: str) -> float:
    try:
        return float(stats.get(key) or 0)
    except Exception:
        return 0.0


def build_threat_line(match_stats: dict | None, minute: int) -> str | None:
    """威胁指数：射正×0.32 + 非射正射门×0.055 + 角球×0.035，泊松估算剩余时间破门倾向"""
    if not match_stats:
        return None
    xg = {}
    for team, s in match_stats.items():
        sot = _stat_num(s, "射正")
        off = max(0.0, _stat_num(s, "射门") - sot)
        xg[team] = sot * 0.32 + off * 0.055 + _stat_num(s, "角球") * 0.035
    total = sum(xg.values())
    if total <= 0:
        return None

    remaining = max(6.0, 92.0 - float(minute))
    p_goal = 1.0 - math.exp(-total * remaining / 90.0)

    if total >= 2.6:
        tone = "🔥 高危"
    elif total >= 1.4:
        tone = "⚠️ 中等"
    else:
        tone = "❄️ 沉闷"
    line = (f"⚔️ 威胁指数: {tone}（累积威胁度 {total:.1f}，"
            f"剩余时间破门倾向参考 ≈ {p_goal * 100:.0f}%）")

    ranked = sorted(xg.items(), key=lambda kv: kv[1], reverse=True)
    if ranked and len(ranked) > 1 and ranked[0][1] - ranked[1][1] >= 0.6:
        line += f" | 单边压制: {ranked[0][0]}"
    return line


def build_tempo_line(old_stats: dict | None, new_stats: dict | None) -> str | None:
    """节奏对比：新旧两次统计快照的增量"""
    if not old_stats or not new_stats:
        return None
    parts = []
    delta_shots = delta_sot = 0.0
    for team, s_new in new_stats.items():
        s_old = old_stats.get(team) or {}
        d_shots = _stat_num(s_new, "射门") - _stat_num(s_old, "射门")
        d_sot = _stat_num(s_new, "射正") - _stat_num(s_old, "射正")
        d_cor = _stat_num(s_new, "角球") - _stat_num(s_old, "角球")
        delta_shots += max(0.0, d_shots)
        delta_sot += max(0.0, d_sot)
        segs = []
        if d_shots > 0:
            segs.append(f"射门+{int(d_shots)}")
        if d_sot > 0:
            segs.append(f"射正+{int(d_sot)}")
        if d_cor > 0:
            segs.append(f"角球+{int(d_cor)}")
        if segs:
            parts.append(f"{team}: " + " ".join(segs))
    if not parts:
        return None
    tone = "⚡ 节奏明显提速" if (delta_sot >= 1 or delta_shots >= 4) else "📈 持续施压"
    return f"🏃 {tone}: " + " | ".join(parts)


def _incidents_from_summary(summ: dict) -> list[str]:
    """从 keyEvents 提取红牌/点球关键事件"""
    out = []
    for e in (summ.get("keyEvents") or []):
        try:
            ttxt = (e.get("type") or {}).get("text") or ""
            etxt = e.get("text") or ""
            low = (ttxt + " " + etxt).lower()
            clock = (e.get("clock") or {}).get("displayValue") or ""
            if "red" in low and "card" in low:
                out.append(f"🟥 红牌 {clock}: {etxt or ttxt}")
            elif "penalty" in low:
                scored = bool(e.get("scoringPlay"))
                out.append(f"⚽ 点球{'命中' if scored else '未中'} {clock}: {etxt or ttxt}")
        except Exception:
            continue
    return out[:6]


def refresh_live_details(eid: str, intel: dict, minute: int) -> None:
    """预警时刷新实时统计 + 关键事件，计算节奏对比与最新威胁指数"""
    summ = _espn_first([u.format(eid=eid) for u in SUMMARY_URLS])
    if not summ:
        return
    fresh = _match_stats_from_summary(summ)
    if fresh:
        old = intel.get("match_stats")
        tempo = build_tempo_line(old, fresh)
        if tempo:
            intel["tempo"] = tempo
        intel["match_stats"] = fresh
    inc = _incidents_from_summary(summ)
    if inc:
        intel["incidents"] = inc
    threat = build_threat_line(intel.get("match_stats"), minute)
    if threat:
        intel["threat"] = threat


# ── 赛果回溯 + 联赛命中率 ──

def load_json_file(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[WARN] 读取 {os.path.basename(path)} 失败: {e}")
    return default


def save_json_file(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"[WARN] 保存 {os.path.basename(path)} 失败: {e}")


def league_base_rate_text(results: list, league_id: str) -> str | None:
    """该联赛 70分钟0:0 的历史命中率（本系统累积样本）"""
    if not league_id:
        return None
    rows = [r for r in results if r.get("league_id") == str(league_id)]
    n = len(rows)
    if n == 0:
        return None
    goals = sum(1 for r in rows if r.get("goal_after_70"))
    if n >= 5:
        return f"📈 实证参考: 该联赛近 {n} 场 70分钟0:0 → 最终破门 {goals} 场 ({goals * 100 // n}%)"
    return f"📈 实证参考: 该联赛样本累积中（破门 {goals}/{n}）"


def resolve_finals(state: dict, event_index: dict, results: list) -> int:
    """回查已发 70 分钟预警比赛的终场比分：发赛后回报 + 累积命中率样本"""
    newly = 0
    for eid, rec in state.items():
        if not isinstance(rec, dict) or not rec.get("alert_70_sent"):
            continue
        if rec.get("final_score") or rec.get("final_unknown"):
            continue

        final = None
        ev = event_index.get(eid)
        if ev is not None:
            comp = ev.get("competitions", [{}])[0]
            st = (comp.get("status") or {}).get("type", {}).get("state")
            if st == "post":
                sc = {c.get("homeAway"): c.get("score") for c in comp.get("competitors", [])}
                final = f"{sc.get('home') or 0}:{sc.get('away') or 0}"
        else:
            # 比赛已从今日比分板消失 → 用 summary 回查（最多重试 3 次）
            summ = _espn_first([u.format(eid=eid) for u in SUMMARY_URLS])
            if summ:
                comp = ((summ.get("header") or {}).get("competitions") or [{}])[0]
                st = (comp.get("status") or {}).get("type", {}).get("state")
                if st == "post":
                    sc = {c.get("homeAway"): c.get("score") for c in comp.get("competitors", [])}
                    final = f"{sc.get('home') or 0}:{sc.get('away') or 0}"
            if final is None:
                rec["final_tries"] = rec.get("final_tries", 0) + 1
                if rec["final_tries"] >= 3:
                    rec["final_unknown"] = True
                    print(f"[回溯] {rec.get('game_name')} 终场比分无法回查，已放弃")
                continue

        if final is None:
            continue

        try:
            h, _, a = final.partition(":")
            total = int(float(h)) + int(float(a))
        except Exception:
            continue

        rec["final_score"] = final
        rec["final_recorded_at"] = datetime.now(timezone.utc).isoformat()
        outcome = (f"终场比分 {final}（70 分钟后有进球）" if total > 0
                   else f"终场比分 {final}（0:0 保持到最后）")
        send_dingtalk(f"[足球预警-赛后回报] {rec.get('game_name') or eid}",
                      f"📋 赛后回报\n{'-' * 30}\n{outcome}\n"
                      f"已计入联赛命中率样本库")
        results.append({
            "league_id": str(rec.get("league_id") or ""),
            "league_name": (rec.get("intel") or {}).get("league", {}).get("name")
                            or str(rec.get("league_id") or "?"),
            "match": rec.get("game_name") or eid,
            "final": final,
            "goal_after_70": total > 0,
            "date": (rec.get("first_seen") or "")[:10],
            "recorded_at": rec["final_recorded_at"],
        })
        newly += 1
        print(f"[回溯] {rec.get('game_name')} → {outcome}")

    if len(results) > 800:
        del results[:len(results) - 800]
    return newly


# ── 补漏扫描：对已结束比赛回查 70/80 分钟时刻比分，补发漏掉的预警 ──
# 背景：GitHub Actions cron 可能数小时不执行，比赛在两轮之间从 in → post 时会被永久跳过。
# 本函数每轮对已 post 的比赛用 keyEvents 首球时间判定 70/80 分钟时刻是否 0:0，补发预警。

BACKSTOP_MAX_AGE_HOURS = 8       # 只处理开赛 8 小时内的比赛（更早的视为历史，不重复扫）
BACKSTOP_SEND_WINDOW_HOURS = 4   # 结束超过 4 小时：只记样本库，不再发消息（避免刷屏）
BACKSTOP_MAX_SENDS = 12          # 单轮补发条数上限
BACKSTOP_MAX_TRIES = 3           # summary 拉取失败重试上限


def _first_goal_minute(summ: dict) -> float | None:
    """从 keyEvents 找第一个进球的比赛分钟数；无进球返回 None"""
    best = None
    for e in (summ.get("keyEvents") or []):
        try:
            t = ((e.get("type") or {}).get("text") or "").lower().strip()
            if not ((t.startswith("goal") and "kick" not in t)
                    or t in ("own goal", "penalty - scored") or t.startswith("goal -")):
                continue
            s = ((e.get("clock") or {}).get("displayValue") or "").replace("'", "").strip()
            if "+" in s:
                p = s.split("+")
                m = int(p[0]) + int(p[1] or 0)
            elif s:
                m = int(s)
            else:
                m = None
            if m is not None and (best is None or m < best):
                best = m
        except Exception:
            continue
    return best


def _sample_exists(results: list, match_name: str, final: str) -> bool:
    return any(r.get("match") == match_name and r.get("final") == final for r in results)


def backstop_scan(state: dict, all_games: list, results: list) -> int:
    """对已结束但 70/80 分钟窗口未处理的比赛回查补发。返回本轮补发条数。"""
    now_utc = datetime.now(timezone.utc)
    sends = 0
    resolved = 0

    for g in all_games:
        if g["state"] != "post" or not is_in_active_hours(g):
            continue

        eid = g["event_id"]
        rec = state.get(eid)
        if rec is None:
            # 从未进入过监控的比赛（整场比赛落在两轮运行之间）
            if g["start_time_bj"] is None:
                continue
            try:
                start_age_h = (now_utc - g["start_time_bj"].astimezone(timezone.utc)).total_seconds() / 3600
            except Exception:
                start_age_h = 99.0
            if start_age_h > BACKSTOP_MAX_AGE_HOURS:
                continue  # 陈旧且无记录：静默跳过（不建记录，避免每轮重复抓 summary）
            rec = state[eid] = {
                "alert_70_sent": False, "alert_80_sent": False,
                "game_name": g["event_name"], "tier": g["tier"],
                "league_id": g["league_id"], "first_seen": now_utc.isoformat(),
                "backstop_created": True,
            }

        w70 = bool(rec.get("alert_70_sent") or rec.get("alert_70_skipped"))
        w80 = bool(rec.get("alert_80_sent") or rec.get("alert_80_skipped"))
        if (w70 and w80) or rec.get("backstop_done"):
            continue
        if rec.get("backstop_tries", 0) >= BACKSTOP_MAX_TRIES:
            rec["backstop_done"] = True
            continue

        summ = _espn_first([u.format(eid=eid) for u in SUMMARY_URLS])
        if not summ:
            rec["backstop_tries"] = rec.get("backstop_tries", 0) + 1
            print(f"    [补漏] {g['event_name']} summary 拉取失败 "
                  f"({rec['backstop_tries']}/{BACKSTOP_MAX_TRIES})")
            continue

        fg = _first_goal_minute(summ)
        was70 = fg is None or fg >= 70
        was80 = fg is None or fg >= 80

        # 终场比分（summary 优先，比分板兜底）
        final = None
        try:
            comp = ((summ.get("header") or {}).get("competitions") or [{}])[0]
            if (comp.get("status") or {}).get("type", {}).get("state") == "post":
                sc = {c.get("homeAway"): c.get("score") for c in comp.get("competitors", [])}
                final = f"{sc.get('home') or 0}:{sc.get('away') or 0}"
        except Exception:
            final = None
        if final is None:
            final = f"{g['home_score']}:{g['away_score']}"

        # 决定各窗口的处理方式
        newly = []          # 需要补发的窗口
        if not w70:
            if not was70:
                rec["alert_70_skipped"] = "scored"
            elif rec.get("alert_80_sent"):
                rec["alert_70_sent"] = True   # 80 分钟预警已覆盖，无需重复补发
            else:
                newly.append(70)
        if not w80:
            if not was80:
                rec["alert_80_skipped"] = "scored"
            else:
                newly.append(80)

        resolved += 1
        if not newly:
            # 记终场 + 样本（不涉及发送）
            rec["final_score"] = final
            rec["final_recorded_at"] = now_utc.isoformat()
            rec["backstop_done"] = True
            try:
                h, _, a = final.partition(":")
                total = int(float(h)) + int(float(a))
                name = rec.get("game_name") or g["event_name"]
                if not _sample_exists(results, name, final):
                    results.append({
                        "league_id": str(rec.get("league_id") or ""),
                        "league_name": (rec.get("intel") or {}).get("league", {}).get("name")
                                        or str(rec.get("league_id") or "?"),
                        "match": name, "final": final,
                        "goal_after_70": total > 0,
                        "date": (rec.get("first_seen") or "")[:10],
                        "recorded_at": rec["final_recorded_at"],
                        "backstop": True,
                    })
            except Exception:
                pass
            continue

        try:
            start_age_h = (now_utc - g["start_time_bj"].astimezone(timezone.utc)).total_seconds() / 3600 \
                if g["start_time_bj"] else 99.0
        except Exception:
            start_age_h = 99.0

        if start_age_h > BACKSTOP_SEND_WINDOW_HOURS:
            # 结束太久：只记样本，不刷屏
            rec["final_score"] = final
            rec["final_recorded_at"] = now_utc.isoformat()
            rec["backstop_done"] = True
            rec["backstop_expired"] = True
            if 70 in newly:
                rec["alert_70_sent"] = True
                rec["alert_70_time"] = now_utc.isoformat()
            if 80 in newly:
                rec["alert_80_sent"] = True
                rec["alert_80_time"] = now_utc.isoformat()
            try:
                h, _, a = final.partition(":")
                total = int(float(h)) + int(float(a))
                name = rec.get("game_name") or g["event_name"]
                if not _sample_exists(results, name, final):
                    results.append({
                        "league_id": str(rec.get("league_id") or ""),
                        "league_name": str(rec.get("league_id") or "?"),
                        "match": name, "final": final,
                        "goal_after_70": total > 0,
                        "date": (rec.get("first_seen") or "")[:10],
                        "recorded_at": rec["final_recorded_at"],
                        "backstop": True, "expired": True,
                    })
            except Exception:
                pass
            print(f"    [补漏] {g['event_name']} 第{ '/'.join(str(x) for x in newly) }分钟时刻 0:0，"
                  f"已结束较久 → 仅记入样本库")
            continue

        if sends >= BACKSTOP_MAX_SENDS:
            print(f"    [补漏] 本轮补发已达上限 {BACKSTOP_MAX_SENDS} 条，"
                  f"{g['event_name']} 留待下轮")
            continue

        # 组装补发消息（含情报与威胁指数）
        intel = rec.get("intel")
        if intel is None:
            intel = {
                "match_stats": _match_stats_from_summary(summ),
                "incidents": _incidents_from_summary(summ) or None,
            }
            lg = (summ.get("header") or {}).get("league") or {}
            intel["league"] = {"name": lg.get("shortName") or lg.get("name")}
            threat = build_threat_line(intel.get("match_stats"), 90)
            if threat:
                intel["threat"] = threat
            rec["intel"] = intel
        else:
            try:
                refresh_live_details(eid, intel, 90)
            except Exception:
                pass

        windows = "/".join(str(x) for x in newly)
        intel_text = build_intel_text(intel)
        base_rate = league_base_rate_text(results, rec.get("league_id"))
        now_bj = now_utc + timedelta(hours=8)
        title = (f"[足球预警-补发] {g['home_team']} vs {g['away_team']} "
                 f"— 第{windows}分钟时刻仍0:0")
        body = (
            f"🔁 补发预警（第{windows}分钟时刻比分 0:0）\n"
            f"{'=' * 40}\n"
            f"级别: {TIER_LABEL.get(g.get('tier', 3), '')}\n"
            f"联赛: {g['league'] or (intel.get('league') or {}).get('name') or '未知联赛'}\n"
            f"比赛: {g['event_name']}\n"
            f"终场比分: {final}（首个进球: {'无（全场0:0）' if fg is None else f'{int(fg)}分钟'}）\n"
            f"开赛时间: {g['start_time_bj'].strftime('%m-%d %H:%M') if g['start_time_bj'] else '未知'} (北京)\n"
            f"{'-' * 40}\n"
            f"说明: 系统当轮未能按时送达（调度延迟），现按存档数据补发，仅供复盘。\n"
            f"补发时间: {now_bj.strftime('%Y-%m-%d %H:%M:%S')} (北京)\n"
        )
        if intel_text:
            body += "\n" + intel_text
        if base_rate:
            body += "\n" + base_rate

        if send_dingtalk(title, body):
            if 70 in newly:
                rec["alert_70_sent"] = True
                rec["alert_70_time"] = now_utc.isoformat()
            if 80 in newly:
                rec["alert_80_sent"] = True
                rec["alert_80_time"] = now_utc.isoformat()
            rec["final_score"] = final
            rec["final_recorded_at"] = now_utc.isoformat()
            rec["backstop"] = True
            try:
                h, _, a = final.partition(":")
                total = int(float(h)) + int(float(a))
                name = rec.get("game_name") or g["event_name"]
                if not _sample_exists(results, name, final):
                    results.append({
                        "league_id": str(rec.get("league_id") or ""),
                        "league_name": (rec.get("intel") or {}).get("league", {}).get("name")
                                        or str(rec.get("league_id") or "?"),
                        "match": name, "final": final,
                        "goal_after_70": total > 0,
                        "date": (rec.get("first_seen") or "")[:10],
                        "recorded_at": rec["final_recorded_at"],
                        "backstop": True,
                    })
            except Exception:
                pass
            sends += 1
            print(f"    [补漏] 已补发 {g['event_name']}（第{windows}分钟窗口，终场 {final}）")
            time.sleep(1.2)  # 钉钉限流保护
        # 发送失败 → 不置标志，下轮重试

    if resolved:
        print(f"[补漏] 本轮回查 {resolved} 场已结束比赛，补发 {sends} 条")
    return sends


# ──────────────────────────────────────────────────────────────
# 通知消息格式
# ──────────────────────────────────────────────────────────────

def format_alert_message(game: dict, alert_minute: int, ai_analysis: str | None = None) -> tuple[str, str]:
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    start_str = (
        game["start_time_bj"].strftime("%Y-%m-%d %H:%M")
        if game["start_time_bj"] else "未知"
    )
    tier_str = TIER_LABEL.get(game.get("tier", 3), "")

    if alert_minute == ALERT_MINUTE_1:
        title = f"[足球预警] {game['home_team']} vs {game['away_team']} — 第70分钟仍0:0"
        emoji = "⚽"
        tip = "比赛已过70分钟，双方仍未破门！第一波预警。"
    else:
        title = f"[足球预警] {game['home_team']} vs {game['away_team']} — 第80分钟仍0:0"
        emoji = "🚨"
        tip = "比赛已过80分钟，比分依然0:0！最后悬念时刻！"

    body = (
        f"{emoji} 足球 0:0 预警\n"
        f"{'=' * 40}\n"
        f"级别: {tier_str}\n"
        f"联赛: {game['league'] or '未知联赛'}\n"
        f"比赛: {game['event_name']}\n"
        f"当前比分: {game['home_team']} {game['home_score']} : {game['away_score']} {game['away_team']}\n"
        f"当前时间: {game['display_clock']}\n"
        f"开赛时间: {start_str} (北京)\n"
        f"{'=' * 40}\n"
        f"{tip}\n"
        f"监控时间: {now_bj.strftime('%Y-%m-%d %H:%M:%S')} (北京)\n"
    )

    if ai_analysis:
        max_len = 15000
        if len(ai_analysis) > max_len:
            ai_analysis = ai_analysis[:max_len] + "\n\n[AI 分析内容过长，已截断]"
        body += (
            f"\n{'─' * 40}\n"
            f"🤖 DeepSeek AI 赛事分析\n"
            f"{'─' * 40}\n"
            f"{ai_analysis}"
        )

    return title, body


# ──────────────────────────────────────────────────────────────
# 主检测逻辑
# ──────────────────────────────────────────────────────────────

def monitor_once():
    now_bj = datetime.now(timezone.utc) + timedelta(hours=8)
    print("=" * 60)
    print(f"足球比分监控（全量模式）  {now_bj.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    print("=" * 60)

    if not DINGTALK_WEBHOOK:
        print("[FATAL] 未配置 DINGTALK_WEBHOOK 环境变量，无法发送通知")
        print("        请在 GitHub repo → Settings → Secrets and variables → Actions")
        print("        添加 Secret: DINGTALK_WEBHOOK")
        return

    if not (ACTIVE_HOUR_START <= now_bj.hour < ACTIVE_HOUR_END):
        print(f"[INFO] 当前 {now_bj.hour}:xx 不在监控时段 "
              f"({ACTIVE_HOUR_START}:00-{ACTIVE_HOUR_END}:00)，跳过")
        return

    # 健康自检：连续取不到数据时告警，恢复时通知
    health = load_json_file(HEALTH_FILE, {})
    board = get_scoreboard()
    if not board:
        streak = health.get("espn_fail_streak", 0) + 1
        health["espn_fail_streak"] = streak
        save_json_file(HEALTH_FILE, health)
        print(f"[ERROR] 无法获取比赛数据（连续第 {streak} 轮失败）")
        if streak >= 2 and (streak == 2 or streak % 10 == 0):
            send_dingtalk("[足球预警-系统告警] 监控数据异常",
                          f"⚠️ 系统自检\n{'-' * 30}\n"
                          f"ESPN 数据已连续 {streak} 轮获取失败，监控处于盲区。\n"
                          f"若持续发生，请到 GitHub Actions 查看运行日志排查。")
        return

    if health.get("espn_fail_streak", 0) >= 2:
        send_dingtalk("[足球预警-系统告警] 监控已恢复",
                      f"✅ 系统自检\n{'-' * 30}\n"
                      f"ESPN 数据已恢复正常（此前连续 {health['espn_fail_streak']} 轮失败）")
    health["espn_fail_streak"] = 0
    health["last_success"] = datetime.now(timezone.utc).isoformat()
    save_json_file(HEALTH_FILE, health)

    events = board.get("events", [])
    if not events:
        print("[INFO] 今天暂无足球比赛数据")
        return

    all_games = [parse_game(e) for e in events]

    # 剔除大学生联赛（NCAA 男足/女足）
    excluded_ncaa = [g for g in all_games if g.get("league_id") in EXCLUDED_LEAGUE_IDS]
    all_games = [g for g in all_games if g.get("league_id") not in EXCLUDED_LEAGUE_IDS]
    if excluded_ncaa:
        print(f"[INFO] 已剔除大学生联赛(NCAA) {len(excluded_ncaa)} 场")
        for g in excluded_ncaa[:6]:
            print(f"  [剔除NCAA] {g['event_name']}  {g['display_clock']} "
                  f"{g['home_score']}:{g['away_score']}")

    in_progress = [g for g in all_games if g["state"] == "in"]
    live_games = [g for g in in_progress if is_in_active_hours(g)]
    excluded = len(in_progress) - len(live_games)

    print(f"[INFO] 今日共 {len(all_games)} 场比赛，"
          f"进行中 {len(in_progress)} 场，"
          f"凌晨开赛已排除 {excluded} 场，"
          f"有效监控 {len(live_games)} 场")

    for g in in_progress:
        if not is_in_active_hours(g) and g["start_time_bj"]:
            print(f"  [凌晨排除] {g['start_time_bj'].strftime('%H:%M')} "
                  f"开赛 → {g['event_name']}")

    tier_counts = {1: 0, 2: 0, 3: 0}
    for g in live_games:
        tier_counts[g["tier"]] = tier_counts.get(g["tier"], 0) + 1
    print(f"[TIER分布] Tier1={tier_counts[1]}场  "
          f"Tier2={tier_counts[2]}场  "
          f"Tier3={tier_counts[3]}场")

    if not live_games:
        # 不提前 return：已结束比赛仍需补漏扫描 / 赛果回溯
        print("[INFO] 当前无进行中的比赛（仍执行补漏扫描）")

    state = load_state()
    state_changed = cleanup_stale_state(state)

    # 赛果样本库 + 今日事件索引（供赛果回溯 / 联赛命中率查询）
    results = load_json_file(RESULTS_FILE, [])
    if not isinstance(results, list):
        results = []
    event_index = {str(e.get("id")): e for e in events}

    now_iso = datetime.now(timezone.utc).isoformat()

    for game in live_games:
        eid = game["event_id"]
        ename = game["event_name"]
        minute = game["minute"]
        tier_str = TIER_LABEL.get(game["tier"], "")

        print(f"\n  [{tier_str}] [{game['league']}]")
        print(f"  {ename}  {game['display_clock']} | "
              f"比分: {game['home_score']}:{game['away_score']} | "
              f"解析分钟: {minute}'")

        if eid not in state:
            state[eid] = {
                "alert_70_sent": False,
                "alert_80_sent": False,
                "game_name": ename,
                "tier": game["tier"],
                "league_id": game["league_id"],
                "first_seen": now_iso,
            }
        rec = state[eid]
        rec["last_seen"] = now_iso
        rec["last_minute"] = minute
        rec["last_score"] = f"{game['home_score']}:{game['away_score']}"

        # 65 分钟节点：仍 0:0 → 后台搜集情报（每场只搜集一次，失败最多重试 2 次）
        if (minute >= INTEL_COLLECT_MINUTE and is_zero_zero(game)
                and rec.get("intel") is None and rec.get("intel_fail", 0) < 2):
            print(f"    [情报] 第 {minute}' 仍 0:0，开始后台搜集比赛/球队/联赛数据...")
            try:
                intel = collect_intel(game)
            except Exception as e:
                print(f"    [WARN] 情报搜集异常: {e}")
                intel = {}
            if intel.get("match_stats") or intel.get("team_form") or intel.get("league_goals"):
                rec["intel"] = intel
                rec["intel_collected_at"] = intel["collected_at"]
                print(f"    [OK] 情报搜集完成"
                      f"（实时统计={'有' if intel.get('match_stats') else '无'}"
                      f" 球队近况={'有' if intel.get('team_form') else '无'}"
                      f" 联赛画像={'有' if intel.get('league_goals') else '无'}）")
            else:
                rec["intel_fail"] = rec.get("intel_fail", 0) + 1
                print(f"    [WARN] 情报搜集失败第 {rec['intel_fail']} 次，下轮重试")
            state_changed = True

        # 70 分钟节点
        if minute >= ALERT_MINUTE_1 and not rec["alert_70_sent"]:
            if is_zero_zero(game):
                intel = rec.get("intel")
                if intel:
                    # 预警前刷新实时统计：对比 65 分钟快照 → 节奏变化 + 最新威胁指数/关键事件
                    try:
                        refresh_live_details(eid, intel, minute)
                    except Exception as e:
                        print(f"    [WARN] 实时统计刷新异常: {e}")
                base_rate = league_base_rate_text(results, rec.get("league_id"))
                intel_text = build_intel_text(intel)
                print(f"    [触发] 第 {minute}' 比分仍 0:0，发第一条预警..."
                      f"{'（含情报包）' if intel_text else '（情报未就绪，仅发基础预警）'}")
                title, body = format_alert_message(game, ALERT_MINUTE_1)
                if intel_text:
                    body += "\n\n" + intel_text
                if base_rate:
                    body += "\n" + base_rate
                if send_dingtalk(title, body):
                    rec["alert_70_sent"] = True
                    rec["alert_70_time"] = now_iso
                    state_changed = True
                    print(f"    [OK] 70 分钟预警已发送")
                    # AI 分析作为第二条消息（带上情报数据）
                    # 标题必须包含"足球预警"关键词，否则被钉钉机器人的关键词安全校验拦截(code=310000)
                    ai_analysis = get_ai_analysis(game, intel, base_rate)
                    if ai_analysis:
                        send_dingtalk(f"[足球预警-AI分析] {game['home_team']} vs {game['away_team']} (70')",
                                      ai_analysis[:3500])
                else:
                    print(f"    [WARN] 发送失败，下次重试")
            else:
                rec["alert_70_sent"] = True
                rec["alert_70_skipped"] = "scored"
                state_changed = True
                print(f"    [SKIP] 第 {minute}' 已有进球 "
                      f"({game['home_score']}:{game['away_score']})，跳过 70 分钟预警")

        # 80 分钟节点（与 70 分钟结果解耦，避免"错过 70 分钟后 80 分钟也错过"）
        if minute >= ALERT_MINUTE_2 and not rec["alert_80_sent"]:
            if is_zero_zero(game):
                intel = rec.get("intel")
                if intel:
                    # 预警前刷新实时统计：对比上次快照 → 节奏变化 + 最新威胁指数/关键事件
                    try:
                        refresh_live_details(eid, intel, minute)
                    except Exception as e:
                        print(f"    [WARN] 实时统计刷新异常: {e}")
                base_rate = league_base_rate_text(results, rec.get("league_id"))
                intel_text = build_intel_text(intel)
                print(f"    [触发] 第 {minute}' 比分仍 0:0，发第二条预警..."
                      f"{'（含情报包）' if intel_text else ''}")
                title, body = format_alert_message(game, ALERT_MINUTE_2)
                if intel_text:
                    body += "\n\n" + intel_text
                if base_rate:
                    body += "\n" + base_rate
                if send_dingtalk(title, body):
                    rec["alert_80_sent"] = True
                    rec["alert_80_time"] = now_iso
                    state_changed = True
                    print(f"    [OK] 80 分钟预警已发送")
                    ai_analysis = get_ai_analysis(game, intel, base_rate)
                    if ai_analysis:
                        send_dingtalk(f"[足球预警-AI分析] {game['home_team']} vs {game['away_team']} (80')",
                                      ai_analysis[:3500])
                else:
                    print(f"    [WARN] 发送失败，下次重试")
            else:
                rec["alert_80_sent"] = True
                rec["alert_80_skipped"] = "scored"
                state_changed = True
                print(f"    [SKIP] 80 分钟前已进球 "
                      f"({game['home_score']}:{game['away_score']})，跳过 80 分钟预警")

        flags = []
        flags.append("70':✅已处理" if rec["alert_70_sent"] else f"70':⏳等待(当前{minute}')")
        flags.append("80':✅已处理" if rec["alert_80_sent"] else f"80':⏳等待(当前{minute}')")
        print(f"    [状态] {' | '.join(flags)}")

    # 关注名单：仍 0:0 且接近触发节点的比赛
    watch = [g for g in live_games if is_zero_zero(g) and g["minute"] >= INTEL_COLLECT_MINUTE - 10]
    if watch:
        print(f"\n[关注名单] 仍 0:0 且已到 {INTEL_COLLECT_MINUTE - 10} 分钟+: {len(watch)} 场")
        for g in watch:
            rec_w = state.get(g["event_id"], {})
            if rec_w.get("alert_70_sent"):
                stage = "已发70分钟预警"
            elif rec_w.get("intel"):
                stage = "情报已就绪，等待70分钟"
            elif rec_w.get("alert_70_skipped") or rec_w.get("alert_80_skipped"):
                stage = "已处理"
            else:
                stage = "观察中"
            print(f"  👁️ {g['event_name']} ({g['minute']}') — {stage}")

    # 补漏扫描：对已结束比赛回查 70/80 分钟时刻比分，补发漏掉的预警
    try:
        backstop_scan(state, all_games, results)
        state_changed = True   # 补漏可能写入 skipped/sent/样本标记
    except Exception as e:
        print(f"[WARN] 补漏扫描异常: {e}")

    # 赛果回溯：已发 70 分钟预警的比赛回查终场比分，累积联赛命中率样本 + 赛后回报
    try:
        newly = resolve_finals(state, event_index, results)
        if newly:
            print(f"[回溯] 本轮新增 {newly} 条赛果样本 → {os.path.basename(RESULTS_FILE)}")
    except Exception as e:
        print(f"[WARN] 赛果回溯异常: {e}")

    save_json_file(RESULTS_FILE, results)   # 样本库每次落盘（文件很小）

    if state_changed:
        save_state(state)
        print(f"\n[INFO] 状态已保存 → {STATE_FILE}")

    print("\n" + "=" * 60)
    print(f"检测完成  {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
    print("=" * 60)


if __name__ == "__main__":
    try:
        monitor_once()
    except KeyboardInterrupt:
        print("\n监控已停止。")
    except Exception as e:
        print(f"[FATAL] 脚本异常: {e}")
        raise
