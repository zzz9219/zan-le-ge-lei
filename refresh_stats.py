"""Update existing library metrics only; no registration, media download, ASR or AI."""
import asyncio
import json
import enrichment
from library import process_lock, RUNTIME, now, atomic_write_json


if __name__ == '__main__':
    with process_lock(), process_lock('asr.lock'):
        result = asyncio.run(enrichment.refresh_metrics())
        result['finished_at'] = now()
        path = RUNTIME / 'metrics-refresh.json'
        atomic_write_json(path, result)
    print(json.dumps({**result, 'path': str(path)}, ensure_ascii=True))
