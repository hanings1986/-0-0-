# -*- coding: utf-8 -*-
"""自链式触发：监控结束后睡到下一个 10 分钟整点边界，再 dispatch 下一轮。
公开仓库 Actions 分钟免费，链内休眠无成本。暂停方法：仓库变量 CHAIN_ENABLED 设为 off。"""
import os
import time
import requests
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("CHAIN_TOKEN", "")
API = "https://api.github.com/repos/hanings1986/-0-0-"
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {TOKEN}",
                  "Accept": "application/vnd.github+json",
                  "User-Agent": "soccer-chain"})


def log(m):
    print(f"[chain] {m}", flush=True)


def main():
    if not TOKEN:
        log("未配置 CHAIN_TOKEN，跳过链式")
        return

    # 停止开关
    try:
        r = S.get(f"{API}/actions/variables/CHAIN_ENABLED", timeout=15)
        if r.status_code == 200 and r.json().get("value", "").lower() in ("off", "false", "0"):
            log("CHAIN_ENABLED=off，本轮不再续链")
            return
    except Exception as e:
        log(f"开关检查异常（继续执行）: {e}")

    # 睡到下一个 10 分钟 UTC 边界 + 20s 缓冲
    now = datetime.now(timezone.utc)
    nxt = (now.replace(second=0, microsecond=0)
           + timedelta(minutes=10 - now.minute % 10)
           + timedelta(seconds=20))
    wait = (nxt - now).total_seconds()
    log(f"休眠 {wait:.0f}s 至 {nxt.strftime('%H:%M:%S')}Z")
    time.sleep(max(wait, 0))

    # 防重复：若已有较新且活跃的 run（本地触发器刚触发过），跳过本轮 dispatch
    try:
        r = S.get(f"{API}/actions/runs?per_page=1", timeout=15)
        runs = r.json().get("workflow_runs", [])
        if runs:
            last = runs[0]
            t = datetime.fromisoformat(last["created_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - t).total_seconds()
            if age < 480 and last["status"] in ("queued", "in_progress"):
                log(f"run#{last['run_number']} 仍活跃（{age:.0f}s），跳过 dispatch")
                return
    except Exception as e:
        log(f"查 run 异常（继续 dispatch）: {e}")

    for i in range(3):
        try:
            r = S.post(f"{API}/actions/workflows/main.yml/dispatches",
                       json={"ref": "main", "inputs": {"source": "chain"}}, timeout=15)
            if r.status_code == 204:
                log("已 dispatch 下一轮（source=chain）")
                return
            log(f"dispatch HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            log(f"dispatch 异常: {e}")
        time.sleep(10)
    log("重试 3 次仍失败，本轮断链（下轮 schedule/本地触发兜底）")


if __name__ == "__main__":
    main()
