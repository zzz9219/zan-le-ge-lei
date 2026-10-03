import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import library


class LibraryTests(unittest.TestCase):
    def setUp(self):
        fixture = patch.object(library, "CATEGORIES", ["AI与技术", "学习与成长", "职业与商业", "生活与实用", "文化与观点", "娱乐", "其他"])
        fixture.start()
        self.addCleanup(fixture.stop)
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [patch.object(library,"ROOT",root),patch.object(library,"RUNTIME",root/"runtime"),patch.object(library,"DB",root/"runtime"/"library.sqlite3")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def add(self, video_id="7000000000000000001", baseline=False):
        with library.database() as db:
            return library.ingest(db,[{"aweme_id":video_id,"description":"旧视频今天新增点赞","author":"测试作者"}],baseline)

    def ready(self,video_id="7000000000000000001",text="原话，不能凭空补充。"):
        self.add(video_id)
        import hashlib
        with library.database() as db:
            db.execute("update videos set status='ready',transcript=?,transcript_hash=? where id=?",(text,hashlib.sha256(text.encode()).hexdigest(),video_id))

    def test_baseline_and_duplicate_never_download(self):
        self.assertEqual(self.add(baseline=True),1)
        self.assertEqual(self.add(baseline=False),0)
        with library.database() as db:
            row=db.execute("select * from videos").fetchone()
            self.assertEqual(row["status"],"baseline")
            self.assertEqual(row["baseline"],1)
            self.assertTrue(row["discovered_at"])
        self.assertEqual(self.add("7000000000000000002"),1)
        self.assertEqual(library.stats()["counts"]["queued"],1)

    def test_export_marks_uncertain_asr_without_changing_raw_or_hash(self):
        import hashlib
        video_id = "7000000000000000001"
        text = "这段原文有\ufffd，后面的内容仍保留。"
        self.ready(video_id, text)
        with library.database() as db:
            library.export_video(db, video_id)
            row = db.execute('select transcript,transcript_hash,status from videos where id=?', (video_id,)).fetchone()
        self.assertEqual(tuple(row), (text, hashlib.sha256(text.encode()).hexdigest(), 'ready'))
        note = (library.ROOT/'knowledge'/('like-'+video_id+'.md')).read_text(encoding='utf-8')
        self.assertIn('这段原文有[识别不清]，后面的内容仍保留。', note)
        self.assertIn('原始正文与文件完整保留', note)
        self.assertNotIn('\ufffd', note)

    def test_cached_uncertain_transcript_resumes_without_recognition(self):
        video_id = '7000000000000000001'
        self.add(video_id)
        media = library.RUNTIME/'media'/(video_id+'.mp4')
        media.parent.mkdir(); media.write_bytes(b'cached-media')
        saved = library.RUNTIME/'transcripts'/(video_id+'.json')
        saved.parent.mkdir()
        result = {'transcript':'原文\ufffd待核对','duration_seconds':10,'elapsed_seconds':1}
        saved.write_text(json.dumps(result),encoding='utf-8')
        with patch.object(library.local_whisper,'_local_model'), patch.object(library.local_whisper,'transcribe_file') as asr, patch.object(library.local_whisper,'_download') as download:
            self.assertEqual(library._transcribe_queue(scope='new',video_ids=[video_id])['completed'],1)
            asr.assert_not_called(); download.assert_not_called()
        with library.database() as db:
            row = db.execute('select transcript,status from videos where id=?',(video_id,)).fetchone()
        self.assertEqual(tuple(row),('原文\ufffd待核对','ready'))
        self.assertFalse(media.exists())
        self.assertEqual(json.loads(saved.read_text(encoding='utf-8'))['transcript'],result['transcript'])

    def test_register_audited_only_selected_missing_and_preserves_existing(self):
        from register_audited import register
        old, new, excluded = [str(7000000000000000000+i) for i in range(1,4)]
        self.ready(old)
        path = library.RUNTIME/'audit.json'
        payload = {'range_complete':True,'target_found':True,'target_id':old,'items':[
            {'id':new,'title':'指定新增','author':'作者'},
            {'id':excluded,'title':'名单中未选择','author':'作者'},
            {'id':old,'title':'不能覆盖旧标题','author':'作者'},
        ]}
        path.write_text(json.dumps(payload),encoding='utf-8')
        self.assertEqual(register(path,[old,new])['added'],[new])
        self.assertEqual(register(path,[old,new])['added'],[])
        from unittest.mock import AsyncMock
        with patch.object(library,'refresh_media_metadata',new=AsyncMock(return_value={new})) as refresh:
            self.assertEqual(register(path,[new],refresh_media=True)['media_refreshed'],[new])
            refresh.assert_awaited_once_with([new])
        with library.database() as db:
            rows = {r['id']:r for r in db.execute('select * from videos')}
            self.assertEqual(set(rows),{old,new})
            self.assertEqual(rows[old]['status'],'ready')
            self.assertEqual(rows[old]['transcript'],'原话，不能凭空补充。')
            self.assertNotEqual(json.loads(rows[old]['metadata'])['title'],'不能覆盖旧标题')
            self.assertEqual(rows[new]['status'],'queued')
            self.assertEqual(db.execute('select count(*) from backfill_items').fetchone()[0],0)
        with self.assertRaises(ValueError):
            register(path,['7000000000000000099'])

    def test_new_queue_excludes_history_and_does_not_duplicate_pending_items(self):
        history="7000000000000000001"
        new="7000000000000000002"
        self.ready(history)
        with library.database() as db:
            db.execute('insert into backfill_items values(?,?,?)',(history,1,library.now()))
        older=library.pending_batch('backfill')
        self.ready(new)
        newer=library.pending_batch('new')
        self.assertNotEqual(older['batch_id'],newer['batch_id'])
        self.assertEqual(json.loads(Path(older['path']).read_text(encoding='utf-8'))['items'][0]['id'],history)
        self.assertEqual(json.loads(Path(newer['path']).read_text(encoding='utf-8'))['items'][0]['id'],new)
        self.assertEqual(library.pending_batch('new')['batch_id'],newer['batch_id'])

    def test_latest_priority_blocks_older_work_until_asr_and_ai_finish(self):
        latest, older, history = [str(7000000000000000000+i) for i in range(1,4)]
        self.add(older)
        self.ready(history)
        self.add(latest)
        with library.database() as db:
            db.execute('insert into backfill_items values(?,?,?)',(history,1,library.now()))
        report={'range_complete':True,'target_found':True,'target_id':latest,'items':[{'id':latest}]}
        (library.RUNTIME/'display-likes-order.json').write_text(json.dumps(report),encoding='utf-8')
        self.assertEqual(library.pending_batch('backfill')['status'],'latest_in_progress')
        self.assertEqual(library.pending_batch('new')['status'],'waiting_for_transcription')
        with patch.object(library.local_whisper,'_local_model') as model:
            self.assertEqual(library._transcribe_queue(scope='backfill')['status'],'latest_in_progress')
            model.assert_not_called()
        self.ready(latest)
        batch=library.pending_batch()
        self.assertEqual([i['id'] for i in json.loads(Path(batch['path']).read_text(encoding='utf-8'))['items']],[latest])
        self.assertEqual(library.pending_batch('backfill')['status'],'latest_in_progress')
        self.assertEqual(library.pending_batch('new',video_ids=[older])['status'],'latest_in_progress')
        with library.database() as db:
            db.execute("update videos set status='done' where id=?",(latest,))
        self.assertEqual(library.pending_batch('backfill')['status'],'latest_in_progress')
        with library.database() as db:
            db.execute("update batches set state='done' where id=?",(batch['batch_id'],))
        self.assertEqual(library.pending_batch('backfill')['status'],'older_queue_in_progress')
        self.ready(older)
        batch=library.pending_batch()
        self.assertEqual(json.loads(Path(batch['path']).read_text(encoding='utf-8'))['items'][0]['id'],older)
        with library.database() as db:
            db.execute("update videos set status='done' where id=?",(older,))
            db.execute("update batches set state='done' where id=?",(batch['batch_id'],))
        self.assertEqual(library.pending_batch('backfill')['count'],1)

    def test_latest_asr_selection_never_downloads_older_queue(self):
        latest, older = [str(7000000000000000000+i) for i in range(1,3)]
        self.add(older)
        self.add(latest)
        report={'range_complete':True,'target_found':True,'target_id':latest,'items':[{'id':latest}]}
        (library.RUNTIME/'display-likes-order.json').write_text(json.dumps(report),encoding='utf-8')
        saved=library.RUNTIME/'transcripts'/(latest+'.json')
        saved.parent.mkdir()
        saved.write_text(json.dumps({'transcript':'最新口播','duration_seconds':3,'elapsed_seconds':1}),encoding='utf-8')
        with patch.object(library.local_whisper,'_local_model'),patch.object(library.local_whisper,'_download') as download:
            self.assertEqual(library._transcribe_queue()['completed'],1)
            download.assert_not_called()
        with library.database() as db:
            self.assertEqual(db.execute('select status from videos where id=?',(older,)).fetchone()[0],'queued')

    def test_daily_freezes_latest_head_and_preserves_old_groups(self):
        from unittest.mock import AsyncMock
        newest, overlap, older, history = [str(7000000000000000000+i) for i in range(1,5)]
        self.ready(overlap)
        self.add(older)
        self.ready(history)
        with library.database() as db:
            library.setting(db,'baseline_complete',library.now())
            db.execute("update videos set status='done' where id in (?,?)",(overlap,history))
            db.execute('insert into backfill_items values(?,?,?)',(history,1,library.now()))
        frozen=library.RUNTIME/'audit.json'
        frozen.write_text(json.dumps({'range_complete':True,'target_found':True,'target_id':history,'items':[{'id':overlap},{'id':history}]}),encoding='utf-8')
        library.prioritize_audit(frozen)
        class Collector:
            def __init__(self,**kwargs): pass
            async def open(self,**kwargs): pass
            async def close(self): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def fetch_page(self,**kwargs):
                return {'ok':True,'has_more':False,'items':[{'aweme_id':i} for i in (newest,overlap,older,history)]}
        with patch.object(library.browser_collector,'BrowserCollector',Collector):
            result=asyncio.run(library.scan())
        report=json.loads(Path(result['priority_audit']).read_text(encoding='utf-8'))
        self.assertEqual([i['id'] for i in report['items']],[newest,overlap,history])
        self.assertEqual(library.audit_ids(library.RUNTIME/'display-likes-order.json'),[newest,overlap,history])
        self.assertEqual(library.pending_batch('new',video_ids=library.audit_ids(result['priority_audit']))['status'],'waiting_for_transcription')
        with library.database() as db:
            self.assertEqual(db.execute('select status from videos where id=?',(older,)).fetchone()[0],'queued')
            self.assertEqual(db.execute('select count(*) from backfill_items').fetchone()[0],1)

    def test_running_older_asr_yields_at_next_item_when_latest_arrives(self):
        first, second, latest = [str(7000000000000000000+i) for i in range(1,4)]
        for video_id in (first,second):
            self.add(video_id)
            saved=library.RUNTIME/'transcripts'/(video_id+'.json')
            saved.parent.mkdir(exist_ok=True)
            saved.write_text(json.dumps({'transcript':'旧队列已保存原文','duration_seconds':3,'elapsed_seconds':1}),encoding='utf-8')
        audit=library.RUNTIME/'new-audit.json'
        audit.write_text(json.dumps({'range_complete':True,'target_found':True,'target_id':latest,'items':[{'id':latest}]}),encoding='utf-8')
        original_export=library.export_video
        def export_and_select(db,video_id):
            original_export(db,video_id)
            library.atomic_write_json(library.RUNTIME/'display-likes-order.json',json.loads(audit.read_text(encoding='utf-8')))
        with patch.object(library.local_whisper,'_local_model'),patch.object(library,'export_video',side_effect=export_and_select):
            result=library._transcribe_queue(scope='new')
        self.assertEqual((result['status'],result['completed']),('waiting_for_collection',1))
        with library.database() as db:
            self.assertEqual(dict(db.execute('select id,status from videos')), {first:'ready',second:'queued'})

    def test_authorized_backfill_can_continue_after_daily_new_limit(self):
        history="7000000000000000001"
        self.ready(history)
        with library.database() as db:
            db.execute('insert into backfill_items values(?,?,?)',(history,1,library.now()))
            payload=json.dumps({'items':[{'id':str(7100000000000000000+i)} for i in range(500)]})
            db.execute("insert into batches(id,day,payload,state) values(?,?,?,'done')",('used',library.today(),payload))
        self.assertEqual(library.pending_batch('new')['status'],'daily_ai_limit')
        self.assertEqual(library.pending_batch('backfill')['count'],1)
        with library.database() as db:
            db.execute("update videos set status='done' where id=?",(history,))
        self.assertEqual(library.pending_batch('new')['status'],'daily_ai_limit')

    def test_history_transcription_barrier_and_daily_resume(self):
        from unittest.mock import AsyncMock
        import wait_backfill
        history='7000000000000000001'
        self.add(history)
        with library.database() as db:
            db.execute('insert into backfill_items values(?,?,?)',(history,1,library.now()))
        self.assertEqual(library.pending_batch('backfill')['status'],'waiting_for_transcription')
        with patch.object(wait_backfill,'DB',library.DB):
            self.assertEqual(wait_backfill.wait(0)['status'],'waiting_for_transcription')
        with library.database() as db:
            self.assertEqual(db.execute('select count(*) from batches').fetchone()[0],0)
        with patch('sys.argv',['library.py','daily']),patch.object(library,'scan',new=AsyncMock(return_value={'status':'synced'})) as scan,patch.object(library,'transcribe_queue',return_value={}) as asr:
            self.assertEqual(library.main(),0)
            scan.assert_awaited_once()
            asr.assert_called_once_with('small',500,'new',[])
            scan.reset_mock(); asr.reset_mock()
            with library.database() as db:
                db.execute("update videos set status='done' where id=?",(history,))
            with patch.object(wait_backfill,'DB',library.DB):
                self.assertEqual(wait_backfill.wait(0)['phase'],'complete')
            self.assertEqual(library.main(),0)
            scan.assert_awaited_once()
            asr.assert_called_once_with('small',500,'new',[])

    def test_backfill_exact_snapshot_is_idempotent_and_does_not_expand(self):
        for i in range(301):
            self.add(str(7000000000000000000+i),baseline=True)
        class Collector:
            def __init__(self,**kwargs): pass
            async def open(self,**kwargs): pass
            async def close(self): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def fetch_page(self,cursor,count):
                return {"ok":True,"cursor":cursor+50,"has_more":True,"items":[{"aweme_id":str(7000000000000000000+i),"description":"历史"} for i in range(cursor,cursor+50)]}
        from unittest.mock import AsyncMock
        with patch.object(library.browser_collector,"BrowserCollector",Collector),patch.object(library.asyncio,"sleep",new=AsyncMock()):
            self.assertEqual(asyncio.run(library.select_backfill())["count"],300)
            self.assertEqual(asyncio.run(library.select_backfill())["status"],"already_selected")
        self.assertEqual(library.stats()["counts"],{"baseline":1,"queued":300})
        with library.database() as db:
            self.assertEqual(db.execute("select video_id from backfill_items order by rank limit 1").fetchone()[0],"7000000000000000000")
        note=(library.ROOT/"knowledge"/"like-7000000000000000000.md").read_text(encoding="utf-8")
        self.assertIn("不是今日新增点赞",note)

    def test_incomplete_backfill_never_promotes_history(self):
        self.add(baseline=True)
        class Collector:
            def __init__(self,**kwargs): pass
            async def open(self,**kwargs): pass
            async def close(self): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def fetch_page(self,**kwargs):
                return {"ok":True,"has_more":False,"items":[{"aweme_id":"7000000000000000001","description":"历史"}]}
        with patch.object(library.browser_collector,"BrowserCollector",Collector):
            with self.assertRaisesRegex(ValueError,"backfill_requires_300"):
                asyncio.run(library.select_backfill())
        self.assertEqual(library.stats()["counts"],{"baseline":1})
        self.assertEqual(library.stats()["backfill_300"],{})

    def test_ai_batch_cap_and_atomic_stale_rejection(self):
        for i in range(6):
            self.ready(str(7000000000000000001+i),"原始正文"*400)
        batch=library.pending_batch()
        payload=json.loads(Path(batch["path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(payload["items"]),5)
        self.assertLessEqual(sum(len(item["transcript"]) for item in payload["items"]),12000)
        self.assertEqual(library.pending_batch(),batch)
        result={"batch_id":payload["batch_id"],"items":[{"id":item["id"],"clean":"原始正文","summary":"博主讲原始正文。","points":["原始正文"],"category":"其他","kind":"口播","check_note":""} for item in payload["items"]]}
        path=library.RUNTIME/"analysis.json"
        path.write_text(json.dumps(result),encoding="utf-8")
        with library.database() as db:
            db.execute("update videos set transcript_hash='changed' where id=?",(payload["items"][-1]["id"],))
        with self.assertRaisesRegex(ValueError,"stale_transcript"):
            library.apply_analysis(path)
        self.assertNotIn("done",library.stats()["counts"])

    def test_apply_preserves_raw_and_is_idempotent(self):
        self.ready()
        batch=library.pending_batch()
        result={"batch_id":batch["batch_id"],"items":[{"id":"7000000000000000001","clean":"原话。不能凭空补充。","summary":"博主强调保留原话。","points":["保留原话"],"category":"学习与成长","kind":"知识口播","check_note":""}]}
        path=library.RUNTIME/"analysis.json"
        path.write_text(json.dumps(result),encoding="utf-8")
        self.assertEqual(library.apply_analysis(path)["count"],1)
        self.assertEqual(library.apply_analysis(path)["status"],"already_applied")
        with library.database() as db:
            self.assertEqual(db.execute("select transcript from videos").fetchone()[0],"原话，不能凭空补充。")
        self.assertTrue((library.ROOT/"knowledge"/"like-7000000000000000001.md").exists())

    def test_long_text_is_segmented_completely(self):
        text="长内容。"*7000
        self.ready(text=text)
        batch=library.pending_batch()
        payload=json.loads(Path(batch["path"]).read_text(encoding="utf-8"))
        segments=[Path(p).read_text(encoding="utf-8") for p in payload["items"][0]["segment_paths"]]
        self.assertEqual("".join(segments),text)
        self.assertTrue(all(len(part)<=10000 for part in segments))

    def test_partial_scan_does_not_enable_baseline(self):
        class Collector:
            def __init__(self,**kwargs):
                self.pages=iter([{"ok":True,"cursor":20,"has_more":True,"items":[{"aweme_id":"7000000000000000001","description":"历史"}]},
                                 {"ok":False,"status_code":8}])
            async def open(self,**kwargs): pass
            async def close(self): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def fetch_page(self,**kwargs): return next(self.pages)
        from unittest.mock import AsyncMock
        with patch.object(library.browser_collector,"BrowserCollector",Collector), patch.object(library.asyncio,"sleep",new=AsyncMock()):
            with self.assertRaisesRegex(ValueError,"collection_stopped"):
                asyncio.run(library.scan())
        state=library.stats()
        self.assertNotIn("baseline_complete",state["settings"])
        self.assertEqual(state["counts"],{"baseline":1})

    def test_complete_scan_enables_baseline_without_enqueue(self):
        class Collector:
            def __init__(self,**kwargs): pass
            async def open(self,**kwargs): pass
            async def close(self): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def fetch_page(self,**kwargs): return {"ok":True,"cursor":0,"has_more":False,"items":[{"aweme_id":"7000000000000000001","description":"历史"}]}
        with patch.object(library.browser_collector,"BrowserCollector",Collector):
            self.assertEqual(asyncio.run(library.scan())["status"],"baseline_created")
        self.assertIn("baseline_complete",library.stats()["settings"])
        self.assertEqual(library.stats()["counts"],{"baseline":1})

    def test_incremental_scan_counts_ids_resets_known_pages_and_full_is_explicit(self):
        from unittest.mock import AsyncMock
        items=[{'aweme_id':str(7600000000000000000+i)} for i in range(2000)]
        new_id='7700000000000000001'
        with library.database() as db:
            library.ingest(db,items,True)
            library.setting(db,'baseline_complete','existing_baseline')
            library.setting(db,'last_scan','previous_full_scan')
        class Collector:
            def __init__(self,**kwargs): pass
            async def open(self,**kwargs): pass
            async def close(self): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def fetch_page(self,cursor,count):
                page=cursor+1
                rows=items[cursor*100:page*100]
                if page==12:
                    rows=rows+[{'aweme_id':new_id,'description':'以前发布、现在新增点赞'}]
                return {'ok':True,'cursor':page,'has_more':page<20,'items':rows}
        with patch.object(library.browser_collector,'BrowserCollector',Collector),patch.object(library.asyncio,'sleep',new=AsyncMock()):
            result=asyncio.run(library.scan())
            self.assertEqual((result['pages'],result['added'],result['stop_reason']),(15,1,'known_pages'))
            self.assertFalse(result['full_scan_complete'])
            self.assertGreaterEqual(result['ids_checked'],1000)
            self.assertEqual(library.stats()['settings']['last_scan'],'previous_full_scan')
            repeat=asyncio.run(library.scan())
            self.assertEqual((repeat['pages'],repeat['added']),(13,0))
            full=asyncio.run(library.scan(full=True))
            self.assertEqual((full['pages'],full['mode'],full['stop_reason']),(20,'full','end_of_list'))
            self.assertTrue(full['full_scan_complete'])
        with library.database() as db:
            self.assertEqual(tuple(db.execute('select baseline,status from videos where id=?',(new_id,)).fetchone()),(0,'queued'))

    def test_incremental_scan_caps_pages_without_counting_duplicate_ids_twice(self):
        from unittest.mock import AsyncMock
        items=[{'aweme_id':str(7800000000000000000+i)} for i in range(20)]
        with library.database() as db:
            library.ingest(db,items,True)
            library.setting(db,'baseline_complete','existing_baseline')
        class Collector:
            def __init__(self,**kwargs): pass
            async def open(self,**kwargs): pass
            async def close(self): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def fetch_page(self,cursor,count):
                return {'ok':True,'cursor':cursor+1,'has_more':True,'items':items}
        with patch.object(library.browser_collector,'BrowserCollector',Collector),patch.object(library.asyncio,'sleep',new=AsyncMock()):
            result=asyncio.run(library.scan())
        self.assertEqual((result['pages'],result['ids_checked'],result['stop_reason']),(100,20,'page_limit'))
        self.assertFalse(result['full_scan_complete'])

    def test_targeted_audit_stops_at_known_anchor_without_changing_queue(self):
        import audit_likes
        from types import SimpleNamespace
        items=[{'aweme_id':str(7900000000000000000+i)} for i in range(5)]
        with library.database() as db:
            library.ingest(db,items,True)
        calls=[]
        class Collector:
            def __init__(self,**kwargs): self._page=SimpleNamespace(on=lambda *args:None)
            async def open(self,**kwargs): pass
            async def close(self): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def fetch_page(self,cursor,count):
                calls.append((cursor,count))
                return {'ok':True,'cursor':1,'has_more':False,'items':items}
        with patch.object(library.browser_collector,'BrowserCollector',Collector),patch.object(audit_likes,'RUNTIME',library.RUNTIME):
            found=asyncio.run(audit_likes.audit(items[2]['aweme_id']))
            self.assertTrue(found['range_complete'])
            self.assertEqual(found['unique_items'],3)
            report=json.loads(Path(found['path']).read_text(encoding='utf-8'))
            self.assertEqual([i['id'] for i in report['items']],[i['aweme_id'] for i in items[:3]])
            self.assertTrue(all(i['baseline']==1 and i['local_status']=='baseline' for i in report['items']))
            missing=asyncio.run(audit_likes.audit('7999999999999999999'))
            self.assertFalse(missing['range_complete'])
            self.assertEqual(missing['stop_reason'],'end_of_list')
        self.assertEqual(calls,[(0,20),(0,20)])
        self.assertEqual(library.stats()['counts'],{'baseline':5})
        with library.database() as db:
            self.assertEqual(db.execute('select count(*) from attempts').fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from batches').fetchone()[0],0)

    def test_daily_limit_and_quiet_queue_do_not_load_model(self):
        self.ready()
        with library.database() as db:
            db.execute("update videos set status='queued'")
            db.executemany("insert into attempts(day,video_id,started_at,status) values(?,?,?,'failed')",[(library.today(),'x',library.now())]*500)
        with patch.object(library.local_whisper,"_local_model") as model:
            self.assertEqual(library.transcribe_queue()["completed"],0)
            model.assert_not_called()

    def test_new_ai_uses_last_slot_then_stops_at_500(self):
        for i in range(6):
            self.ready(str(7300000000000000000+i))
        with library.database() as db:
            payload=json.dumps({'items':[{'id':str(7400000000000000000+i)} for i in range(499)]})
            db.execute("insert into batches(id,day,payload,state) values(?,?,?,'done')",('used',library.today(),payload))
        batch=library.pending_batch('new')
        self.assertEqual(batch['count'],1)
        with library.database() as db:
            db.execute("update batches set state='done' where id=?",(batch['batch_id'],))
        self.assertEqual(library.pending_batch('new')['status'],'daily_ai_limit')

    def test_new_transcription_uses_last_slot_then_stops_at_500(self):
        with library.database() as db:
            library.ingest(db,[{'aweme_id':str(7500000000000000000+i),'media_kind':'图文'} for i in range(2)],False)
            db.executemany("insert into attempts(day,video_id,started_at,status) values(?,?,?,'failed')",[(library.today(),'x',library.now())]*499)
        with patch.object(library.local_whisper,'_local_model'),patch.object(library,'export_daily',return_value={}):
            library.transcribe_queue(scope='new')
        with library.database() as db:
            self.assertEqual(db.execute('select count(*) from attempts').fetchone()[0],500)
        self.assertEqual(library.stats()['counts']['untranscribed'],1)
        with patch.object(library.local_whisper,'_local_model') as model:
            self.assertEqual(library.transcribe_queue(scope='new')['completed'],0)
            model.assert_not_called()
        self.assertEqual(library.stats()['counts']['queued'],1)

    def test_success_is_verified_before_media_cleanup_and_not_recomputed(self):
        self.add()
        with library.database() as db:
            raw=json.loads(db.execute("select metadata from videos").fetchone()[0])
            raw['play_url']='https://example.test/video.mp4'
            db.execute("update videos set metadata=?",(json.dumps(raw),))
        def download(url,path,maximum):
            path.write_bytes(b'test_media')
            return None
        result={'transcript':'完整原始转写。','transcript_status':'success','transcript_source':'local_whisper','duration_seconds':10,'elapsed_seconds':1}
        with patch.object(library.local_whisper,'_local_model'),patch.object(library.local_whisper,'_download',side_effect=download) as downloader,patch.object(library.local_whisper,'transcribe_file',return_value=result) as asr:
            self.assertEqual(library.transcribe_queue()['completed'],1)
            self.assertFalse((library.RUNTIME/'media'/'7000000000000000001.mp4').exists())
            self.assertEqual((library.RUNTIME/'transcripts'/'7000000000000000001.txt').read_text(encoding='utf-8'),'完整原始转写。')
            self.assertEqual(library.transcribe_queue()['completed'],0)
            self.assertEqual(downloader.call_count,1)
            self.assertEqual(asr.call_count,1)

    def test_detail_metadata_uses_video_audio_source_and_checks_response(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
        video_id = '7000000000000000001'
        payload = {'status_code':0,'aweme_detail':{'aweme_id':video_id,'desc':'口播',
            'video':{'duration':12300,'play_addr':{'url_list':['https://example.test/video'] }},
            'music':{'play_url':{'uri':'https://example.test/music'}},'statistics':{'collect_count':123}}}
        response = SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id='+video_id,
                                   status=200,json=AsyncMock(return_value=payload))
        class Expected:
            async def __aenter__(self):
                future = asyncio.get_running_loop().create_future()
                future.set_result(response)
                return SimpleNamespace(value=future)
            async def __aexit__(self,*args): return False
        page = SimpleNamespace(goto=AsyncMock())
        def expect(predicate,timeout):
            self.assertTrue(predicate(response))
            self.assertFalse(predicate(SimpleNamespace(url='https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=7000000000000000002')))
            self.assertFalse(predicate(SimpleNamespace(url='https://example.test/aweme/v1/web/aweme/detail/')))
            return Expected()
        page.expect_response = expect
        collector = SimpleNamespace(_page=page)
        result = asyncio.run(library.fetch_video_metadata(collector,video_id))
        self.assertEqual(result['play_url'],'https://example.test/video')
        self.assertEqual(result['duration_seconds'],12.3)
        self.assertEqual(result['statistics']['collect_count'],123)
        payload['aweme_detail']['aweme_id'] = 'wrong'
        self.assertIsNone(asyncio.run(library.fetch_video_metadata(collector,video_id)))
        response.status = 429
        with self.assertRaisesRegex(ValueError,'collection_stopped'):
            asyncio.run(library.fetch_video_metadata(collector,video_id))
        response.status = 200
        payload['status_code'] = 8
        with self.assertRaisesRegex(ValueError,'collection_stopped'):
            asyncio.run(library.fetch_video_metadata(collector,video_id))
        page.goto.side_effect = PlaywrightTimeoutError('timeout')
        payload['status_code'] = 0
        payload['aweme_detail']['aweme_id'] = video_id
        self.assertEqual(asyncio.run(library.fetch_video_metadata(collector,video_id))['aweme_id'],video_id)
        page.goto.side_effect = PlaywrightError('Page.goto: Navigation is interrupted by another navigation')
        self.assertEqual(asyncio.run(library.fetch_video_metadata(collector,video_id))['aweme_id'],video_id)
        page.goto.side_effect = PlaywrightError('Target page, context or browser has been closed')
        with self.assertRaisesRegex(PlaywrightError,'browser has been closed'):
            asyncio.run(library.fetch_video_metadata(collector,video_id))
        page.goto.side_effect = PlaywrightTimeoutError('timeout')
        class MissingResponse(Expected):
            async def __aexit__(self,*args): raise PlaywrightTimeoutError('response timeout')
        page.expect_response = lambda predicate,timeout: MissingResponse()
        with self.assertRaisesRegex(ValueError,'media_metadata_lookup_timed_out'):
            asyncio.run(library.fetch_video_metadata(collector,video_id))

    def test_media_refresh_continues_after_item_timeout_but_stops_for_login(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        first, second = '7000000000000000001','7000000000000000002'
        self.add(first); self.add(second)
        collector = SimpleNamespace(open=AsyncMock(),authenticated=AsyncMock(return_value=True),close=AsyncMock())
        item = {'aweme_id':second,'play_url':'https://example.test/fresh','media_kind':'视频'}
        with patch.object(library.browser_collector,'BrowserCollector',return_value=collector),patch.object(library,'fetch_video_metadata',new=AsyncMock(side_effect=[ValueError('media_metadata_lookup_timed_out'),item])) as fetch,patch.object(library.asyncio,'sleep',new=AsyncMock()):
            self.assertEqual(asyncio.run(library.refresh_media_metadata([first,second])),{second})
            self.assertEqual(fetch.await_count,2)
        with library.database() as db:
            self.assertEqual(tuple(db.execute('select status,error from videos where id=?',(first,)).fetchone()),('queued','media_metadata_lookup_timed_out'))
        with patch.object(library.browser_collector,'BrowserCollector',return_value=collector),patch.object(library,'fetch_video_metadata',new=AsyncMock(side_effect=ValueError('collection_stopped: video_detail_http=429'))) as fetch:
            with self.assertRaisesRegex(ValueError,'collection_stopped'):
                asyncio.run(library.refresh_media_metadata([first,second]))
            self.assertEqual(fetch.await_count,1)

    def test_direct_media_refresh_preserves_existing_content_and_scope(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        video_id, other = '7000000000000000001','7000000000000000002'
        self.ready(video_id); self.add(other)
        with library.database() as db:
            db.execute('insert into backfill_items values(?,?,?)',(video_id,1,library.now()))
            before = tuple(db.execute('select status,transcript,transcript_hash,discovered_at from videos where id=?',(video_id,)).fetchone())
            other_before = tuple(db.execute('select * from videos where id=?',(other,)).fetchone())
        collector = SimpleNamespace(open=AsyncMock(),authenticated=AsyncMock(return_value=True),close=AsyncMock())
        item = {'aweme_id':video_id,'play_url':'https://example.test/fresh','description':'口播','media_kind':'视频'}
        with patch.object(library.browser_collector,'BrowserCollector',return_value=collector),patch.object(library,'fetch_video_metadata',new=AsyncMock(return_value=item)) as fetch,patch.object(library.asyncio,'sleep',new=AsyncMock()):
            self.assertEqual(asyncio.run(library.refresh_media_metadata([video_id,video_id])),{video_id})
            fetch.assert_awaited_once_with(collector,video_id)
            collector.close.assert_awaited_once()
        with library.database() as db:
            self.assertEqual(tuple(db.execute('select status,transcript,transcript_hash,discovered_at from videos where id=?',(video_id,)).fetchone()),before)
            self.assertEqual(tuple(db.execute('select * from videos where id=?',(other,)).fetchone()),other_before)
            self.assertEqual(db.execute('select count(*) from backfill_items').fetchone()[0],1)
            self.assertEqual(json.loads(db.execute('select metadata from videos where id=?',(video_id,)).fetchone()[0])['play_url'],item['play_url'])

    def test_expired_links_are_refreshed_immediately_before_each_download(self):
        ids=['7000000000000000001','7000000000000000002']
        for video_id in ids:
            self.add(video_id)
            with library.database() as db:
                meta=json.loads(db.execute('select metadata from videos where id=?',(video_id,)).fetchone()[0])
                meta['play_url']='https://example.test/old/'+video_id
                db.execute('update videos set metadata=? where id=?',(json.dumps(meta),video_id))
        fresh_urls = []
        async def refresh(video_ids):
            with library.database() as db:
                for video_id in video_ids:
                    meta=json.loads(db.execute('select metadata from videos where id=?',(video_id,)).fetchone()[0])
                    meta['play_url']='https://example.test/fresh/'+video_id
                    db.execute('update videos set metadata=? where id=?',(json.dumps(meta),video_id))
                    fresh_urls.append(meta['play_url'])
            return set(video_ids)
        def download(url,path,maximum):
            if '/old/' in url: return 'http_403' if url.endswith(ids[0]) else 'http_410'
            # A URL refreshed before later lookups may expire before download.
            if url != fresh_urls[-1]: return 'http_403'
            path.write_bytes(b'media')
            return None
        result={'transcript':'真实口播','duration_seconds':10,'elapsed_seconds':1}
        with patch.object(library.local_whisper,'_local_model'),patch.object(library,'refresh_media_metadata',side_effect=refresh) as refresh_call,patch.object(library.local_whisper,'_download',side_effect=download) as downloader,patch.object(library.local_whisper,'transcribe_file',return_value=result):
            self.assertEqual(library.transcribe_queue()['completed'],2)
            self.assertEqual(refresh_call.call_count,2)
            self.assertEqual(downloader.call_count,4)
            self.assertEqual(library.stats()['counts'],{'ready':2})

    def test_metadata_refresh_failure_is_isolated_to_current_video(self):
        ids = ['7000000000000000001','7000000000000000002','7000000000000000003']
        for video_id in ids:
            self.add(video_id)
            with library.database() as db:
                meta = json.loads(db.execute('select metadata from videos where id=?',(video_id,)).fetchone()[0])
                meta['play_url'] = 'https://example.test/expired/' + video_id
                db.execute('update videos set metadata=? where id=?',(json.dumps(meta),video_id))
        async def refresh_metadata(video_ids):
            if ids[0] in video_ids:
                raise ValueError('media_detail_http_500')
            with library.database() as db:
                for video_id in video_ids:
                    meta = json.loads(db.execute('select metadata from videos where id=?',(video_id,)).fetchone()[0])
                    meta['play_url'] = 'https://example.test/fresh/' + video_id
                    db.execute('update videos set metadata=? where id=?',(json.dumps(meta),video_id))
            return set(video_ids)
        def download_media(url,path,maximum):
            if '/expired/' in url: return 'http_403'
            path.write_bytes(b'media')
            return None
        result = {'transcript':'真实口播','duration_seconds':10,'elapsed_seconds':1}
        with patch.object(library.local_whisper,'_local_model'),patch.object(library.local_whisper,'_download',side_effect=download_media) as download,patch.object(library,'refresh_media_metadata',side_effect=refresh_metadata) as refresh,patch.object(library.local_whisper,'transcribe_file',return_value=result) as asr:
            self.assertEqual(library.transcribe_queue()['completed'],2)
            self.assertEqual(refresh.call_count,3)
            self.assertEqual(download.call_count,5)
            self.assertEqual(asr.call_count,2)
        with library.database() as db:
            self.assertEqual(db.execute('select error from videos where id=?',(ids[0],)).fetchone()[0],'media_detail_http_500')
            self.assertEqual(library.stats()['counts'],{'failed':1,'ready':2})

    def test_fresh_link_download_failure_is_not_retried_forever(self):
        video_id = '7000000000000000001'
        self.add(video_id)
        with library.database() as db:
            meta = json.loads(db.execute('select metadata from videos where id=?',(video_id,)).fetchone()[0])
            meta['play_url'] = 'https://example.test/expired'
            db.execute('update videos set metadata=? where id=?',(json.dumps(meta),video_id))
        with patch.object(library.local_whisper,'_local_model'),patch.object(library.local_whisper,'_download',return_value='http_410') as download,patch.object(library,'refresh_media_metadata',return_value={video_id}) as refresh,patch.object(library.local_whisper,'transcribe_file') as asr:
            self.assertEqual(library.transcribe_queue()['completed'],0)
            refresh.assert_called_once_with([video_id])
            self.assertEqual(download.call_count,2)
            asr.assert_not_called()
        with library.database() as db:
            self.assertEqual(tuple(db.execute('select status,error from videos where id=?',(video_id,)).fetchone()),('failed','media_download_http_410'))

    def test_download_keeps_http_status_and_empty_asr_is_not_no_audio(self):
        import urllib.error
        failure=urllib.error.HTTPError('https://example.test',403,'Forbidden',{},None)
        with patch.object(library.local_whisper.urllib.request,'urlopen',side_effect=failure):
            self.assertEqual(library.local_whisper._download('https://example.test',library.RUNTIME/'unused',1024),'http_403')
        self.add()
        media=library.RUNTIME/'media'/'7000000000000000001.mp4'
        media.parent.mkdir(); media.write_bytes(b'media')
        result={'transcript':'','transcript_status':'recognition_empty','duration_seconds':10,'elapsed_seconds':1}
        with patch.object(library.local_whisper,'_local_model'),patch.object(library.local_whisper,'transcribe_file',return_value=result):
            library.transcribe_queue()
        with library.database() as db:
            row=db.execute('select status,kind,error from videos').fetchone()
            self.assertEqual(row['status'],'untranscribed')
            self.assertEqual(row['kind'],'未识别出文字')
            self.assertIn('尚不能判定',row['error'])


    def test_expanded_history_counts_exports_and_transcribes_past_300(self):
        with library.database() as db:
            items=[{'aweme_id':str(7200000000000000000+i),'description':'历史图文','media_kind':'图文'} for i in range(1000)]
            library.ingest(db,items,False)
            db.executemany('insert into backfill_items values(?,?,?)',[(item['aweme_id'],rank,library.now()) for rank,item in enumerate(items,1)])
            db.execute("update videos set status='done',summary='已有摘要' where id=?",(items[0]['aweme_id'],))
            library.export_backfill(db)
        state=library.stats()
        self.assertEqual(state['backfill_count'],1000)
        self.assertEqual(sum(state['backfill_300'].values()),300)
        self.assertEqual(sum(state['backfill_after_300'].values()),700)
        folder=library.ROOT/'knowledge'
        self.assertIn('全部历史回填 · 1000条',(folder/'历史回填整理.md').read_text(encoding='utf-8'))
        extra=(folder/'后续历史整理.md').read_text(encoding='utf-8')
        self.assertIn('实际收录700条',extra)
        self.assertIn('第301–1000条',extra)
        with patch.object(library.local_whisper,'_local_model'),patch.object(library,'export_video'),patch.object(library,'export_daily',return_value={}):
            library.transcribe_queue(limit=301,scope='backfill')
        with library.database() as db:
            self.assertEqual(db.execute('select count(*) from attempts').fetchone()[0],301)
            row=db.execute('select status,summary from videos where id=?',(items[0]['aweme_id'],)).fetchone()
            self.assertEqual(tuple(row),('done','已有摘要'))

    def test_video_without_audio_does_not_run_recognizer(self):
        import av
        path=Path(self.temp.name)/'silent.mp4'
        with av.open(str(path),'w') as container:
            stream=container.add_stream('mpeg4',rate=1)
            stream.width=16; stream.height=16; stream.pix_fmt='yuv420p'
            frame=av.VideoFrame(16,16,'yuv420p')
            for packet in stream.encode(frame): container.mux(packet)
            for packet in stream.encode(): container.mux(packet)
        with patch.object(library.local_whisper,'_local_model') as model:
            result=library.local_whisper.transcribe_file(path)
            model.assert_not_called()
        self.assertFalse(result['audio_present'])
        self.assertEqual(result['transcript_status'],'no_audio')

    def test_missing_media_url_refreshes_current_video_once(self):
        video_id='7000000000000000001'
        self.add(video_id)
        async def refresh(ids):
            with library.database() as db:
                library.ingest(db,[{'aweme_id':video_id,'play_url':'https://example.test/fresh','description':'真实口播'}],False)
            return set(ids)
        def download(url,path,maximum):
            path.write_bytes(b'video'); return None
        result={'transcript':'有效口播。','duration_seconds':5,'elapsed_seconds':1}
        with patch.object(library.local_whisper,'_local_model'),patch.object(library,'refresh_media_metadata',side_effect=refresh) as metadata,patch.object(library.local_whisper,'_download',side_effect=download) as media,patch.object(library.local_whisper,'transcribe_file',return_value=result):
            self.assertEqual(library.transcribe_queue(scope='new')['completed'],1)
            metadata.assert_called_once_with([video_id]); media.assert_called_once()
        with library.database() as db:
            self.assertEqual(db.execute('select status from videos where id=?',(video_id,)).fetchone()[0],'ready')
        self.assertFalse((library.RUNTIME/'media'/(video_id+'.mp4')).exists())

    def test_refreshed_image_is_not_reported_as_download_failure(self):
        video_id='7000000000000000001'; self.add(video_id)
        async def refresh(ids):
            with library.database() as db:
                library.ingest(db,[{'aweme_id':video_id,'media_kind':'图文'}],False)
            return set(ids)
        with patch.object(library.local_whisper,'_local_model'),patch.object(library,'refresh_media_metadata',side_effect=refresh),patch.object(library.local_whisper,'_download') as download,patch.object(library.local_whisper,'transcribe_file') as asr:
            library.transcribe_queue(scope='new'); download.assert_not_called(); asr.assert_not_called()
        with library.database() as db:
            self.assertEqual(tuple(db.execute('select status,kind from videos where id=?',(video_id,)).fetchone()),('untranscribed','图文'))

    def test_authentication_stop_keeps_queue_and_reports_stopped(self):
        ids=['7000000000000000001','7000000000000000002']
        for video_id in ids: self.add(video_id)
        with patch.object(library.local_whisper,'_local_model'),patch.object(library,'refresh_media_metadata',side_effect=ValueError('login_required: test')) as refresh,patch.object(library.local_whisper,'transcribe_file') as asr:
            result=library.transcribe_queue(scope='new')
            self.assertEqual(result['status'],'stopped'); self.assertIn('login_required',result['error']); self.assertEqual(result['completed'],0)
            refresh.assert_called_once_with([ids[0]]); asr.assert_not_called()
        with library.database() as db:
            self.assertEqual(db.execute('select status from videos where id=?',(ids[1],)).fetchone()[0],'queued')
            self.assertIn('login_required',dict(db.execute('select key,value from settings'))['collection_error'])

    def test_media_lookup_timeout_remains_the_failure_reason(self):
        video_id='7000000000000000001'; self.add(video_id)
        async def refresh(ids):
            with library.database() as db: db.execute('update videos set error=? where id=?',('media_metadata_lookup_timed_out',video_id))
            return set()
        with patch.object(library.local_whisper,'_local_model'),patch.object(library,'refresh_media_metadata',side_effect=refresh),patch.object(library.local_whisper,'transcribe_file') as asr:
            library.transcribe_queue(scope='new'); asr.assert_not_called()
        with library.database() as db:
            self.assertEqual(db.execute('select error from videos where id=?',(video_id,)).fetchone()[0],'media_metadata_lookup_timed_out')

    def test_asr_lock_owner_closes_stale_attempts_before_resume(self):
        self.add()
        with library.database() as db: db.execute("insert into attempts(day,video_id,started_at,status) values(?,?,?,'running')",('old-day','7000000000000000001','old-time'))
        with patch.object(library.local_whisper,'_local_model') as model:
            library.transcribe_queue(limit=0); model.assert_not_called()
        with library.database() as db:
            self.assertEqual(db.execute('select status from attempts').fetchone()[0],'interrupted')
            self.assertEqual(db.execute('select status from videos').fetchone()[0],'queued')


    def test_reapplying_committed_ai_repairs_failed_markdown_export(self):
        self.ready();batch=library.pending_batch()
        result={'batch_id':batch['batch_id'],'items':[{'id':'7000000000000000001','clean':'原话。','summary':'来自博主内容。','points':['保留原话'],'category':'学习与成长','kind':'知识口播','check_note':''}]}
        path=library.RUNTIME/'analysis.json';path.write_text(json.dumps(result),encoding='utf-8')
        with patch.object(library,'export_video',side_effect=OSError('disk temporarily unavailable')):
            with self.assertRaises(OSError): library.apply_analysis(path)
        with library.database() as db:
            self.assertEqual(db.execute('select state from batches').fetchone()[0],'done')
        self.assertEqual(library.apply_analysis(path)['status'],'already_applied')
        self.assertIn('来自博主内容。',(library.ROOT/'knowledge'/'like-7000000000000000001.md').read_text(encoding='utf-8'))
        with library.database() as db:
            self.assertEqual(db.execute('select count(*) from batches').fetchone()[0],1)
            self.assertEqual(db.execute('select transcript from videos').fetchone()[0],'原话，不能凭空补充。')


if __name__ == "__main__":
    unittest.main()
