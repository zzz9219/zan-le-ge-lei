"""Minimal local adapter around douyin-favorites-to-knowledge 2.3.2."""
import argparse
import asyncio
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from entry_probe import ROOT, browser_collector
from douyin_favorites_knowledge.core_bridge import atomic_write_json, atomic_write_text
from douyin_favorites_knowledge.security import safe_error_message
from douyin_favorites_knowledge.workflow import normalize_item
from douyin_favorites_knowledge import local_whisper

RUNTIME = ROOT / "runtime"
DB = RUNTIME / "library.sqlite3"
TZ = timezone(timedelta(hours=8))
CATEGORIES = ["未分类"]


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def today():
    return now()[:10]


@contextmanager
def database():
    RUNTIME.mkdir(exist_ok=True)
    with closing(sqlite3.connect(DB, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        db.executescript("""
        create table if not exists settings (key text primary key, value text);
        create table if not exists videos (
          id text primary key, metadata text not null, discovered_at text not null,
          baseline integer not null, status text not null, error text default '',
          transcript text default '', clean text default '', summary text default '',
          points text default '[]', category text default '未分类', kind text default '待识别',
          duration real default 0, elapsed real default 0, transcript_hash text default '',
          check_note text default '', model text default '');
        create table if not exists attempts (
          id integer primary key, day text not null, video_id text not null,
          started_at text not null, status text not null);
        create table if not exists batches (
          id text primary key, day text not null, payload text not null,
          state text not null, tokens integer, token_kind text default 'unknown');
        create table if not exists backfill_items (
          video_id text primary key, rank integer not null, selected_at text not null);
        """)
        if "conclusion" not in {row[1] for row in db.execute("pragma table_info(videos)")}:
            db.execute("alter table videos add column conclusion text default ''")
        yield db


@contextmanager
def process_lock(name="pipeline.lock"):
    # OS byte lock is released even after termination; no stale PID lock.
    import msvcrt
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME / name).open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError("another_pipeline_is_running") from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def setting(db, key, value):
    db.execute("insert into settings values (?, ?) on conflict(key) do update set value=excluded.value", (key, value))


def category_names(db):
    row = db.execute("select value from settings where key='custom_categories'").fetchone()
    themes = db.execute("select value from settings where key='topic_categories'").fetchone()
    base = json.loads(themes[0]) if themes else CATEGORIES
    return list(dict.fromkeys(base + (json.loads(row[0]) if row else [])))


def ingest(db, items, baseline):
    added = 0
    for raw in items:
        item = browser_collector._source_item(raw, now(), "like")
        if item is None:
            raise ValueError("invalid_video_id")
        added += db.execute("insert or ignore into videos(id,metadata,discovered_at,baseline,status,duration) values(?,?,?,?,?,?)",
                            (item["aweme_id"], json.dumps(item, ensure_ascii=False), now(), int(baseline),
                             "baseline" if baseline else "queued", item.get("duration_seconds", 0))).rowcount
        # Refresh expiring media links without touching transcripts or first-discovery time.
        db.execute("update videos set metadata=? where id=?", (json.dumps(item, ensure_ascii=False), item["aweme_id"]))
    return added


async def scan(full=False):
    with database() as db:
        baseline = db.execute("select value from settings where key='baseline_complete'").fetchone() is None
        state = dict(db.execute("select key,value from settings").fetchall())
    progress = json.loads(state.get("scan_progress", "{}"))
    resume_cursor = int(state.get("next_cursor", "0")) if baseline and progress.get("baseline") else 0
    start_page = int(progress.get("page", 0)) + 1 if resume_cursor else 1
    full = full or baseline
    mode = "baseline" if baseline else "full" if full else "incremental"
    collector = browser_collector.BrowserCollector(source="like")
    await collector.open(headless=True)
    added, cursor, seen = 0, resume_cursor, set()
    scanned_ids, known_pages = set(), 0
    order_path = RUNTIME / "display-likes-order.json"
    previous = json.loads(order_path.read_text(encoding="utf-8")) if order_path.exists() else None
    boundary_ids = set(audit_ids(order_path)) if previous else set()
    if not baseline and not boundary_ids:
        with database() as db:
            boundary_ids = {row[0] for row in db.execute("select id from videos")}
    head, head_ids, boundary_found = [], set(), False
    try:
        await collector.navigate()
        if not await collector.authenticated():
            raise ValueError("login_required: 请运行 login.cmd")
        # User-authorized bounded scan; unverified ordering can leave deeper additions unseen.
        for page in range(start_page, 5001 if full else 101):
            response = await collector.fetch_page(cursor=cursor, count=100)
            if not response.get("ok"):
                raise ValueError("collection_stopped: login/captcha/rate-limit; status=" + str(response.get("status_code", response.get("http_status", "unknown"))))
            items = response.get("items")
            if not isinstance(items, list) or (not items and response.get("has_more")):
                raise ValueError("incomplete_pagination: no baseline enabled")
            if not baseline and not boundary_found:
                for raw in items:
                    video_id = str(raw["aweme_id"])
                    if video_id not in head_ids:
                        head.append({"id": video_id, "title": raw.get("description", ""), "author": raw.get("author", "")})
                        head_ids.add(video_id)
                    if video_id in boundary_ids:
                        boundary_found = True
                        break
            with database() as db:
                page_added = ingest(db, items, baseline)
                added += page_added
                known_pages = known_pages + 1 if len(scanned_ids) >= 1000 and page_added == 0 else 0
                scanned_ids.update(str(item["aweme_id"]) for item in items)
                setting(db, "scan_progress", json.dumps({"page": page, "baseline": baseline, "mode": mode, "ids_checked": len(scanned_ids), "time": now()}))
                setting(db, "next_cursor", str(response.get("cursor") or 0))
            stop_reason = "end_of_list" if not response.get("has_more") else ""
            if not full and not stop_reason:
                stop_reason = "known_pages" if known_pages >= 3 else "page_limit" if page == 100 else ""
            if stop_reason:
                result = {"status": "baseline_created" if baseline else "synced", "added": added, "pages": page,
                          "mode": mode, "ids_checked": len(scanned_ids), "stop_reason": stop_reason,
                          "full_scan_complete": stop_reason == "end_of_list"}
                with database() as db:
                    if baseline:
                        setting(db, "baseline_complete", now())
                    setting(db, "last_scan" if stop_reason == "end_of_list" else "last_incremental_scan", now())
                    setting(db, "scan_result", json.dumps(result))
                    setting(db, "collection_error", "")
                if not baseline:
                    if boundary_found or stop_reason == "end_of_list":
                        merged = head + [item for item in (previous or {}).get("items", []) if item["id"] not in head_ids]
                        if merged:
                            report = {"checked_at": now(), "range_complete": True, "target_found": True,
                                      "target_id": merged[-1]["id"], "items": merged, "source": "daily_bounded_scan"}
                            frozen = RUNTIME / "collection" / ("daily-latest-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + ".json")
                            atomic_write_json(frozen, report)
                            atomic_write_json(order_path, report)
                            result["priority_audit"] = str(frozen)
                    else:
                        with database() as db:
                            setting(db, "collection_error", "latest_range_unconfirmed")
                        result["latest_range_confirmed"] = False
                return result
            nxt = int(response.get("cursor") or 0)
            if nxt == cursor or nxt in seen:
                raise ValueError("pagination_cursor_repeated: baseline remains incomplete")
            seen.add(cursor)
            cursor = nxt
            await asyncio.sleep(3)
        raise ValueError("metadata_scan_limit_5000_pages: baseline remains incomplete")
    except Exception as error:
        with database() as db:
            setting(db, "collection_error", safe_error_message(error))
        raise
    finally:
        await collector.close()


def backfill_workflow(db):
    counts = dict(db.execute("select v.status,count(*) from videos v join backfill_items b on b.video_id=v.id group by v.status").fetchall())
    remaining = counts.get("queued", 0) + counts.get("transcribing", 0)
    return {"phase": "transcribing" if remaining else "organizing" if counts.get("ready", 0) else "complete",
            "remaining_transcriptions": remaining, "ready": counts.get("ready", 0), "counts": counts}


def latest_workflow(db):
    path = RUNTIME / "display-likes-order.json"
    ids = audit_ids(path) if path.exists() else []
    selected = json.dumps(ids)
    historical = {row[0] for row in db.execute("select video_id from backfill_items where video_id in (select value from json_each(?)) union select id from videos where baseline=1 and id in (select value from json_each(?))", (selected, selected))}
    ids = [video_id for video_id in ids if video_id not in historical]
    selected = json.dumps(ids)
    counts = dict(db.execute("select status,count(*) from videos where id in (select value from json_each(?)) group by status", (selected,)).fetchall())
    missing = len(ids) - sum(counts.values()) + counts.get("baseline", 0)
    pending = db.execute("select count(distinct b.id) from batches b,json_each(b.payload,'$.items') j where b.state='pending' and json_extract(j.value,'$.id') in (select value from json_each(?))", (selected,)).fetchone()[0]
    error = db.execute("select value from settings where key='collection_error'").fetchone()
    remaining = counts.get("queued", 0) + counts.get("transcribing", 0)
    phase = "collecting" if missing or (error and error[0]) else "transcribing" if remaining else "organizing" if counts.get("ready", 0) or pending else "complete"
    return {"phase": phase, "ids": ids, "missing": missing, "remaining_transcriptions": remaining,
            "ready": counts.get("ready", 0), "pending_batches": pending, "counts": counts}


def prioritized_selection(db, scope, video_ids):
    latest = latest_workflow(db)
    progress = {key: value for key, value in latest.items() if key != "ids"}
    if latest["phase"] != "complete":
        requested = video_ids
        if video_ids is not None:
            selected = json.dumps(video_ids)
            excluded = {row[0] for row in db.execute("select video_id from backfill_items where video_id in (select value from json_each(?)) union select id from videos where baseline=1 and id in (select value from json_each(?))", (selected, selected))}
            requested = [video_id for video_id in video_ids if video_id not in excluded]
        if scope == "backfill" or (requested is not None and not set(requested).issubset(latest["ids"])):
            return scope, video_ids, {"status": "latest_in_progress", **progress}
        if latest["phase"] == "collecting":
            return scope, video_ids, {"status": "waiting_for_collection", **progress}
        return "new", latest["ids"] if requested is None else requested, None
    older = db.execute("select count(*) from videos where baseline=0 and status in ('queued','transcribing','ready') and id not in (select video_id from backfill_items)").fetchone()[0]
    if scope == "backfill" and older:
        return scope, video_ids, {"status": "older_queue_in_progress", "remaining": older}
    return "new" if scope == "all" and older else scope, video_ids, None


def prioritize_audit(path):
    audit_ids(path)
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    with database() as db:
        previous = latest_workflow(db)
        terminal = {row[0] for row in db.execute("select id from videos where status in ('done','untranscribed','failed') and id in (select value from json_each(?))", (json.dumps(previous["ids"]),))}
        pending = {row[0] for row in db.execute("select json_extract(j.value,'$.id') from batches b,json_each(b.payload,'$.items') j where b.state='pending'")}
        unfinished = set(previous["ids"]) - terminal | (pending & set(previous["ids"]))
        if not unfinished.issubset({item["id"] for item in report["items"]}):
            raise ValueError("priority_audit_omits_unfinished_latest")
    atomic_write_json(RUNTIME / "display-likes-order.json", report)
    return {"status": "latest_priority_selected", "count": len(report["items"]), "audit_path": str(Path(path).resolve())}


def stats():
    with database() as db:
        counts = dict(db.execute("select status,count(*) from videos group by status").fetchall())
        settings = dict(db.execute("select key,value from settings").fetchall())
        duration, elapsed = db.execute("select coalesce(sum(duration),0),coalesce(sum(elapsed),0) from videos where baseline=0").fetchone()
        batches = db.execute("select count(*) from batches where state='done'").fetchone()[0]
        backfill_300 = dict(db.execute("select v.status,count(*) from videos v join backfill_items b on b.video_id=v.id where b.rank<=300 group by v.status").fetchall())
        backfill_after_300 = dict(db.execute("select v.status,count(*) from videos v join backfill_items b on b.video_id=v.id where b.rank>300 group by v.status").fetchall())
        workflow = backfill_workflow(db)
        latest = latest_workflow(db)
        backfill = workflow["counts"]
    return {"counts": counts, "settings": settings, "duration_seconds": duration, "asr_seconds": elapsed,
            "ai_batches": batches, "backfill_300": backfill_300, "backfill_after_300": backfill_after_300,
            "backfill": backfill, "backfill_count": sum(backfill.values()),
            "workflow": workflow,
            "latest_workflow": {key: value for key, value in latest.items() if key != "ids"},
            "token_usage": "unknown unless reported by the agent", "db": str(DB)}


async def select_backfill(limit=300, allow_short=False):
    """One explicitly authorized snapshot; incomplete fetches never enqueue history."""
    with database() as db:
        selected = db.execute("select count(*) from backfill_items").fetchone()[0]
    if selected:
        return {"status": "already_selected", "count": selected}
    if not 1 <= limit <= 500:
        raise ValueError("invalid_initial_sample_count")
    collector = browser_collector.BrowserCollector(source="like")
    await collector.open(headless=True)
    items, ids, cursors, cursor = [], set(), set(), 0
    ended = False
    try:
        await collector.navigate()
        if not await collector.authenticated():
            raise ValueError("login_required: 请运行 login.cmd")
        for _ in range(30):
            response = await collector.fetch_page(cursor=cursor, count=100)
            if not response.get("ok"):
                raise ValueError("backfill_collection_stopped: login/captcha/rate-limit")
            for raw in response.get("items", []):
                item = browser_collector._source_item(raw, now(), "like")
                if item is None:
                    raise ValueError("invalid_video_id")
                if item["aweme_id"] not in ids:
                    items.append(raw)
                    ids.add(item["aweme_id"])
                if len(items) == limit:
                    break
            ended = not response.get("has_more")
            if len(items) == limit or ended:
                break
            nxt = int(response.get("cursor") or 0)
            if nxt == cursor or nxt in cursors:
                raise ValueError("backfill_pagination_cursor_repeated")
            cursors.add(cursor)
            cursor = nxt
            await asyncio.sleep(3)
        if not items or (len(items) != limit and not (allow_short and ended)):
            raise ValueError("backfill_requires_" + str(limit) + "_unique_items: found=" + str(len(items)))
        selected_at = now()
        with database() as db:
            ingest(db, items, baseline=True)
            for rank, raw in enumerate(items, 1):
                video_id = browser_collector._source_item(raw, selected_at, "like")["aweme_id"]
                db.execute("insert into backfill_items values(?,?,?)", (video_id, rank, selected_at))
                db.execute("update videos set baseline=0,status='queued',error='' where id=? and baseline=1 and status='baseline'", (video_id,))
            setting(db, "backfill_selected_at", selected_at)
            setting(db, "backfill_scope", "用户授权：喜欢列表当前前" + str(len(items)) + "条；接口顺序不等于已验证点赞时间")
        return {"status": "selected", "count": len(items), **export_daily()}
    finally:
        await collector.close()


def export_backfill(db):
    rows = db.execute("select v.*,b.rank from backfill_items b join videos v on v.id=b.video_id order by b.rank").fetchall()
    if not rows:
        return
    for row in rows:
        if not (ROOT / "knowledge" / ("like-" + row["id"] + ".md")).exists():
            export_video(db, row["id"])
    groups = [
        (rows, "历史回填整理.md", "全部历史回填"),
        ([row for row in rows if row["rank"] <= 300], "首批历史整理.md", "原固定历史名单"),
        ([row for row in rows if row["rank"] > 300], "后续历史整理.md", "后续历史回填"),
    ]
    for selected, filename, label in groups:
        if not selected:
            continue
        lines = [f"# 抖音{label} · {len(selected)}条", "",
                 f"范围：固定名单第{selected[0]['rank']}–{selected[-1]['rank']}条；实际收录{len(selected)}条；真实点赞时间排序未验证。", "",
                 "知识类保留摘要和重点，娱乐类保留简短记录，失败和未转写内容仍保留来源。", ""]
        counts = {}
        for row in selected:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        lines += ["当前状态：" + "；".join(k + " " + str(v) for k,v in counts.items()), ""]
        for category in category_names(db):
            group = [row for row in selected if row["category"] == category]
            if not group:
                continue
            lines += ["## " + category, ""]
            for row in group:
                title = json.loads(row["metadata"])["title"].replace("\n", " ").replace("[", "（").replace("]", "）")
                lines += [f"### {row['rank']}. {title}", "", f"状态：{row['status']}；类型：{row['kind']}", "",
                          row["summary"] or row["error"] or "等待转写或整理。", "",
                          f"[完整笔记](like-{row['id']}.md) · [原视频](https://www.douyin.com/video/{row['id']})", ""]
        atomic_write_text(ROOT / "knowledge" / filename, "\n".join(lines))


def export_video(db, video_id):
    row = db.execute("select * from videos where id=? and baseline=0", (video_id,)).fetchone()
    if row is None:
        return
    raw = json.loads(row["metadata"])
    raw.update(transcript=row["transcript"], transcript_status=row["status"],
               transcript_source="local_whisper", observed_at=row["discovered_at"], tags=[row["category"], row["kind"]])
    # Keep raw ASR bytes in SQLite/files; mark uncertain characters only in the export view.
    uncertain = "\ufffd" in raw["transcript"]
    if uncertain:
        raw["transcript"] = raw["transcript"].replace("\ufffd", "[识别不清]")
    text = normalize_item(raw)["note"]
    report = ""
    if uncertain:
        report += "\n## 转写待核对\n\n原始转写含 Unicode 替换字符；导出以[识别不清]标记，原始正文与文件完整保留。\n"
    if row["summary"]:
        conclusion = row["conclusion"] or row["summary"].split("。")[0] + "。"
        report += "\n## 一句话结论（来自博主内容）\n\n" + conclusion + "\n"
        report += "\n## 摘要（来自博主内容）\n\n" + row["summary"] + "\n"
        points = json.loads(row["points"])
        if points:
            report += "\n## 核心重点\n\n" + "\n".join("- " + point for point in points) + "\n"
    if row["check_note"]:
        report += "\n## 待核对\n\n" + row["check_note"] + "\n"
    if row["clean"]:
        report += "\n## 清理稿\n\n" + row["clean"] + "\n"
    text = text.replace("\n## 原始材料", report + "\n## 原始材料", 1)
    if db.execute("select 1 from backfill_items where video_id=?", (video_id,)).fetchone():
        text += "\n## 归档来源\n\n用户授权的历史点赞回填；不是今日新增点赞。\n"
    atomic_write_text(ROOT / "knowledge" / ("like-" + video_id + ".md"), text)


def export_daily(day=None):
    day = day or today()
    with database() as db:
        rows = db.execute("select * from videos where baseline=0 and id not in (select video_id from backfill_items) and substr(discovered_at,1,10)=? order by discovered_at", (day,)).fetchall()
        lines = ["# 抖音归档日报 " + day, "", "记录首次发现日期，不代表真实点赞时间；历史回填在单篇笔记中标注。", ""]
        for row in rows:
            export_video(db, row["id"])
            meta = json.loads(row["metadata"])
            lines += ["## " + meta["title"], "", "状态：" + row["status"] + "；分类：" + row["category"],
                      "", row["summary"] or "尚未完成整理", "", "https://www.douyin.com/video/" + row["id"], ""]
        export_backfill(db)
    path = ROOT / "knowledge" / "日报" / (day + ".md")
    atomic_write_text(path, "\n".join(lines))
    return {"daily_report": str(path), "items": len(rows)}


def audit_ids(path):
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = [item["id"] for item in report.get("items", [])]
    if (report.get("range_complete") is not True or report.get("target_found") is not True
            or not ids or ids[-1] != report.get("target_id") or len(set(ids)) != len(ids)
            or any(not isinstance(i,str) or len(i)!=19 or not i.isascii() or not i.isdigit() for i in ids)):
        raise ValueError("invalid_or_incomplete_audit_selection")
    return ids


def transcribe_queue(model_name="small", limit=500, scope="all", video_ids=None):
    with process_lock("asr.lock"):
        # Owning the ASR lock proves no previous transcription attempt is live.
        with database() as db:
            db.execute("update attempts set status='interrupted' where status='running'")
        return _transcribe_queue(model_name,limit,scope,video_ids)


async def fetch_video_metadata(collector, video_id):
    """Read the video's own detail response through the existing logged-in browser."""
    from urllib.parse import parse_qs, urlparse
    from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
    try:
        async with collector._page.expect_response(
            lambda response: urlparse(response.url).hostname == "www.douyin.com"
            and urlparse(response.url).path == "/aweme/v1/web/aweme/detail/"
            and parse_qs(urlparse(response.url).query).get("aweme_id") == [video_id],
            timeout=25000,
        ) as info:
            try:
                await collector._page.goto("https://www.douyin.com/video/" + video_id, wait_until="domcontentloaded", timeout=25000)
            except PlaywrightTimeoutError:
                # The detail response may already exist even if the page keeps loading.
                pass
            except PlaywrightError as error:
                if "is interrupted by another navigation" not in str(error):
                    raise
        response = await info.value
    except PlaywrightTimeoutError as error:
        raise ValueError("media_metadata_lookup_timed_out") from error
    if response.status in (401, 403, 429):
        raise ValueError("collection_stopped: video_detail_http=" + str(response.status))
    if response.status != 200:
        raise ValueError("media_detail_http_" + str(response.status))
    payload = await response.json()
    if payload.get("status_code") != 0:
        raise ValueError("collection_stopped: video_detail_status=" + str(payload.get("status_code")))
    item = payload.get("aweme_detail") or {}
    if str(item.get("aweme_id")) != video_id:
        return None
    video = item.get("video") or {}
    return {"aweme_id": video_id, "description": item.get("desc") or "",
            "author": (item.get("author") or {}).get("nickname") or "",
            "play_url": next(iter((video.get("play_addr") or {}).get("url_list") or []), ""),
            "duration_seconds": float(video.get("duration") or 0) / 1000,
            "media_kind": "图文" if item.get("images") else "视频",
            "statistics": item.get("statistics") or {}, "create_time": item.get("create_time"),
            "statistics_observed_at": now()}


async def refresh_media_metadata(video_ids):
    """Refresh only selected existing IDs, regardless of their position in the likes list."""
    collector = browser_collector.BrowserCollector(source="like")
    await collector.open(headless=True)
    refreshed = set()
    try:
        if not await collector.authenticated():
            raise ValueError("login_required: 请运行 login.cmd")
        for video_id in dict.fromkeys(video_ids):
            try:
                item = await fetch_video_metadata(collector, video_id)
            except ValueError as error:
                if str(error) != "media_metadata_lookup_timed_out":
                    raise
                with database() as db:
                    db.execute("update videos set error=? where id=?", (str(error), video_id))
                continue
            if item is None or (not item.get("play_url") and item.get("media_kind") != "图文"):
                continue
            with database() as db:
                if not db.execute("select 1 from videos where id=?", (video_id,)).fetchone():
                    raise ValueError("media_refresh_requires_existing_video")
                ingest(db, [item], baseline=False)
            refreshed.add(video_id)
            await asyncio.sleep(1)
        return refreshed
    finally:
        await collector.close()


def _transcribe_queue(model_name="small", limit=500, scope="all", video_ids=None):
    requested_scope, requested_ids = scope, video_ids
    with database() as db:
        scope, video_ids, blocked = prioritized_selection(db, scope, video_ids)
        if blocked:
            return {**blocked, "completed": 0}
        used = db.execute("select count(*) from attempts where day=? and video_id not in (select video_id from backfill_items)", (today(),)).fetchone()[0]
        membership = {"all":"1=1","new":"id not in (select video_id from backfill_items)","backfill":"id in (select video_id from backfill_items)"}[scope]
        maximum = max(0,limit) if scope == "backfill" else max(0,min(limit,500-used))
        selection = "" if video_ids is None else " and " + ("v.id" if scope == "backfill" else "id") + " in (" + ",".join("?" for _ in video_ids) + ")"
        parameters = [] if video_ids is None else list(video_ids)
        if scope == "backfill":
            rows = db.execute("select v.* from videos v join backfill_items b on b.video_id=v.id where v.baseline=0 and v.status in ('queued','transcribing')" + selection + " order by b.rank limit ?", (*parameters,maximum)).fetchall()
        else:
            rows = db.execute("select * from videos where baseline=0 and status in ('queued','transcribing') and " + membership + selection + " order by discovered_at limit ?", (*parameters,maximum)).fetchall()
    # Avoid model startup entirely on quiet days.
    if rows:
        local_whisper._local_model(model_name)
    completed, stop_error = 0, ""
    for row in rows:
        video_id = row["id"]
        with database() as db:
            _, current_ids, blocked = prioritized_selection(db, requested_scope, requested_ids)
            if blocked or (current_ids is not None and video_id not in current_ids):
                return {**(blocked or {"status": "latest_in_progress"}), "completed": completed, **export_daily()}
        media = RUNTIME / "media" / (video_id + ".mp4")
        media.parent.mkdir(exist_ok=True)
        with database() as db:
            attempt = db.execute("insert into attempts(day,video_id,started_at,status) values(?,?,?,'running')", (today(), video_id, now())).lastrowid
            db.execute("update videos set status='transcribing',error='' where id=?", (video_id,))
        try:
            saved = RUNTIME / "transcripts" / (video_id + ".json")
            with database() as db:
                metadata = json.loads(db.execute("select metadata from videos where id=?", (video_id,)).fetchone()[0])
            if metadata.get("media_kind") == "图文":
                with database() as db:
                    db.execute("update videos set status='untranscribed',kind='图文',error='未转写：第一版不做 OCR' where id=?", (video_id,))
                    db.execute("update attempts set status='untranscribed' where id=?", (attempt,))
                    export_video(db,video_id)
                continue
            if not saved.exists() and not media.exists():
                urls = local_whisper._candidate_urls(metadata)
                staging = media.with_suffix(".part")
                error = local_whisper._download(urls[0], staging, local_whisper.MAX_MEDIA_BYTES) if urls else "metadata_unavailable"
                if error in ("http_403", "http_410", "metadata_unavailable"):
                    staging.unlink(missing_ok=True)
                    refreshed = asyncio.run(refresh_media_metadata([video_id]))
                    if video_id not in refreshed:
                        with database() as db:
                            reason = db.execute("select error from videos where id=?", (video_id,)).fetchone()[0]
                        raise ValueError(reason or "media_metadata_unavailable")
                    with database() as db:
                        metadata = json.loads(db.execute("select metadata from videos where id=?", (video_id,)).fetchone()[0])
                    if metadata.get("media_kind") == "图文":
                        with database() as db:
                            db.execute("update videos set status='untranscribed',kind='图文',error='未转写：第一版不做 OCR' where id=?", (video_id,))
                            db.execute("update attempts set status='untranscribed' where id=?", (attempt,))
                            export_video(db, video_id)
                        continue
                    urls = local_whisper._candidate_urls(metadata)
                    if not urls:
                        raise ValueError("media_metadata_unavailable")
                    error = local_whisper._download(urls[0], staging, local_whisper.MAX_MEDIA_BYTES)
                if error:
                    staging.unlink(missing_ok=True)
                    raise ValueError("media_download_" + error)
                staging.replace(media)
            try:
                result = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else local_whisper.transcribe_file(media, model_name)
            except Exception as error:
                raise ValueError("audio_transcription_failed: " + safe_error_message(error)) from error
            text = result["transcript"]
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            text_path = RUNTIME / "transcripts" / (video_id + ".txt")
            atomic_write_text(text_path, text)
            if hashlib.sha256(text_path.read_bytes()).hexdigest() != digest:
                raise ValueError("transcript_disk_verification_failed")
            atomic_write_json(saved, result)
            with database() as db:
                no_audio = result.get("transcript_status") == "no_audio"
                reason = "" if text else ("视频没有音轨" if no_audio else "音轨处理完成，但未识别出文字；尚不能判定没有口播")
                kind = "口播待核对" if text else ("无音轨" if no_audio else "未识别出文字")
                db.execute("update videos set transcript=?,transcript_hash=?,status=?,kind=?,error=?,duration=?,elapsed=?,model=? where id=?",
                           (text, digest, "ready" if text else "untranscribed", kind, reason, result["duration_seconds"], result["elapsed_seconds"], model_name, video_id))
                db.execute("update attempts set status=? where id=?", ("success" if text else "untranscribed", attempt))
                export_video(db, video_id)
            media.unlink(missing_ok=True)
            completed += 1
        except Exception as error:
            with database() as db:
                db.execute("update videos set status='failed',error=? where id=?", (safe_error_message(error), video_id))
                db.execute("update attempts set status='failed' where id=?", (attempt,))
            if str(error).startswith(("login_required", "collection_stopped")):
                stop_error = safe_error_message(error)
                with database() as db:
                    setting(db, "collection_error", stop_error)
                break
    return {"status": "stopped" if stop_error else "transcribed", "completed": completed,
            **({"error": stop_error} if stop_error else {}), **export_daily()}


def pending_batch(scope="all", video_ids=None):
    with database() as db:
        scope, video_ids, blocked = prioritized_selection(db, scope, video_ids)
        if blocked:
            return blocked
        membership = {"all": "1=1", "new": "id not in (select video_id from backfill_items)", "backfill": "id in (select video_id from backfill_items)"}[scope]
        previous = None
        for batch in db.execute("select payload from batches where state='pending' order by rowid"):
            ids = [item["id"] for item in json.loads(batch[0])["items"]]
            if video_ids is not None and not set(ids).issubset(video_ids):
                if set(ids).intersection(video_ids):
                    return {"status":"selection_conflicts_pending","batch_id":json.loads(batch[0])["batch_id"]}
                continue
            matched = db.execute("select count(*) from videos where " + membership + " and id in (" + ",".join("?" for _ in ids) + ")",ids).fetchone()[0]
            if matched == len(ids):
                previous = batch
                break
        if previous:
            payload = json.loads(previous[0])
            path = RUNTIME / "pending" / (payload["batch_id"] + ".json")
            atomic_write_json(path, payload)
            return {"batch_id": payload["batch_id"], "count": len(payload["items"]), "path": str(path)}
        workflow = backfill_workflow(db)
        if scope in ("backfill", "all") and workflow["phase"] == "transcribing":
            return {"status": "waiting_for_transcription", **workflow}
        selection = "" if video_ids is None else " and " + ("v.id" if scope == "backfill" else "id") + " in (" + ",".join("?" for _ in video_ids) + ")"
        parameters = [] if video_ids is None else list(video_ids)
        if video_ids is not None:
            remaining = db.execute("select count(*) from videos v where v.baseline=0 and v.status in ('queued','transcribing')" + selection,parameters).fetchone()[0]
            if remaining:
                return {"status":"waiting_for_transcription","remaining_transcriptions":remaining}
        elif scope == "new":
            remaining = db.execute("select count(*) from videos where baseline=0 and status in ('queued','transcribing') and " + membership).fetchone()[0]
            if remaining:
                return {"status": "waiting_for_transcription", "remaining_transcriptions": remaining}
        if scope == "all" and workflow["phase"] == "organizing":
            scope = "backfill"
            membership = "id in (select video_id from backfill_items)"
        used = db.execute("select count(*) from batches b,json_each(b.payload,'$.items') j where b.day=? and json_extract(j.value,'$.id') not in (select video_id from backfill_items)", (today(),)).fetchone()[0]
        if scope != "backfill" and used >= 500:
            return {"status": "daily_ai_limit", "items": []}
        if scope == "backfill":
            rows = db.execute("select v.* from videos v join backfill_items b on b.video_id=v.id where v.baseline=0 and v.status='ready' and v.id not in (select json_extract(j.value,'$.id') from batches b,json_each(b.payload,'$.items') j where b.state='pending')" + selection + " order by b.rank limit 5",parameters).fetchall()
        else:
            rows = db.execute("select * from videos where baseline=0 and status='ready' and " + membership + " and id not in (select json_extract(j.value,'$.id') from batches b,json_each(b.payload,'$.items') j where b.state='pending')" + selection + " order by discovered_at limit ?", (*parameters,min(5,500-used))).fetchall()
        items, size = [], 0
        for row in rows:
            text = row["transcript"]
            # Long records are explicit segments, never silently truncated.
            if len(text) > 12000:
                if items:
                    break
                segment_paths = []
                for index,pos in enumerate(range(0,len(text),10000)):
                    segment_path = RUNTIME / "pending" / "segments" / (row["id"] + "-" + str(index) + ".txt")
                    atomic_write_text(segment_path, text[pos:pos+10000])
                    segment_paths.append(str(segment_path))
                item = {"id": row["id"], "transcript_hash": row["transcript_hash"], "title": json.loads(row["metadata"])["title"],
                        "segment_paths": segment_paths, "characters":len(text)}
                items.append(item)
                break
            if size + len(text) > 12000:
                break
            items.append({"id": row["id"], "title": json.loads(row["metadata"])["title"], "transcript_hash": row["transcript_hash"], "transcript": text})
            size += len(text)
        if not items:
            return {"status": "no_pending", "items": []}
        batch_id = hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()[:24]
        payload = {"batch_id": batch_id, "items": items}
        db.execute("insert into batches(id,day,payload,state) values(?,?,?,'pending')", (batch_id,today(),json.dumps(payload,ensure_ascii=False)))
    path = RUNTIME / "pending" / (batch_id + ".json")
    atomic_write_json(path, payload)
    return {"batch_id": batch_id, "count": len(items), "path": str(path)}


def apply_analysis(path):
    result = json.loads(Path(path).read_text(encoding="utf-8"))
    with database() as db:
        batch = db.execute("select * from batches where id=?", (result["batch_id"],)).fetchone()
        if not batch:
            raise ValueError("unknown_batch")
        already_applied = batch["state"] == "done"
        expected = {item["id"]: item for item in json.loads(batch["payload"])["items"]}
        if not already_applied:
            if len(result["items"]) != len(expected) or {item["id"] for item in result["items"]} != set(expected):
                raise ValueError("batch_ids_mismatch")
            for item in result["items"]:
                row = db.execute("select * from videos where id=?", (item["id"],)).fetchone()
                if row["status"] != "ready" or row["transcript_hash"] != expected[item["id"]]["transcript_hash"]:
                    raise ValueError("stale_transcript")
                if "segment_paths" in expected[item["id"]]:
                    parts = item.get("clean_parts",[])
                    if len(parts) != len(expected[item["id"]]["segment_paths"]):
                        raise ValueError("long_clean_parts_incomplete")
                    contents = []
                    for part in parts:
                        part_path = Path(part).resolve()
                        if not part_path.is_relative_to((RUNTIME / "ai").resolve()):
                            raise ValueError("clean_part_outside_ai_workspace")
                        contents.append(part_path.read_text(encoding="utf-8"))
                    item["clean"] = "\n".join(contents)
                if item["category"] not in category_names(db) or not isinstance(item["points"], list) or not all(isinstance(p,str) for p in item["points"]):
                    raise ValueError("invalid_category_or_points")
                for key in ("clean", "summary", "kind", "check_note"):
                    if not isinstance(item[key],str):
                        raise ValueError("invalid_analysis_field:" + key)
                conclusion = item.get("conclusion", "")
                if not isinstance(conclusion, str):
                    raise ValueError("invalid_analysis_field:conclusion")
                db.execute("update videos set clean=?,summary=?,points=?,category=?,kind=?,check_note=?,conclusion=?,status='done' where id=?",
                           (item["clean"],item["summary"],json.dumps(item["points"],ensure_ascii=False),item["category"],item["kind"],item["check_note"],conclusion,item["id"]))
            tokens = result.get("tokens")
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise ValueError("invalid_tokens")
            token_kind = result.get("token_kind", "unknown")
            if token_kind not in ("actual", "estimate", "unknown") or (tokens is None and token_kind != "unknown"):
                raise ValueError("invalid_token_kind")
            db.execute("update batches set state='done',tokens=?,token_kind=? where id=?", (tokens,token_kind,result["batch_id"]))
    # Database is authoritative. Export may be rerun without redoing AI or ASR.
    with database() as db:
        report_days = set()
        for video_id in expected:
            export_video(db, video_id)
            report_days.add(db.execute("select substr(discovered_at,1,10) from videos where id=?", (video_id,)).fetchone()[0])
    for day in report_days:
        export_daily(day)
    return {"status": "already_applied" if already_applied else "analysis_applied", "count": len(expected), **export_daily()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["scan", "status", "transcribe", "daily", "pending", "apply", "retry", "export", "prepare-model", "backfill", "prioritize"])
    parser.add_argument("--model", choices=["small", "base"], default="small")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--file")
    parser.add_argument("--id")
    parser.add_argument("--scope",choices=["all","new","backfill"],default="all")
    parser.add_argument("--full", action="store_true", help="Explicit full metadata reconciliation; daily defaults to bounded scan")
    parser.add_argument("--ids-file", help="Complete targeted audit JSON; prioritize it or restrict transcribe/pending --scope new to its IDs")
    args = parser.parse_args()
    try:
        if args.ids_file and args.action != "prioritize" and (args.action not in ("transcribe","pending") or args.scope != "new"):
            raise ValueError("ids_file_requires_transcribe_or_pending_scope_new")
        if args.action == "prioritize" and not args.ids_file:
            raise ValueError("prioritize_requires_ids_file")
        selected_ids = audit_ids(args.ids_file) if args.ids_file else None
        if args.action in ("scan", "transcribe", "daily", "pending", "apply", "retry", "export", "prepare-model", "backfill", "prioritize"):
            with process_lock("ai.lock" if args.action in ("pending", "apply", "prioritize") else "pipeline.lock"):
                if args.action == "backfill":
                    result = asyncio.run(select_backfill(args.count, allow_short=True))
                elif args.action == "prioritize":
                    result = prioritize_audit(args.ids_file)
                elif args.action in ("scan", "daily"):
                    result = asyncio.run(scan(full=args.full))
                    if args.action == "daily" and result["status"] != "baseline_created":
                        with database() as db:
                            latest_ids = latest_workflow(db)["ids"]
                        result = {**result, **transcribe_queue(args.model, args.limit, "new", latest_ids)}
                elif args.action == "transcribe":
                    result = transcribe_queue(args.model, args.limit, args.scope, selected_ids)
                elif args.action == "pending":
                    result = pending_batch(args.scope, selected_ids)
                elif args.action == "apply":
                    result = apply_analysis(args.file)
                elif args.action == "retry":
                    with database() as db:
                        changed = db.execute("update videos set status='queued',error='' where id=? and baseline=0 and status='failed'", (args.id,)).rowcount
                    result = {"requeued": changed}
                elif args.action == "export":
                    result = export_daily()
                else:
                    from huggingface_hub import snapshot_download
                    snapshot_download("Systran/faster-whisper-" + args.model,
                                      allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*"])
                    result = {"model": args.model, "ready": True}
        else:
            result = stats()
        print(json.dumps(result, ensure_ascii=True))
        return 1 if result.get("status") == "stopped" else 0
    except Exception as error:
        print(json.dumps({"status": "stopped", "error": safe_error_message(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
