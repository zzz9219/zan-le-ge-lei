"""Read-only audit: compare the page's likes pagination with the fixed snapshot."""
import argparse
import asyncio
from collections import Counter
import json
from urllib.parse import parse_qs, urlparse
from library import browser_collector, database, process_lock, now, RUNTIME
from douyin_favorites_knowledge.core_bridge import atomic_write_json


async def audit(until_id=None):
    collector = browser_collector.BrowserCollector(source="like")
    await collector.open(headless=True)
    requests = []
    def observe(request):
        parsed = urlparse(request.url)
        if parsed.path == "/aweme/v1/web/aweme/favorite/":
            q = parse_qs(parsed.query)
            requests.append({"cursor":q.get("max_cursor"),"count":q.get("count"),
                             "keys":sorted(q),"order":{k:v for k,v in q.items() if "order" in k}})
    collector._page.on("request", observe)
    try:
        await collector.navigate()
        if not await collector.authenticated():
            raise ValueError("login_required")
        first = await collector.fetch_page(cursor=0,count=20)
        if not first.get("ok"):
            raise ValueError("native_first_page_failed")
        direct = await collector.fetch_page(cursor=0,count=100) if until_id is None else first
        with database() as db:
            selected = [r[0] for r in db.execute("select video_id from backfill_items order by rank")]
        pages, items, seen, cursors = [], [], set(), set()
        response, cursor, target_found = first, 0, False
        for page in range(1,101 if until_id else 31):
            if not response.get("ok"):
                raise ValueError("audit_collection_stopped")
            rows = response.get("items")
            if not isinstance(rows,list) or (not rows and response.get("has_more")):
                raise ValueError("audit_incomplete_pagination")
            page_ids = []
            for raw in rows:
                video_id = str(raw["aweme_id"])
                page_ids.append(video_id)
                if video_id not in seen and (until_id or len(items)<350):
                    items.append({"id":video_id,"title":raw.get("description", ""),"author":raw.get("author", ""),"rank":len(items)+1})
                    seen.add(video_id)
                if until_id and video_id==until_id:
                    target_found=True
                    break
            pages.append({"page":page,"request_cursor":cursor,"next_cursor":response.get("cursor"),"ids":page_ids})
            if target_found or (until_id is None and len(items)==350) or not response.get("has_more"):
                break
            nxt = int(response.get("cursor") or 0)
            if nxt==cursor or nxt in cursors:
                raise ValueError("audit_cursor_repeated")
            cursors.add(cursor)
            cursor=nxt
            await asyncio.sleep(3)
            response=await collector.fetch_page(cursor=cursor,count=20)
        ids=[i["id"] for i in items]
        with database() as db:
            local={r[0]:(r[1],r[2]) for r in db.execute("select id,baseline,status from videos")}
        if until_id:
            for item in items:
                state=local.get(item["id"])
                item.update({"baseline":state[0] if state else None,"local_status":state[1] if state else "not_registered","in_original_300":item["id"] in selected})
        report={"checked_at":now(),"source_page":browser_collector.LIKES_PAGE_URL,
                "unique_items":len(ids),"same_initial_order":selected[:len(first['items'])]==[i['aweme_id'] for i in first['items']],
                "first_native_ids":[i['aweme_id'] for i in first['items']],
                "first_direct_ids":[i['aweme_id'] for i in direct.get('items',[])],
                "snapshot_overlap":len(set(ids)&set(selected)),
                "selected_missing_now":[i for i in selected if i not in ids],
                "selected_order_preserved":[i for i in ids if i in selected]==selected,
                "missing_from_snapshot":[i for i in ids if i not in selected],
                "missing_from_database":[i for i in ids if i not in local],
                "items":items,"pages":pages,"requests":requests}
        if until_id:
            report.pop("selected_missing_now")
            report.pop("selected_order_preserved")
            report.update({"target_id":until_id,"target_found":target_found,"range_complete":target_found,
                           "stop_reason":"target_found" if target_found else "end_of_list" if not response.get("has_more") else "page_limit",
                           "status_counts":dict(Counter(item["local_status"] for item in items)),"database_writes":0})
        atomic_write_json(RUNTIME/'likes-audit.json',report)
        result={k:report[k] for k in ("unique_items","same_initial_order","snapshot_overlap","missing_from_snapshot","missing_from_database")}
        if until_id:
            result={k:report[k] for k in ("unique_items","target_id","target_found","range_complete","stop_reason","status_counts","database_writes")}
            result["path"]=str(RUNTIME/'likes-audit.json')
        return result
    finally:
        await collector.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--until-id')
    args=parser.parse_args()
    if args.until_id and (not args.until_id.isdigit() or len(args.until_id)!=19):
        parser.error('--until-id must be a 19-digit video ID')
    with process_lock():
        print(json.dumps(asyncio.run(audit(args.until_id)),ensure_ascii=True),flush=True)
