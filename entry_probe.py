"""入口试验：复用上游登录和采集器，不下载媒体、不建立历史基线。"""
import argparse
import asyncio
import functools
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["DOUYIN_FAVORITES_PROFILE_DIR"] = str(ROOT / "runtime" / "browser-profile")

from douyin_favorites_knowledge import browser_collector
from douyin_favorites_knowledge.core_bridge import ProfileRegistry
from douyin_favorites_knowledge.security import safe_error_message

# 将上游的非敏感 profile 路径登记保存在本项目中。
browser_collector.ProfileRegistry = functools.partial(
    ProfileRegistry, path=ROOT / "runtime" / "profile-registry.json"
)


async def probe():
    collector = browser_collector.BrowserCollector(source="like")
    await collector.open(headless=True)
    try:
        await collector.navigate()
        if not await collector.authenticated():
            raise ValueError("login_required: 请先运行 login.cmd 完成登录")
        cursor = 0
        pages = []
        ids = []
        for index in range(2):
            response = await collector.fetch_page(cursor=cursor, count=20)
            if not response.get("ok"):
                diagnostics = {key: response.get(key) for key in ("error", "status_code", "http_status") if key in response}
                raise ValueError("喜欢列表入口失败: " + json.dumps(diagnostics, ensure_ascii=True))
            items = response.get("items")
            if not isinstance(items, list):
                raise ValueError("喜欢列表响应缺少有效 items")
            page_ids = [str(item.get("aweme_id", "")) for item in items]
            if any(not browser_collector.AWEME_ID.fullmatch(item) for item in page_ids):
                raise ValueError("喜欢列表返回无效视频 ID")
            ids.extend(page_ids)
            pages.append({"page": index + 1, "count": len(items), "has_more": bool(response.get("has_more"))})
            if not response.get("has_more"):
                break
            next_cursor = int(response.get("cursor") or 0)
            if next_cursor == cursor:
                raise ValueError("喜欢列表分页游标没有前进")
            cursor = next_cursor
            await asyncio.sleep(3)
        if not ids:
            raise ValueError("喜欢列表为空，尚不能验证真实视频采集")
        result = {"status": "entry_access_verified", "pages": pages, "unique_ids": len(set(ids)), "ordering_verified": False, "baseline_created": False}
        output = ROOT / "runtime" / "entry-probe.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({**result, "ids": ids}, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        await collector.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["login", "probe"])
    args = parser.parse_args()
    try:
        result = browser_collector.login_browser(timeout_seconds=600, source="like") if args.action == "login" else asyncio.run(probe())
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as error:
        print(json.dumps({"status": "entry_blocked", "message": safe_error_message(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
