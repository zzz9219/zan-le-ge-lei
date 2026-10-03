import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import library
import onboarding
from douyin_favorites_knowledge import core_bridge


class PersonalTopicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for obj, name, value in [(library,"ROOT",root),(library,"RUNTIME",root/"runtime"),(library,"DB",root/"runtime/library.sqlite3"),(onboarding,"RUNTIME",root/"runtime")]:
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def seed(self, count=200, text="春天如何种番茄。"):
        with library.database() as db:
            for i in range(count):
                video_id = str(7100000000000000000+i)
                library.ingest(db,[{"aweme_id":video_id,"description":text}],False)
                db.execute("insert into backfill_items values(?,?,?)",(video_id,i+1,library.now()))
                db.execute("update videos set status='ready',transcript=? where id=?",(text,video_id))

    def proposal(self, names):
        rows,digest=onboarding.sample()
        path=onboarding.RUNTIME/'themes.json'
        core_bridge.atomic_write_json(path,{"sample_hash":digest,"reviewed_ids":[r['id'] for r in rows],"categories":names})
        return path

    def test_new_user_has_no_preset_topics_and_saves_personalized_themes(self):
        with library.database() as db:
            self.assertEqual(library.category_names(db),["未分类"])
        self.seed()
        result=onboarding.context()
        evidence=[item for path in result['paths'] for item in json.loads(Path(path).read_text(encoding='utf-8'))['items']]
        self.assertEqual(len(evidence),200)
        self.assertEqual(onboarding.apply_themes(self.proposal(['园艺','烹饪']))['categories'],['未分类','园艺','烹饪'])
        with library.database() as db:
            self.assertEqual(library.category_names(db),['未分类','园艺','烹饪'])

    def test_incomplete_sample_review_does_not_install_topics(self):
        self.seed()
        path=self.proposal(['园艺'])
        value=json.loads(path.read_text(encoding='utf-8'));value['reviewed_ids'].pop()
        core_bridge.atomic_write_json(path,value)
        with self.assertRaisesRegex(ValueError,'not_fully_reviewed'):
            onboarding.apply_themes(path)
        with library.database() as db:
            self.assertEqual(library.category_names(db),['未分类'])

    def test_sample_change_and_existing_theme_replacement_are_rejected(self):
        self.seed(2)
        path=self.proposal(['数码','电影'])
        onboarding.apply_themes(path)
        with self.assertRaisesRegex(ValueError,'already_initialized'):
            onboarding.apply_themes(self.proposal(['园艺']))
        value=json.loads(path.read_text(encoding='utf-8'));value['sample_hash']='stale'
        core_bridge.atomic_write_json(path,value)
        with self.assertRaisesRegex(ValueError,'sample_changed'):
            onboarding.apply_themes(path)

    def test_long_evidence_is_explicit_and_raw_transcript_stays_complete(self):
        text='长内容原始转写。'*2500
        self.seed(30,text)
        result=onboarding.context()
        for path in result['paths']:
            group=json.loads(Path(path).read_text(encoding='utf-8'))['items']
            self.assertLessEqual(sum(len(json.dumps(i,ensure_ascii=False)) for i in group),12000)
            self.assertTrue(all(i['excerpt_only'] and i['original_characters']==len(text) for i in group))
        with library.database() as db:
            self.assertEqual(db.execute('select transcript from videos limit 1').fetchone()[0],text)

    def test_waits_for_asr_before_theme_inference(self):
        self.seed(2)
        with library.database() as db:
            db.execute("update videos set status='queued'")
        self.assertEqual(onboarding.context(),{'status':'waiting_for_transcription','remaining':2})
        with self.assertRaisesRegex(ValueError,'waiting_for_transcription'):
            onboarding.apply_themes(self.proposal(['园艺']))

    def test_fewer_than_200_likes_are_allowed_only_when_list_ends(self):
        class Collector:
            def __init__(self, **kwargs): pass
            async def open(self, **kwargs): pass
            async def navigate(self): pass
            async def authenticated(self): return True
            async def close(self): pass
            async def fetch_page(self, **kwargs):
                return {'ok':True,'items':[{'aweme_id':str(7100000000000000000+i),'description':'园艺'} for i in range(3)],'has_more':False}
        with patch.object(library.browser_collector,'BrowserCollector',Collector):
            self.assertEqual(asyncio.run(library.select_backfill(200,allow_short=True))['count'],3)
            self.assertEqual(asyncio.run(library.select_backfill(200,allow_short=True))['status'],'already_selected')

    def test_personal_topics_and_manual_addition_reach_the_web_api(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        import web
        self.seed(2)
        onboarding.apply_themes(self.proposal(['园艺','烹饪']))
        with patch.object(web,'ROOT',library.ROOT):
            server=ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url='http://127.0.0.1:'+str(server.server_port)
            try:
                with urllib.request.urlopen(url+'/api/status') as response:
                    self.assertEqual(json.load(response)['categories'],['未分类','园艺','烹饪'])
                request=urllib.request.Request(url+'/api/categories',data=json.dumps({'name':'旅行'}).encode(),headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:19423'})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)['categories'],['未分类','园艺','烹饪','旅行'])
            finally:
                server.shutdown();server.server_close();thread.join()


class StdlibCoreTests(unittest.TestCase):
    def test_unicode_bytes_are_preserved_and_proprietary_module_is_unused(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'中文.txt';text='原始转写\n第二行。\r\n'
            core_bridge.atomic_write_text(path,text)
            self.assertEqual(path.read_bytes(),text.encode('utf-8'))
            self.assertEqual(core_bridge.file_sha256(path),hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(list(Path(folder).glob('.中文.txt.*')),[])
        self.assertNotIn('douyin_knowledge_core',sys.modules)
        self.assertEqual(core_bridge.canonical_json({'b':2,'a':'中文'}),core_bridge.canonical_json({'a':'中文','b':2}))

    def test_profile_lock_excludes_another_process_and_is_reusable(self):
        code="from pathlib import Path;import sys;from douyin_favorites_knowledge.core_bridge import profile_lock\ntry:\n with profile_lock(Path(sys.argv[1]),blocking=False): pass\nexcept ValueError:\n sys.exit(7)"
        with tempfile.TemporaryDirectory() as folder:
            with core_bridge.profile_lock(folder):
                result=subprocess.run([sys.executable,'-c',code,folder],capture_output=True)
                self.assertEqual(result.returncode,7,result.stderr.decode())
            result=subprocess.run([sys.executable,'-c',code,folder],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())


if __name__=='__main__':
    unittest.main()
