"""Register only explicitly selected IDs from a completed targeted likes audit."""
import argparse
import asyncio
import json
from pathlib import Path
import library


def register(path, selected_ids, refresh_media=False):
    allowed = set(library.audit_ids(path))
    if not selected_ids or len(set(selected_ids)) != len(selected_ids) or not set(selected_ids).issubset(allowed):
        raise ValueError("ids_not_in_completed_audit")
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    items = {item['id']: item for item in report['items']}
    added, existing = [], []
    with library.process_lock(), library.database() as db:
        for video_id in selected_ids:
            if db.execute('select 1 from videos where id=?', (video_id,)).fetchone():
                existing.append(video_id)
                continue
            item = items[video_id]
            library.ingest(db, [{'aweme_id': video_id, 'description': item['title'], 'author': item['author']}], False)
            added.append(video_id)
    refreshed = []
    if refresh_media:
        with library.process_lock():
            refreshed = sorted(asyncio.run(library.refresh_media_metadata(selected_ids)))
    return {'status': 'registered', 'added': added, 'already_registered': existing, 'media_refreshed': refreshed,
            'audit_path': str(Path(path).resolve()), 'historical_list_changed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', required=True)
    parser.add_argument('--ids', nargs='+', required=True)
    parser.add_argument('--refresh-media', action='store_true', help='Refresh metadata only for these IDs using the existing bounded lookup')
    args = parser.parse_args()
    print(json.dumps(register(args.audit, args.ids, args.refresh_media), ensure_ascii=True))
