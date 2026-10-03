"""Optional metadata/comment jobs; never change transcription queues."""
import asyncio
from contextlib import closing, ExitStack
from datetime import datetime
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from urllib.request import urlopen

from library import ROOT, TZ, database, process_lock, now, browser_collector

COMMENTS = ROOT / "comments"
job = {"status": "idle"}
job_guard = threading.Lock()
comment_browser_id = ""


def metrics(meta):
    values = meta.get("statistics") or {}
    counts = {}
    for key in ("digg_count", "comment_count", "collect_count", "share_count"):
        value = values.get(key)
        counts[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    try:
        published = datetime.fromtimestamp(float(meta["create_time"]), TZ).date().isoformat() if meta.get("create_time") else None
    except (TypeError, ValueError, OverflowError, OSError):
        published = None
    return {**counts, "published_date": published,
            "observed_at": meta.get("statistics_observed_at"),
            "deep_dive_recommended": counts["collect_count"] is not None and counts["collect_count"] > 100}


async def refresh_metrics():
    with database() as db:
        wanted = {row[0] for row in db.execute("select id from videos where baseline=0")}
    collector = browser_collector.BrowserCollector(source="like")
    await collector.open(headless=True)
    seen, cursor, updated = set(), 0, 0
    try:
        await collector.navigate()
        if not await collector.authenticated():
            raise ValueError("login_required")
        for page in range(100):
            response = await collector.fetch_page(cursor=cursor, count=100)
            if not response.get("ok"):
                raise ValueError("metadata_request_failed: " + str(response.get("error") or response.get("status_code")))
            with database() as db:
                for raw in response.get("items", []):
                    video_id = str(raw.get("aweme_id", ""))
                    if video_id not in wanted or video_id in seen:
                        continue
                    seen.add(video_id)
                    row = db.execute("select metadata from videos where id=?", (video_id,)).fetchone()
                    meta = json.loads(row[0])
                    for key in ("statistics", "create_time", "statistics_observed_at"):
                        if key in raw:
                            meta[key] = raw[key]
                    db.execute("update videos set metadata=? where id=?", (json.dumps(meta, ensure_ascii=False), video_id))
                    updated += 1
            job.update(updated=updated, pages=page + 1, remaining=len(wanted-seen))
            if seen == wanted or not response.get("has_more"):
                return {"updated": updated, "remaining": len(wanted-seen), "stop_reason": "all_found" if seen == wanted else "end_of_list"}
            next_cursor = int(response.get("cursor") or 0)
            if next_cursor == cursor:
                raise ValueError("metadata_cursor_stalled")
            cursor = next_cursor
            await asyncio.sleep(3)
        return {"updated": updated, "remaining": len(wanted-seen), "stop_reason": "page_limit"}
    finally:
        await collector.close()


def comment_rows(video_id, offset=0):
    path = COMMENTS / "data" / "douyin_comments.sqlite"
    if not path.exists():
        return {"items": [], "total": 0, "collection": None, "limit": 10, "offset": offset, "high_like_threshold": 100}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        video = db.execute("select top_comments,last_status,last_collected_at,last_error from videos where aweme_id=?", (video_id,)).fetchone()
        total = db.execute("select count(*) from comments where aweme_id=? and level=1", (video_id,)).fetchone()[0]
        threshold = int(db.execute("select value from settings where key='high_like_threshold'").fetchone()[0])
        rows = db.execute("select comment_id,text,nickname,likes,note,tags from comments where aweme_id=? and level=1 order by case when likes<0 then 1 else 0 end,likes desc,comment_id limit 10 offset ?", (video_id, offset)).fetchall()
    return {"items": [{**dict(row), "high_like": row["likes"] >= threshold} for row in rows], "total": total, "collection": dict(video) if video else None, "limit": 10, "offset": offset, "high_like_threshold": threshold}


def comment_export(video_id):
    node = shutil.which("node")
    if not node:
        raise ValueError("node_unavailable")
    script = "const c=require('./scripts/comment_library');process.stdout.write(c.exportCommentsMarkdown({awemeId:process.argv[1],highLikeThreshold:c.getSettings().high_like_threshold}).markdown);"
    try:
        result = subprocess.run([node, "-e", script, video_id], cwd=COMMENTS, capture_output=True, encoding="utf-8", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise ValueError("comment_export_failed") from error
    if result.returncode:
        raise ValueError("comment_export_failed")
    return result.stdout


def ensure_comment_bridge():
    def status():
        with urlopen("http://127.0.0.1:19422/api/status", timeout=2) as response:
            return json.load(response)
    try:
        return status()
    except OSError:
        node = shutil.which("node")
        if not node:
            raise ValueError("node_unavailable")
        log_path = ROOT / "runtime" / "comment-bridge.log"
        with log_path.open("ab") as log:
            subprocess.Popen([node, str(COMMENTS / "douyin-upstream" / "server.js")], cwd=COMMENTS / "douyin-upstream", stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(20):
            try:
                return status()
            except OSError:
                time.sleep(.2)
        raise ValueError("comment_bridge_unavailable")


def comment_connection(video_id=""):
    global comment_browser_id
    ensure_comment_bridge()
    node = shutil.which("node")
    if not node:
        raise ValueError("node_unavailable")
    script = """
const fs=require('node:fs');const c=require('./scripts/collect_comments');
const b=new c.BridgeClient(JSON.parse(fs.readFileSync('./douyin-upstream/config.json','utf8')).bridge,35000);
(async()=>{
 const wanted=process.argv[1],previous=process.argv[2],deadline=Date.now()+20000;
 while(true){
  const status=await b.status();
  const available=status.connections?.['douyin.com']||[];
  const verified=available.filter(x=>x.id===previous);
  const connections=c.bridgeConnectionIndex({connections:{'douyin.com':verified}})>=0?verified:available.filter(x=>!wanted||c.extractAwemeId(x.url)===wanted);
  const index=c.bridgeConnectionIndex({connections:{'douyin.com':connections}});
  if(index>=0){b.connectionId=connections[index].id;break;}
  if(Date.now()>=deadline){process.exitCode=1;return;}
  await new Promise(resolve=>setTimeout(resolve,1000));
 }
 if(b.connectionId!==previous){
  const value=await b.call("({ready:typeof window.__bridge?.getComments==='function'})");
  if(!value?.ready){process.exitCode=2;return;}
 }
 process.stdout.write(JSON.stringify({connection_id:b.connectionId}));
})().catch(()=>{process.exitCode=2;});
"""
    try:
        check = subprocess.run([node, "-e", script, video_id, comment_browser_id], cwd=COMMENTS, capture_output=True, encoding="utf-8", timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        comment_browser_id = ""
        raise ValueError("comment_browser_unresponsive") from error
    if check.returncode == 2:
        comment_browser_id = ""
        raise ValueError("comment_browser_unresponsive")
    if check.returncode == 0:
        comment_browser_id = json.loads(check.stdout)["connection_id"]
        return comment_browser_id
    comment_browser_id = ""
    return None


def update_comment_progress(folder, video_id):
    try:
        checkpoint = json.loads((folder / "checkpoint.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if checkpoint.get("aweme_id") == video_id:
        job.update(top_comments=checkpoint.get("top_comments", 0), top_pages=checkpoint.get("top_pages", 0), last_progress_at=checkpoint.get("updated_at"))


def collect_comments(video_id, resume=False):
    # Reuse the collector's live-browser check; never auto-retry a partial run.
    job["stage"] = "connecting_browser"
    connection_id = comment_connection(video_id)
    if not connection_id:
        raise ValueError("comment_browser_not_connected")
    node = shutil.which("node")
    args = [node, str(COMMENTS / "scripts" / "collect_comments.js"), "https://www.douyin.com/video/" + video_id, "--connection-id", connection_id, "--timeout-ms", "90000", "--max-comments", "1000", "--page-size", "50"]
    folder = COMMENTS / "outputs" / (video_id + "-" + datetime.now(TZ).strftime("%Y%m%dT%H%M%S%f"))
    if resume:
        path = COMMENTS / "data" / "douyin_comments.sqlite"
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("select last_output_dir,last_status from videos where aweme_id=?", (video_id,)).fetchone()
        if not row or row[1] != "partial":
            raise ValueError("no_partial_collection")
        folder = (COMMENTS / row[0]).resolve()
        if not folder.is_relative_to((COMMENTS / "outputs").resolve()) or not (folder / "checkpoint.json").is_file():
            raise ValueError("resume_checkpoint_unavailable")
        args.extend(["--resume-dir", str(folder)])
    else:
        args.extend(["--output-dir", str(folder)])
    job["stage"] = "collecting_comments"
    job["output_dir"] = str(folder)
    result = subprocess.Popen(args, cwd=COMMENTS, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
    while True:
        try:
            stdout, stderr = result.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            update_comment_progress(folder, video_id)
    update_comment_progress(folder, video_id)
    lines = stdout.strip().splitlines()
    summary = json.loads(lines[-1]) if lines else {}
    if result.returncode and not summary:
        raise ValueError(stderr.strip()[-600:] or "comment_collection_failed")
    return summary


def start_job(action, video_id="", resume=False):
    with job_guard:
        if job["status"] in ("queued", "running"):
            raise ValueError("enrichment_already_running")
        job.clear()
        job.update(status="queued", action=action, video_id=video_id, started_at=now())
        if action == "comments":
            job["comment_limit"] = 1000
        def run():
            deadline = time.monotonic() + 1800
            while True:
                try:
                    with ExitStack() as locks:
                        if action == "metrics":
                            locks.enter_context(process_lock())
                            locks.enter_context(process_lock("asr.lock"))
                        else:
                            locks.enter_context(process_lock("comments.lock"))
                        job["status"] = "running"
                        job.pop("waiting_for", None)
                        if action == "connect-comments":
                            job["stage"] = "connecting_browser"
                            connection_id = comment_connection(video_id)
                            result = {"connected": bool(connection_id), "connection_id": connection_id}
                        else:
                            result = asyncio.run(refresh_metrics()) if action == "metrics" else collect_comments(video_id, resume)
                        job.update(status="partial" if result.get("status") == "partial" else "completed", result=result)
                    break
                except Exception as error:
                    if action == "comments" and str(error) == "another_pipeline_is_running" and time.monotonic() < deadline:
                        job.update(status="queued", waiting_for="other_comment_task")
                        time.sleep(2)
                        continue
                    job.update(status="failed", error=str(error))
                    break
            job["finished_at"] = now()
        threading.Thread(target=run, daemon=True).start()
        return dict(job)
