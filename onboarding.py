"""Generate user-specific topics from a frozen sample, never a preset taxonomy."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from library import RUNTIME, database, setting, category_names, process_lock, select_backfill, now
from douyin_favorites_knowledge.core_bridge import atomic_write_json


def sample():
    with database() as db:
        rows = db.execute("select v.*,b.rank from videos v join backfill_items b on b.video_id=v.id order by b.rank").fetchall()
    return rows, hashlib.sha256(json.dumps([row["id"] for row in rows]).encode()).hexdigest()


def context():
    rows, digest = sample()
    if not rows:
        return {"status": "sample_required"}
    remaining = sum(row["status"] in ("queued", "transcribing") for row in rows)
    if remaining:
        return {"status": "waiting_for_transcription", "remaining": remaining}
    # Explicit excerpts infer themes only; full summaries use the original
    # pending workflow, which never truncates a long transcript.
    evidence = []
    for row in rows:
        meta = json.loads(row["metadata"])
        body = row["transcript"] or meta.get("description", "")
        evidence.append({"id": row["id"], "title": meta["title"][:300], "status": row["status"],
                         "excerpt": body[:600], "excerpt_characters": min(len(body), 600),
                         "original_characters": len(body), "excerpt_only": len(body) > 600,
                         "evidence_source": "local_transcript" if row["transcript"] else "video_description"})
    paths, group, size = [], [], 0
    def write_group():
        path = RUNTIME / "onboarding" / ("topic-evidence-" + str(len(paths)) + ".json")
        atomic_write_json(path, {"sample_hash": digest, "purpose": "theme_discovery_only", "items": list(group)})
        paths.append(str(path))
    for item in evidence:
        length = len(json.dumps(item, ensure_ascii=False))
        if group and size + length > 12000:
            write_group()
            group, size = [], 0
        group.append(item)
        size += length
    if group:
        write_group()
    return {"status": "topic_evidence_ready", "count": len(rows), "sample_hash": digest,
            "paths": paths, "output": str(RUNTIME / "onboarding" / "themes.json")}


def apply_themes(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    rows, digest = sample()
    if not rows or value.get("sample_hash") != digest:
        raise ValueError("topic_sample_changed")
    if any(row["status"] in ("queued", "transcribing") for row in rows):
        raise ValueError("topic_sample_waiting_for_transcription")
    reviewed = value.get("reviewed_ids")
    if not isinstance(reviewed, list) or any(not isinstance(item, str) for item in reviewed) or len(reviewed) != len(rows) or set(reviewed) != {row["id"] for row in rows}:
        raise ValueError("topic_sample_not_fully_reviewed")
    names = value.get("categories")
    if not isinstance(names, list) or not 1 <= len(names) <= 12 or any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 24 or any(ord(c) < 32 for c in name) for name in names):
        raise ValueError("invalid_topics")
    names = [name.strip() for name in names]
    if len(set(names)) != len(names) or "未分类" in names:
        raise ValueError("duplicate_or_reserved_topic")
    with database() as db:
        previous = db.execute("select value from settings where key='topic_categories'").fetchone()
        if previous and json.loads(previous[0]) != ["未分类"] + names:
            raise ValueError("topics_already_initialized_use_manual_categories")
        setting(db, "topic_categories", json.dumps(["未分类"] + names, ensure_ascii=False))
        setting(db, "topics_initialized_at", now())
        categories = category_names(db)
    return {"status": "personal_topics_saved", "sample_count": len(rows), "categories": categories}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["collect", "context", "apply-themes", "status"])
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--file")
    args = p.parse_args()
    try:
        if args.action == "collect":
            with process_lock():
                result = asyncio.run(select_backfill(args.count, allow_short=True))
        elif args.action == "context":
            result = context()
        elif args.action == "apply-themes":
            with process_lock("ai.lock"):
                result = apply_themes(args.file)
        else:
            with database() as db:
                result = {"categories": category_names(db), "topics_ready": bool(db.execute("select value from settings where key='topics_initialized_at'").fetchone())}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception as error:
        from douyin_favorites_knowledge.security import safe_error_message
        print(json.dumps({"status": "stopped", "error": safe_error_message(error)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
