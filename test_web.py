from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import library
import web
import enrichment
import asyncio
from web import Handler


class WebTests(unittest.TestCase):
    def test_latest_phase_message_takes_priority_over_history(self):
        import subprocess
        code = """
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const line=fs.readFileSync(process.argv[1],'utf8').split(String.fromCharCode(10)).find(x=>x.startsWith('function workflowText(')),context={};
vm.runInNewContext(line,context);
const historical={queued:100,ready:30};
assert.match(context.workflowText({backfill:historical,latest_workflow:{phase:'transcribing',remaining_transcriptions:2}},{}),/新增集中转写.*2/);
assert.match(context.workflowText({backfill:historical,latest_workflow:{phase:'organizing',ready:3}},{}),/新增集中提炼.*3/);
assert.match(context.workflowText({backfill:historical,latest_workflow:{phase:'complete'}},{}),/历史集中转写/);
"""
        result=subprocess.run(['node','-e',code,str(Path(__file__).with_name('index.html'))],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)

    def setUp(self):
        fixture=patch.object(library, "CATEGORIES", ["AI与技术", "学习与成长", "职业与商业", "生活与实用", "文化与观点", "娱乐", "其他"])
        fixture.start()
        self.addCleanup(fixture.stop)

    def test_filter_feedback_precedes_fetch_and_stale_results_do_not_replace_cards(self):
        script = """
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8'),calls=[],requests=[],rendered=[];
const nodes=Object.fromEntries(['items','loadIndicator','refresh','search','sort'].map(id=>[id,{value:id==='sort'?'source':'',textContent:'',dataset:{},setAttribute(k,v){this[k]=v}}]));
const context={loadVersion:0,libraryStatus:{cached:true},statusRequest:null,category:'',view:'all',offset:0,URLSearchParams,
 $:id=>nodes[id],metricQuery:()=>({}),syncFilterStates:()=>calls.push('selection'),
 renderLibraryStatus:()=>calls.push('status-render'),renderCards:data=>rendered.push(data.id),
 get:url=>{calls.push(url);if(url==='/api/status')return Promise.resolve({fresh:true});return new Promise((resolve,reject)=>requests.push({resolve,reject}))}};
vm.runInNewContext(html.slice(html.indexOf('async function load('),html.indexOf('async function openDetail(')),context);
(async()=>{
 const older=context.load(),newer=context.load();
 assert.equal(calls[0],'selection');assert.equal(nodes.items['aria-busy'],'true');assert.equal(calls.filter(c=>c==='/api/status').length,0);
 requests[1].resolve({id:'newer'});await newer;requests[0].resolve({id:'older'});await older;
 assert.deepEqual(rendered,['newer']);assert.equal(nodes.items['aria-busy'],'false');assert.equal(nodes.loadIndicator.textContent,'');
 const stale=context.load(),refresh=context.load({refreshStatus:true});requests[3].resolve({id:'refreshed'});await refresh;requests[2].reject(Error('stale network error'));await stale;
 assert.deepEqual(rendered,['newer','refreshed']);assert.equal(calls.filter(c=>c==='/api/status').length,1);assert.equal(calls.filter(c=>c==='status-render').length,1);assert.equal(nodes.loadIndicator.textContent,'');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
        result=enrichment.subprocess.run([enrichment.shutil.which('node'),'-e',script,str(web.ROOT/'index.html')],capture_output=True,encoding='utf-8',timeout=10,creationflags=enrichment.subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_comment_timeout_shows_seconds_and_retains_partial_progress(self):
        script = """
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8'),context={COMMENT_LIMIT:1000};
vm.runInNewContext(html.slice(html.indexOf('function commentErrorText('),html.indexOf('function showJob(')),context);
const value=context.jobText({action:'comments',status:'partial',result:{top_comments:47,error:'Request timeout after 89000ms'}});
assert.match(value,/保留 47 条/);assert.match(value,/89 秒/);assert.doesNotMatch(value,/89000ms|采集完成/);
assert.equal(context.commentErrorText('captcha'),'captcha');
"""
        result=enrichment.subprocess.run([enrichment.shutil.which('node'),'-e',script,str(web.ROOT/'index.html')],capture_output=True,encoding='utf-8',timeout=10,creationflags=enrichment.subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_comments_share_the_existing_library_with_ten_row_pages_and_high_likes(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder, patch.object(enrichment, 'COMMENTS', Path(folder)):
            path=Path(folder)/'data'/'douyin_comments.sqlite';path.parent.mkdir()
            with sqlite3.connect(path) as db:
                db.executescript("create table videos(aweme_id text,top_comments integer,last_status text,last_collected_at text,last_error text); create table comments(aweme_id text,comment_id text,text text,nickname text,likes integer,note text,tags text,level integer); create table settings(key text,value text); insert into settings values('high_like_threshold','100');")
                db.execute("insert into videos values('7000000000000000001',1101,'completed','today',null)")
                db.executemany("insert into comments values('7000000000000000001',?,'正文','昵称',?,'保留备注','保留标签',1)",[(str(n), n if n < 1100 else -1) for n in range(1101)])
                db.execute("insert into comments values('7000000000000000001','reply','回复','昵称',9999,'','',2)")
            db.close()
            first=enrichment.comment_rows('7000000000000000001')
            second=enrichment.comment_rows('7000000000000000001',10)
            tail=enrichment.comment_rows('7000000000000000001',1100)
            self.assertEqual((first['total'],len(first['items'])),(1101,10))
            self.assertTrue(set(x['comment_id'] for x in first['items']).isdisjoint(x['comment_id'] for x in second['items']))
            self.assertEqual(tail['items'][0]['likes'],-1)
            self.assertFalse(tail['items'][0]['high_like'])
            self.assertEqual(first['items'][0]['note'],'保留备注')
            self.assertTrue(first['items'][0]['high_like'])

    def test_comment_collection_uses_the_checked_video_connection_and_does_not_retry(self):
        from unittest.mock import Mock
        completed=Mock(returncode=0)
        completed.communicate.return_value = ('{"status":"completed","top_comments":1}', '')
        with patch.object(enrichment,'comment_connection',return_value='checked-tab') as connection, patch.object(enrichment.shutil,'which',return_value='node'), patch.object(enrichment.subprocess,'Popen',return_value=completed) as run, patch.object(enrichment,'job',{}):
            self.assertEqual(enrichment.collect_comments('7000000000000000001')['top_comments'],1)
            connection.assert_called_once_with('7000000000000000001')
            args = run.call_args.args[0]
            self.assertEqual(args[args.index('--connection-id')+1],'checked-tab')
            self.assertEqual(args[args.index('--timeout-ms')+1],'90000')
            self.assertEqual(args[args.index('--max-comments')+1],'1000')
            self.assertEqual(args[args.index('--page-size')+1],'50')
            run.assert_called_once()
        with patch.object(enrichment,'comment_connection',return_value=None),patch.object(enrichment.subprocess,'Popen') as run,patch.object(enrichment,'job',{}):
            with self.assertRaisesRegex(ValueError,'comment_browser_not_connected'):
                enrichment.collect_comments('7000000000000000001')
            run.assert_not_called()

    def test_live_comment_progress_uses_checkpoint_for_the_requested_video_only(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(enrichment,'job',{'status':'running'}):
            path = Path(folder)
            checkpoint = path/'checkpoint.json'
            checkpoint.write_text(json.dumps({'aweme_id':'7000000000000000001','top_comments':120,'top_pages':3,'updated_at':'today'}),encoding='utf-8')
            enrichment.update_comment_progress(path,'7000000000000000001')
            self.assertEqual((enrichment.job['top_comments'],enrichment.job['top_pages']),(120,3))
            checkpoint.write_text(json.dumps({'aweme_id':'other','top_comments':9000}),encoding='utf-8')
            enrichment.update_comment_progress(path,'7000000000000000001')
            self.assertEqual(enrichment.job['top_comments'],120)
            checkpoint.write_text('{',encoding='utf-8')
            enrichment.update_comment_progress(path,'7000000000000000001')
            self.assertEqual(enrichment.job['top_comments'],120)

    def test_comment_connection_allows_browser_startup_and_normal_bridge_response_time(self):
        from unittest.mock import Mock
        with patch.object(enrichment,'comment_browser_id',''),patch.object(enrichment,'ensure_comment_bridge'),patch.object(enrichment.shutil,'which',return_value='node'),patch.object(enrichment.subprocess,'run',return_value=Mock(returncode=0,stdout='{"connection_id":"checked-tab"}')) as run:
            self.assertEqual(enrichment.comment_connection('7000000000000000001'),'checked-tab')
            self.assertEqual(run.call_args.kwargs['timeout'],60)
            self.assertEqual(run.call_args.args[0][-2:],['7000000000000000001',''])
            self.assertEqual(enrichment.comment_connection('7000000000000000002'),'checked-tab')
            self.assertEqual(run.call_args.args[0][-2:],['7000000000000000002','checked-tab'])

    def test_comment_entry_post_and_markdown_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(library,'ROOT',root),patch.object(library,'RUNTIME',root/'runtime'),patch.object(library,'DB',root/'runtime'/'library.sqlite3'):
                with library.database() as db:
                    library.ingest(db,[{'aweme_id':'7000000000000000001','description':'视频'}],False)
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                url='http://127.0.0.1:'+str(server.server_port)
                try:
                    with patch.object(enrichment,'start_job',return_value={'status':'queued'}) as start:
                        request=urllib.request.Request(url+'/api/enrich',data=b'{"action":"comments","id":"7000000000000000001"}',headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:19423'})
                        with urllib.request.urlopen(request) as response:
                            self.assertEqual(response.status,202)
                        start.assert_called_once_with('comments','7000000000000000001',False)
                    with patch.object(enrichment,'comment_export',return_value='# 一级评论\n\n已保存') as export:
                        with urllib.request.urlopen(url+'/api/comments/export?id=7000000000000000001') as response:
                            self.assertEqual(response.read().decode('utf-8'),'# 一级评论\n\n已保存')
                            self.assertIn('.md',response.headers['Content-Disposition'])
                        export.assert_called_once_with('7000000000000000001')
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(url+'/api/comments/export?id=invalid')
                    self.assertEqual(error.exception.code,400)
                finally:
                    server.shutdown();server.server_close();thread.join()

    def test_comment_bridge_starts_only_if_offline_and_reuses_online_service(self):
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as folder, patch.object(enrichment,'ROOT',Path(folder)):
            (Path(folder)/'runtime').mkdir()
            response=MagicMock()
            response.__enter__.return_value.read.return_value=b'{"connections":{}}'
            with patch.object(enrichment,'urlopen',side_effect=[OSError('offline'),response]), patch.object(enrichment.shutil,'which',return_value='node'), patch.object(enrichment.subprocess,'Popen') as start:
                self.assertEqual(enrichment.ensure_comment_bridge(),{'connections':{}})
                self.assertEqual(start.call_count,1)
                self.assertEqual(start.call_args.kwargs['creationflags'],enrichment.subprocess.CREATE_NO_WINDOW)
            with patch.object(enrichment,'urlopen',return_value=response),patch.object(enrichment.subprocess,'Popen') as start:
                self.assertEqual(enrichment.ensure_comment_bridge(),{'connections':{}})
                start.assert_not_called()

    def test_recent_audit_group_precedes_history_and_overlap_stays_historical(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(library,'ROOT',root),patch.object(web,'ROOT',root),patch.object(library,'RUNTIME',root/'runtime'),patch.object(library,'DB',root/'runtime'/'library.sqlite3'):
                ids=[str(7000000000000000000+i) for i in range(1,5)]
                with library.database() as db:
                    library.ingest(db,[{'aweme_id':i,'description':'视频'} for i in ids],False)
                    for rank,vid in enumerate(ids[:2],1):
                        db.execute('insert into backfill_items values(?,?,?)',(vid,rank,library.now()))
                    db.execute("update videos set status='done' where id=?",(ids[2],))
                (root/'runtime'/'display-likes-order.json').write_text(json.dumps({'range_complete':True,'target_found':True,'items':[{'id':ids[3]},{'id':ids[2]},{'id':ids[0]}]}),encoding='utf-8')
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                url='http://127.0.0.1:'+str(server.server_port)
                try:
                    with urllib.request.urlopen(url+'/api/videos') as response:
                        rows=json.load(response)['items']
                    self.assertEqual([r['id'] for r in rows],[ids[3],ids[2],ids[0],ids[1]])
                    self.assertEqual([r['current_like_rank'] for r in rows],[1,2,None,None])
                    self.assertTrue(rows[2]['historical'])
                    with urllib.request.urlopen(url+'/api/video?id='+ids[3]) as response:
                        self.assertEqual(json.load(response)['current_like_rank'],1)
                    with urllib.request.urlopen(url+'/api/videos?view=done') as response:
                        self.assertEqual(json.load(response)['items'][0]['current_like_rank'],2)
                finally:
                    server.shutdown();server.server_close();thread.join()
    def test_metrics_snapshot_filters_and_unknown(self):
        from urllib.parse import urlencode
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(library, 'ROOT', root), patch.object(library, 'RUNTIME', root/'runtime'), patch.object(library, 'DB', root/'runtime'/'library.sqlite3'):
                with library.database() as db:
                    library.ingest(db, [
                        {'aweme_id':'7000000000000000001','description':'高收藏','statistics':{'collect_count':101,'digg_count':999,'comment_count':12,'share_count':0},'create_time':1790985600,'statistics_observed_at':'2026-10-03T00:00:00Z'},
                        {'aweme_id':'7000000000000000002','description':'未知'},
                        {'aweme_id':'7000000000000000003','description':'临界','statistics':{'collect_count':100}},
                    ], False)
                server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
                thread = threading.Thread(target=server.serve_forever,daemon=True)
                thread.start()
                url = 'http://127.0.0.1:'+str(server.server_port)
                try:
                    with urllib.request.urlopen(url+'/api/videos?'+urlencode({'min_collect_count':101,'published_from':'2026-10-03','published_to':'2026-10-03'})) as response:
                        data = json.load(response)
                    self.assertEqual(data['total'], 1)
                    self.assertTrue(data['items'][0]['metrics']['deep_dive_recommended'])
                    self.assertEqual(data['items'][0]['metrics']['share_count'], 0)
                    with urllib.request.urlopen(url+'/api/video?id=7000000000000000002') as response:
                        self.assertIsNone(json.load(response)['metrics']['collect_count'])
                    with urllib.request.urlopen(url+'/api/videos?min_collect_count=0') as response:
                        self.assertEqual(json.load(response)['total'],2)
                    for query in ('min_collect_count=-1','published_from=garbage'):
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            urllib.request.urlopen(url+'/api/videos?'+query)
                        self.assertEqual(error.exception.code,400)
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(urllib.request.Request(url+'/api/enrich',data=b'{"action":"metrics"}',headers={'Content-Type':'application/json'}))
                    self.assertEqual(error.exception.code,403)
                finally:
                    server.shutdown(); server.server_close(); thread.join()

    def test_metadata_refresh_preserves_transcription_and_only_existing_ids(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(library, 'ROOT', root), patch.object(library, 'RUNTIME', root/'runtime'), patch.object(library, 'DB', root/'runtime'/'library.sqlite3'):
                with library.database() as db:
                    library.ingest(db,[{'aweme_id':'7000000000000000001','description':'已有'}],False)
                    db.execute("update videos set status='transcribing',transcript='原文',clean='清理稿',category='AI与技术'")
                collector = AsyncMock()
                collector.authenticated.return_value = True
                collector.fetch_page.return_value = {'ok':True,'has_more':False,'items':[
                    {'aweme_id':'7000000000000000001','statistics':{'collect_count':102},'create_time':1790985600,'statistics_observed_at':'snapshot'},
                    {'aweme_id':'7000000000000000002','statistics':{'collect_count':999}},
                ]}
                with patch.object(enrichment.browser_collector,'BrowserCollector',return_value=collector), patch.object(enrichment,'job',{}):
                    self.assertEqual(asyncio.run(enrichment.refresh_metrics())['updated'],1)
                with library.database() as db:
                    rows=db.execute('select * from videos').fetchall()
                    self.assertEqual(len(rows),1)
                    self.assertEqual((rows[0]['status'],rows[0]['transcript'],rows[0]['clean'],rows[0]['category']),('transcribing','原文','清理稿','AI与技术'))
                    self.assertEqual(json.loads(rows[0]['metadata'])['statistics']['collect_count'],102)

    def test_metrics_job_refuses_busy_pipeline_without_running_collector(self):
        from unittest.mock import Mock
        import time
        with patch.object(enrichment,'job',{'status':'idle'}), patch.object(enrichment.time,'monotonic',side_effect=[0,1801]), patch.object(enrichment,'process_lock',side_effect=ValueError('another_pipeline_is_running')), patch.object(enrichment,'collect_comments') as collect:
            enrichment.start_job('metrics')
            for _ in range(100):
                if enrichment.job.get('finished_at'):
                    break
                time.sleep(.01)
            self.assertEqual(enrichment.job['status'],'failed')
            self.assertEqual(enrichment.job['error'],'another_pipeline_is_running')
            collect.assert_not_called()

    def test_comment_job_does_not_wait_for_transcription_locks(self):
        from contextlib import nullcontext
        import time
        calls=[]
        def lock(*args):
            calls.append(args)
            if args != ('comments.lock',):
                raise ValueError('another_pipeline_is_running')
            return nullcontext()
        with patch.object(enrichment,'job',{'status':'idle'}),patch.object(enrichment,'process_lock',side_effect=lock),patch.object(enrichment.time,'sleep'),patch.object(enrichment,'collect_comments',return_value={'status':'completed'}) as collect:
            enrichment.start_job('comments','7000000000000000001')
            for _ in range(100):
                if enrichment.job.get('finished_at'):
                    break
                threading.Event().wait(.01)
            self.assertEqual(enrichment.job['status'],'completed')
            self.assertNotIn('waiting_for',enrichment.job)
            self.assertEqual(calls,[('comments.lock',)])
            collect.assert_called_once_with('7000000000000000001',False)

    def test_connection_check_does_not_wait_for_transcription_locks(self):
        from contextlib import nullcontext
        def lock(*args):
            if args != ('comments.lock',):
                raise ValueError('another_pipeline_is_running')
            return nullcontext()
        with patch.object(enrichment,'job',{'status':'idle'}),patch.object(enrichment,'process_lock',side_effect=lock),patch.object(enrichment,'comment_connection',return_value='checked-tab') as connect:
            enrichment.start_job('connect-comments','7000000000000000001')
            for _ in range(100):
                if enrichment.job.get('finished_at'): break
                threading.Event().wait(.01)
            self.assertEqual(enrichment.job['status'],'completed')
            self.assertTrue(enrichment.job['result']['connected'])
            connect.assert_called_once_with('7000000000000000001')

    def test_search_filter_and_category_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(library,'ROOT',root),patch.object(library,'RUNTIME',root/'runtime'),patch.object(library,'DB',root/'runtime'/'library.sqlite3'):
                with library.database() as db:
                    library.ingest(db,[{'aweme_id':'7000000000000000001','description':'AI学习视频','author':'作者'}],False)
                    library.ingest(db,[{'aweme_id':'7000000000000000002','description':'历史隐藏'}],True)
                    db.execute("update videos set transcript='向量检索',category='AI与技术' where baseline=0")
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True)
                thread.start()
                url='http://127.0.0.1:'+str(server.server_port)
                try:
                    from urllib.parse import urlencode
                    with urllib.request.urlopen(url+'/api/videos?'+urlencode({'q':'向量','category':'AI与技术'})) as response:
                        result=json.load(response)
                    self.assertEqual(result['total'],1)
                    self.assertTrue(result['items'][0]['has_transcript'])
                    self.assertNotIn('transcript',result['items'][0])
                    with urllib.request.urlopen(url+'/api/video?id=7000000000000000001') as response:
                        self.assertEqual(json.load(response)['transcript'],'向量检索')
                    with self.assertRaises(urllib.error.HTTPError) as hidden:
                        urllib.request.urlopen(url+'/api/video?id=7000000000000000002')
                    self.assertEqual(hidden.exception.code,404)
                    data=json.dumps({'id':'7000000000000000001','category':'学习与成长'}).encode()
                    with self.assertRaises(urllib.error.HTTPError) as rejection:
                        urllib.request.urlopen(urllib.request.Request(url+'/api/category',data=data,headers={'Content-Type':'application/json'}))
                    self.assertEqual(rejection.exception.code,403)
                    request=urllib.request.Request(url+'/api/category',data=data,headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:19423'})
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(json.load(response)['updated'],1)
                    with library.database() as db:
                        self.assertEqual(db.execute("select category from videos where baseline=0").fetchone()[0],'学习与成长')
                    def post(path,value):
                        request=urllib.request.Request(url+path,data=json.dumps(value).encode(),headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:19423'})
                        with urllib.request.urlopen(request) as response:
                            return json.load(response)
                    for _ in range(2):
                        names=post('/api/categories',{'name':'  工具评测  '})['categories']
                        self.assertEqual(names.count('工具评测'),1)
                        self.assertEqual(names[:7],library.CATEGORIES)
                    self.assertEqual(post('/api/category',{'id':'7000000000000000001','category':'工具评测'})['updated'],1)
                    with urllib.request.urlopen(url+'/api/status') as response:
                        self.assertIn('工具评测',json.load(response)['categories'])
                    with urllib.request.urlopen(url+'/api/videos?'+urlencode({'category':'工具评测'})) as response:
                        self.assertEqual(json.load(response)['total'],1)
                    with library.database() as db:
                        self.assertEqual(tuple(db.execute('select status,transcript,category from videos where baseline=0').fetchone()),('queued','向量检索','工具评测'))
                        db.execute('insert into backfill_items values(?,?,?)',('7000000000000000001',1,library.now()))
                        library.export_backfill(db)
                    self.assertIn('## 工具评测',(root/'knowledge'/'首批历史整理.md').read_text(encoding='utf-8'))
                    for invalid in ['', 'x'*25, 123, '含\n换行']:
                        with self.assertRaises(urllib.error.HTTPError) as error:
                            post('/api/categories',{'name':invalid})
                        self.assertEqual(error.exception.code,400)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()

    def test_default_order_preserves_fixed_likes_rank_and_filters_status(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(library,'ROOT',root),patch.object(library,'RUNTIME',root/'runtime'),patch.object(library,'DB',root/'runtime'/'library.sqlite3'):
                with library.database() as db:
                    library.ingest(db,[{'aweme_id':str(7000000000000000000+i),'description':'视频'} for i in range(1,4)],False)
                    for i,rank in [(1,3),(2,1),(3,2)]:
                        db.execute('insert into backfill_items values(?,?,?)',(str(7000000000000000000+i),rank,'2026-10-02'))
                    db.execute("update videos set status='done' where id='7000000000000000001'")
                server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True)
                thread.start()
                url='http://127.0.0.1:'+str(server.server_port)
                try:
                    with urllib.request.urlopen(url+'/api/videos') as response:
                        self.assertEqual([x['source_rank'] for x in json.load(response)['items']],[1,2,3])
                    with urllib.request.urlopen(url+'/api/videos?sort=readable') as response:
                        self.assertEqual(json.load(response)['items'][0]['source_rank'],3)
                    with urllib.request.urlopen(url+'/api/videos?view=done') as response:
                        self.assertEqual(json.load(response)['total'],1)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


    def test_cold_list_load_is_independent_of_slow_or_failed_status(self):
        script="""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8'),nodes=Object.fromEntries(['items','loadIndicator','refresh','search','sort','notice'].map(id=>[id,{value:'',textContent:'',dataset:{},setAttribute(k,v){this[k]=v}}]));
