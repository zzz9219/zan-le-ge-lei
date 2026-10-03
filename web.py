"""Loopback-only searchable library; stdlib server, no frontend framework."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from datetime import date
import enrichment

from library import ROOT, database, stats, export_video, category_names, setting


VIEWS = {
    "all": "1=1", "done": "status='done'", "ready": "status='ready'",
    "processing": "status in ('queued','transcribing')",
    "attention": "status in ('failed','untranscribed')",
    "readable": "transcript<>''",
}


def recent_order():
    path = ROOT / "runtime" / "display-likes-order.json"
    if not path.exists():
        return "[]"
    report = json.loads(path.read_text(encoding="utf-8"))
    if not report.get("range_complete") or not report.get("target_found"):
        return "[]"
    return json.dumps([{"id": item["id"]} for item in report["items"]])


VIDEO_JOIN = " from videos v left join backfill_items b on b.video_id=v.id left join current_likes r on r.id=v.id and b.video_id is null"
VIDEO_FIELDS = "with current_likes as materialized (select json_extract(value,'$.id') as id,key from json_each(?)) select v.*,b.rank as source_rank,b.video_id is not null as historical,r.key+1 as current_like_rank"


def present(row, detail=False):
    item = dict(row)
    meta = json.loads(item.pop("metadata"))
    item["title"], item["author"] = meta["title"], meta["author"]
    item["points"] = json.loads(item["points"])
    item["url"] = "https://www.douyin.com/video/" + item["id"]
    item["conclusion"] = item["conclusion"] or (item["summary"].split("。")[0] + "。" if item["summary"] else "")
    item["has_transcript"] = bool(item["transcript"])
    item["historical"] = bool(row["historical"])
    item["metrics"] = enrichment.metrics(meta)
    if not detail:
        item["preview"] = item["transcript"][:130] if item["status"] == "ready" else ""
        item.pop("transcript")
        item.pop("clean")
        item.pop("transcript_hash")
    return item


class Handler(BaseHTTPRequestHandler):
    def respond(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write((ROOT / "index.html").read_bytes())
            return
        if url.path == "/api/status":
            with database() as db:
                categories = category_names(db)
            self.respond({**stats(), "categories": categories, "enrichment": dict(enrichment.job)})
            return
        if url.path in ("/api/comments", "/api/comments/export"):
            query = parse_qs(url.query)
            video_id = query.get("id", [""])[0]
            try:
                if not video_id.isascii() or not video_id.isdigit() or len(video_id) != 19:
                    raise ValueError("invalid_video_id")
                if url.path == "/api/comments/export":
                    data = enrichment.comment_export(video_id).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="douyin-comments-' + video_id + '.md"')
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.respond(enrichment.comment_rows(video_id, max(0, int(query.get("offset", ["0"])[0]))))
            except ValueError as error:
                self.respond({"error": str(error)}, 400)
            return
        if url.path in ("/api/video", "/api/note"):
            video_id = parse_qs(url.query).get("id", [""])[0]
            with database() as db:
                row = db.execute(VIDEO_FIELDS + VIDEO_JOIN + " where v.id=? and v.baseline=0", (recent_order(), video_id)).fetchone()
                if row is None:
                    self.respond({"error":"not_found"},404)
                    return
                if url.path == "/api/video":
                    self.respond(present(row, detail=True))
                    return
                export_video(db, video_id)
            data = (ROOT / "knowledge" / ("like-" + video_id + ".md")).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="like-' + video_id + '.md"')
            self.end_headers()
            self.wfile.write(data)
            return
        if url.path != "/api/videos":
            self.respond({"error": "not_found"},404)
            return
        query = parse_qs(url.query)
        search = query.get("q", [""])[0]
        category = query.get("category", [""])[0]
        view = query.get("view", ["all"])[0]
        sort = query.get("sort", ["source"])[0]
        source_order = "r.key is null,r.key,b.rank is null,b.rank,v.discovered_at desc,v.id"
        order = source_order if sort == "source" else "case v.status when 'done' then 0 when 'ready' then 1 when 'transcribing' then 2 when 'queued' then 3 else 4 end," + source_order
        where, params = ["baseline=0", VIEWS.get(view, VIEWS["all"])], []
        if search:
            where.append("(metadata like ? or transcript like ? or clean like ? or summary like ?)")
            params += ["%"+search+"%"]*4
        if category:
            where.append("category=?")
            params.append(category)
        try:
            for key in ("digg_count", "comment_count", "collect_count", "share_count"):
                value = query.get("min_" + key, [""])[0]
                if value:
                    minimum = int(value)
                    if minimum < 0:
                        raise ValueError("invalid_metric_minimum")
                    where.append("json_type(metadata,'$.statistics." + key + "')='integer' and json_extract(metadata,'$.statistics." + key + "')>=?")
                    params.append(minimum)
            for key, operator in (("published_from", ">="), ("published_to", "<=")):
                value = query.get(key, [""])[0]
                if value:
                    date.fromisoformat(value)
                    where.append("date(json_extract(metadata,'$.create_time'),'unixepoch','+8 hours')" + operator + "?")
                    params.append(value)
            offset = max(0,int(query.get("offset", ["0"])[0]))
        except ValueError:
            self.respond({"error":"invalid_filter"},400)
            return
        with database() as db:
            count = db.execute("select count(*) from videos where " + " and ".join(where),params).fetchone()[0]
            rows = db.execute(VIDEO_FIELDS + VIDEO_JOIN + " where " + " and ".join(where) + " order by " + order + " limit 50 offset ?",[recent_order(),*params,offset]).fetchall()
        items = [present(row) for row in rows]
        self.respond({"items":items,"total":count,"offset":offset})

    def do_POST(self):
        # Same-origin writes: prevent another website modifying the localhost library.
        origin = self.headers.get("Origin")
        if origin != "http://127.0.0.1:19423":
            self.respond({"error":"invalid_origin"},403)
            return
        if self.path not in ("/api/category", "/api/categories", "/api/enrich"):
            self.respond({"error":"not_found"},404)
            return
        try:
            length = int(self.headers.get("Content-Length","0"))
            if not 0 < length <= 2048:
                raise ValueError("invalid_length")
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError("invalid_body")
            if self.path == "/api/categories":
                name = value.get("name")
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 24 or any(ord(c) < 32 for c in name):
                    raise ValueError("invalid_category_name")
                name = name.strip()
                with database() as db:
                    categories = category_names(db)
                    if name not in categories:
                        setting(db, "custom_categories", json.dumps([name] + json.loads((db.execute("select value from settings where key='custom_categories'").fetchone() or ["[]"])[0]), ensure_ascii=False))
                    self.respond({"categories": category_names(db)})
                return
            video_id = value.get("id", "")
            if not isinstance(video_id, str) or (video_id and (len(video_id) != 19 or not video_id.isascii() or not video_id.isdigit())):
                raise ValueError("invalid_video_id")
            if self.path == "/api/category" and not video_id:
                raise ValueError("invalid_video_id")
            if self.path == "/api/enrich":
                if value.get("action") not in ("metrics", "comments", "connect-comments"):
                    raise ValueError("invalid_action")
                video_id = value.get("id", "")
                if value["action"] == "comments":
                    with database() as db:
                        if not db.execute("select 1 from videos where id=? and baseline=0", (video_id,)).fetchone():
                            raise ValueError("video_not_found")
                self.respond(enrichment.start_job(value["action"], video_id, value.get("resume") is True), 202)
                return
            with database() as db:
                if value["category"] not in category_names(db):
                    raise ValueError("invalid_category")
                changed = db.execute("update videos set category=? where id=? and baseline=0",(value["category"],value["id"])).rowcount
                export_video(db,value["id"])
            self.respond({"updated":changed})
        except (ValueError,KeyError) as error:
            self.respond({"error":str(error)},400)

    def log_message(self, *_):
        pass


def main():
    with database() as db:
        db.execute("create index if not exists videos_visible_status on videos(baseline,status)")
    print("http://127.0.0.1:19423",flush=True)
    ThreadingHTTPServer(("127.0.0.1",19423), Handler).serve_forever()


if __name__ == "__main__":
    main()