let resolveStatus,rejectStatus,reads=0;const rendered=[];
const context={loadVersion:0,libraryStatus:null,statusRequest:null,category:'',view:'all',offset:0,URLSearchParams,$:id=>nodes[id],metricQuery:()=>({}),syncFilterStates:()=>{},renderLibraryStatus:()=>rendered.push('status'),renderCards:data=>rendered.push(data.id),get:url=>url==='/api/status'?(reads++,new Promise((resolve,reject)=>{resolveStatus=resolve;rejectStatus=reject})):Promise.resolve({id:'cards'})};
vm.runInNewContext(html.slice(html.indexOf('async function load('),html.indexOf('async function openDetail(')),context);
(async()=>{await context.load();await context.load();assert.deepEqual(rendered,['cards','cards']);assert.equal(reads,1);assert.equal(nodes.items['aria-busy'],'false');resolveStatus({counts:{}});await new Promise(setImmediate);assert.deepEqual(rendered,['cards','cards','status']);await context.load({refreshStatus:true});rejectStatus(Error('statistics offline'));await new Promise(setImmediate);assert.deepEqual(rendered,['cards','cards','status','cards']);assert.match(nodes.notice.textContent,/仍可阅读/);})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result=enrichment.subprocess.run([enrichment.shutil.which('node'),'-e',script,str(web.ROOT/'index.html')],capture_output=True,encoding='utf-8',timeout=10,creationflags=enrichment.subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_bad_post_shapes_return_400_without_starting_jobs(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(library,'ROOT',Path(folder)),patch.object(library,'RUNTIME',Path(folder)/'runtime'),patch.object(library,'DB',Path(folder)/'runtime/library.sqlite3'):
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with patch.object(enrichment,'start_job') as start:
                    for path,value in [('/api/categories',[]),('/api/category',{'category':'AI与技术'}),('/api/category',{'id':[],'category':'AI与技术'}),('/api/enrich',{'id':[],'action':'comments'})]:
                        request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,data=json.dumps(value).encode(),headers={'Content-Type':'application/json','Origin':'http://127.0.0.1:19423'})
                        with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as error:
                            urllib.request.urlopen(request)
                        self.assertEqual(error.exception.code,400)
                    start.assert_not_called()
            finally: server.shutdown();server.server_close();thread.join()


if __name__=='__main__':
    unittest.main()
