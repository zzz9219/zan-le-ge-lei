const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');

const {
  BridgeClient,
  bridgeConnectionIndex,
  collectComments,
  deriveTopic,
  extractAwemeId,
  parseArgs,
  resolveVideoReference,
  writeOutputs,
} = require('./collect_comments');
const { openDatabase, saveCollection } = require('./comment_library');

test('opening the HTML file redirects to the knowledge service without using file-origin APIs', async () => {
  const vm = require('node:vm');
  const html = await fs.readFile(path.join(__dirname, '..', 'mobile', 'index.html'), 'utf8');
  let target;
  vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], {
    location: { protocol: 'file:', replace: url => { target = url; } },
  });
  assert.equal(target, 'http://127.0.0.1:19423/');
});

test('the knowledge entry can preselect a connection without switching to a more active tab', async () => {
  const options = parseArgs(['https://www.douyin.com/video/7345678901234567890', '--connection-id', 'selected-tab']);
  assert.equal(options.connectionId, 'selected-tab');
  assert.throws(() => parseArgs(['7345678901234567890', '--connection-id']), /连接 ID/);
  let selected;
  const bridge = new BridgeClient({ host: '127.0.0.1', port: 19422, connectionId: options.connectionId }, 35000, async (url, init) => {
    const status = { connections: { 'douyin.com': ['selected-tab', 'other-tab'].map(id => ({ id, protocol: 3, url: 'https://www.douyin.com/video/7345678901234567890', lastActivity: new Date().toISOString() })) } };
    if (init.method === 'POST') selected = JSON.parse(init.body).connectionId;
    return { ok: true, text: async () => JSON.stringify(init.method === 'POST' ? { ok: true, value: 2 } : status) };
  });
  assert.equal(await bridge.call('1+1'), 2);
  assert.equal(selected, 'selected-tab');
});

test('mobile entry disables collection when a registered bridge is stale or the service is offline', async () => {
  const vm = require('node:vm');
  const html = await fs.readFile(path.join(__dirname, '..', 'mobile', 'index.html'), 'utf8');
  const elements = new Map();
  let status = { ok: true, bridgeRegistered: true, bridgeConnected: true, jobs: [], addresses: ['192.168.0.104'], port: 19522 };
  let offline = false;
  let refreshInterval;
  const context = vm.createContext({
    URLSearchParams, AbortSignal,
    location: { search: '', protocol: 'http:' },
    localStorage: { getItem: () => '', setItem() {} },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { value: '', addEventListener() {} });
        return elements.get(id);
      },
      querySelectorAll: () => [],
    },
    setInterval: callback => { refreshInterval = callback; return 1; },
    fetch: async () => {
      if (offline) throw new Error('Failed to fetch');
      return { ok: true, json: async () => status };
    },
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  await refreshInterval();
  assert.equal(elements.get('submit').disabled, false);
  assert.match(elements.get('bridgeText').textContent, /已登记/);
  status = { ...status, bridgeResponsive: true };
  await refreshInterval();
  assert.equal(elements.get('bridgeDot').className, 'dot good');
  assert.match(elements.get('bridgeText').textContent, /已响应/);
  assert.match(elements.get('bridgeScript').href, /^\/bridge\//);
  status = { ...status, bridgeConnected: false, bridgeState: 'stale-browser' };
  await refreshInterval();
  assert.equal(elements.get('submit').disabled, true);
  assert.match(elements.get('bridgeText').textContent, /停止响应/);
  offline = true;
  await refreshInterval();
  assert.equal(elements.get('submit').disabled, true);
  assert.match(elements.get('bridgeText').textContent, /采集服务/);
});

test('extracts an aweme id from common URL and share-text forms', () => {
  assert.equal(extractAwemeId('https://www.douyin.com/video/7345678901234567890'), '7345678901234567890');
  assert.equal(extractAwemeId('复制打开抖音 https://www.douyin.com/share/video/7345678901234567890/?foo=1'), '7345678901234567890');
  assert.equal(extractAwemeId('aweme_id=7345678901234567890'), '7345678901234567890');
  assert.equal(extractAwemeId('not a douyin link'), null);
});

test('derives a compact topic from video caption and hashtags without an LLM', () => {
  assert.equal(deriveTopic('普通人如何开始做 AI？先解决一个真实问题。', { text_extra: [{ hashtag_name: 'AI' }] }), '普通人如何开始做 AI；标签：AI');
});

test('does not fetch when a full video URL already contains the id', async () => {
  let fetchCount = 0;
  const result = await resolveVideoReference(
    'https://www.douyin.com/video/7345678901234567890',
    async () => { fetchCount += 1; throw new Error('should not fetch'); },
  );
  assert.equal(result.awemeId, '7345678901234567890');
  assert.equal(fetchCount, 0);
});

test('ignores stale Bridge registrations instead of waiting for a polling timeout', () => {
  const stale = new Date(Date.now() - 5 * 60 * 1000).toISOString();
  const fresh = new Date().toISOString();
  const status = { connections: { 'douyin.com': [
    { protocol: 3, alive: true, lastActivity: fresh, url: 'https://www.douyin.com/note/2' },
    { protocol: 3, alive: true, lastActivity: stale, url: 'https://www.douyin.com/video/1' },
  ] } };
  assert.equal(bridgeConnectionIndex(status), 0);
  assert.equal(bridgeConnectionIndex({ connections: { 'douyin.com': [status.connections['douyin.com'][1]] } }), -1);
});

test('prefers the most recently polling browser over the most recently registered tab', () => {
  const now = Date.now();
  assert.equal(bridgeConnectionIndex({ connections: { 'douyin.com': [
    { protocol: 3, alive: true, lastActivity: new Date(now).toISOString(), url: 'https://www.douyin.com/user/self' },
    { protocol: 3, alive: true, lastActivity: new Date(now-60000).toISOString(), url: 'https://www.douyin.com/video/1' },
  ] } }), 0);
});

test('paginates only top-level comments and never calls the reply endpoint', async () => {
  const calls = [];
  const bridgeCall = async expression => {
    calls.push(expression);
    const top = expression.match(/getComments\("([^"]+)", (\d+),/);
    if (top) {
      return Number(top[2]) === 0
        ? {
          comments: [
            { cid: 'top-1', text: '第一条', reply_comment_total: 2, user: { nickname: '甲' } },
            { cid: 'top-2', text: '第二条', reply_comment_total: 0 },
          ],
          has_more: 1,
          cursor: 20,
        }
        : { comments: [{ cid: 'top-2', text: '重复第二条' }, { cid: 'top-3', text: '第三条' }], has_more: 0 };
    }
    throw new Error(`unexpected reply request: ${expression}`);
  };

  const result = await collectComments({
    awemeId: '7345678901234567890',
    bridgeCall,
    pageDelayMs: 0,
  });

  assert.equal(result.topCount, 3);
  assert.equal(result.replyCount, 0);
  assert.equal(result.replyCountsAvailable, 2);
  assert.equal(result.records.length, 3);
  assert.equal(result.topPages, 2);
  assert.equal(result.replyPages, 0);
  assert.equal(calls.filter(call => call.includes('replies')).length, 0);
});

test('accepts an optional positive comment limit without changing standalone defaults', () => {
  assert.equal(parseArgs(['7345678901234567890']).maxComments, 0);
  assert.equal(parseArgs(['7345678901234567890', '--max-comments', '1000']).maxComments, 1000);
  for (const value of ['0', '-1', '1.5']) {
    assert.throws(() => parseArgs(['7345678901234567890', '--max-comments', value]), /正整数/);
  }
});

test('stops at 1000 unique comments even when the endpoint still reports more pages', async () => {
  let calls = 0;
  const result = await collectComments({
    awemeId: '7345678901234567890', pageSize: 50, pageDelayMs: 0, maxComments: 1000,
    bridgeCall: async expression => {
      const match = expression.match(/getComments\("[^"]+", (\d+), (\d+)\)/);
      assert.ok(match);
      calls += 1;
      assert.ok(calls <= 20, 'must not fetch a page after the limit');
      const cursor = Number(match[1]), count = Number(match[2]);
      return { comments: Array.from({ length: count }, (_, n) => ({ cid: String(cursor+n), text: '评论', digg_count: 1000-cursor-n })), has_more: 1, cursor: cursor+count };
    },
  });
  assert.equal(calls, 20);
  assert.equal(result.topCount, 1000);
  assert.equal(result.records.length, 1000);
  assert.equal(result.stopReason, 'comment_limit');
  assert.equal(result.nextCursor, 1000);
});

test('does not exceed the limit if a page returns more comments than requested', async () => {
  let calls = 0;
  const result = await collectComments({
    awemeId: '7345678901234567890', pageDelayMs: 0, maxComments: 3,
    bridgeCall: async expression => {
      calls += 1;
      assert.match(expression, /, 0, 3\)/);
      return { comments: Array.from({ length: 5 }, (_, n) => ({ cid: String(n), text: '评论' })), has_more: 1, cursor: 5 };
    },
  });
  assert.equal(calls, 1);
  assert.equal(result.records.length, 3);
  assert.equal(result.stopReason, 'comment_limit');
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'douyin-comment-limit-'));
  try {
    const files = await writeOutputs({ outputDir: temp, input: '7345678901234567890', resolved: { awemeId: '7345678901234567890' }, status: 'completed', state: result, errorMessage: '' });
    const checkpoint = JSON.parse(await fs.readFile(files.checkpoint, 'utf8'));
    assert.equal(checkpoint.status, 'completed');
    assert.equal(checkpoint.max_comments, 3);
    assert.equal(checkpoint.stop_reason, 'comment_limit');
    const handoff = await fs.readFile(files.handoff, 'utf8');
    assert.match(handoff, /达到本轮条数上限/);
    assert.doesNotMatch(handoff, /所有已返回分页均报告无下一页/);
  } finally {
    await fs.rm(temp, { recursive: true, force: true });
  }
});

test('keeps existing saved records without fetching beyond the new limit on resume', async () => {
  const existingRecords = Array.from({ length: 5 }, (_, n) => ({ comment_id: String(n), parent_id: null, text: '已保存', reply_count: 0 }));
  const result = await collectComments({
    awemeId: '7345678901234567890', maxComments: 3, existingRecords, startCursor: 20,
    bridgeCall: async () => { throw new Error('must not fetch more comments'); },
  });
  assert.equal(result.records.length, 5);
  assert.equal(result.topCount, 5);
  assert.equal(result.topPages, 0);
  assert.equal(result.stopReason, 'comment_limit');
});

test('stops on verification errors without retrying the failed page', async () => {
  let calls = 0;
  await assert.rejects(
    collectComments({
      awemeId: '7345678901234567890',
      pageDelayMs: 0,
      bridgeCall: async () => {
        calls += 1;
        throw new Error('文字点选验证码 challenge required');
      },
    }),
    error => {
      assert.equal(error.details.reason, 'verification_required');
      assert.equal(error.details.records.length, 0);
      assert.equal(error.details.diagnostics[0].type, 'top_level_comments');
      return true;
    },
  );
  assert.equal(calls, 1);
});

test('does not call a page complete when the pagination marker is missing', async () => {
  await assert.rejects(
    collectComments({
      awemeId: '7345678901234567890',
      pageDelayMs: 0,
      bridgeCall: async () => ({ comments: [{ cid: 'c1', text: '评论' }] }),
    }),
    error => error.details.reason === 'missing_pagination',
  );
});

test('BridgeClient sends one call and does not perform hidden retries', async () => {
  let callRequests = 0;
  const client = new BridgeClient({ host: '127.0.0.1', port: 19422 }, 1000, async (_url, request) => {
    const body = request.method === 'GET'
      ? { connections: { 'douyin.com': [{ id: 'first-tab', protocol: 3, url: 'https://www.douyin.com/video/1' }] } }
      : (() => { callRequests += 1; return { ok: false, error: 'captcha' }; })();
    return { ok: true, text: async () => JSON.stringify(body) };
  });
  await assert.rejects(client.call('window.__bridge.getComments("1", 0, 20)'), /captcha/);
  assert.equal(callRequests, 1);
});

test('BridgeClient pins one connection across pages and stops when that connection disappears', async () => {
  const now = Date.now();
  const first = { id: 'first-tab', protocol: 3, url: 'https://www.douyin.com/video/1', lastActivity: new Date(now).toISOString() };
  const other = { ...first, id: 'other-tab', lastActivity: new Date(now - 1000).toISOString() };
  let connections = [first, other];
  const calls = [];
  const client = new BridgeClient({ host: '127.0.0.1', port: 19422 }, 5000, async (_url, request) => {
    const body = request.method === 'GET'
      ? { connections: { 'douyin.com': connections } }
      : (() => { calls.push(JSON.parse(request.body)); return { ok: true, value: 2 }; })();
    return { ok: true, text: async () => JSON.stringify(body) };
  });
  await client.call('1+1');
  connections = [{ ...other, lastActivity: new Date(now + 1000).toISOString() }, first];
  await client.call('1+1');
  assert.deepEqual(calls.map(call => call.connectionId), ['first-tab', 'first-tab']);
  assert.deepEqual(calls.map(call => call.connIndex), [0, 1]);
  connections = [other];
  await assert.rejects(client.call('1+1'), /已经断开/);
  assert.equal(calls.length, 2);
});

test('writes compact handoff files for a partial result', async () => {
  const outputDir = await fs.mkdtemp(path.join(os.tmpdir(), 'douyin-comment-'));
  const state = {
    topPages: 1,
    replyPages: 0,
    topCount: 1,
    replyCount: 0,
    replyCountsAvailable: 0,
    nextCursor: 20,
    stopReason: 'verification_required',
    diagnostics: [],
    records: [{
      comment_id: 'c1', parent_id: null, level: 1, text: '含,逗号', nickname: '甲',
      user_id: 'u1', create_time: 1, likes: 2, reply_count: 0, raw: { cid: 'c1' },
    }],
  };
  const files = await writeOutputs({
    outputDir,
    input: 'https://www.douyin.com/video/7345678901234567890',
    resolved: { awemeId: '7345678901234567890', resolvedUrl: null },
    status: 'partial',
    state,
    errorMessage: '测试中断',
  });
  const [jsonl, csv, handoff] = await Promise.all([
    fs.readFile(files.jsonl, 'utf8'),
    fs.readFile(files.csv, 'utf8'),
    fs.readFile(files.handoff, 'utf8'),
  ]);
  assert.equal(JSON.parse(jsonl).raw.cid, 'c1');
  assert.match(csv, /"含,逗号"/);
  assert.match(handoff, /partial/);
  assert.match(handoff, /测试中断/);
  assert.match(csv, /high_like/);
  assert.match(await fs.readFile(files.checkpoint, 'utf8'), /verification_required/);
});

test('stores a collection in the local SQLite library for later queries', async () => {
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'douyin-db-'));
  const databasePath = path.join(tempDir, 'comments.sqlite');
  const state = {
    topCount: 1,
    replyCount: 1,
    records: [
      { comment_id: 'c1', parent_id: null, level: 1, text: '评论', nickname: '甲', user_id: 'u1', create_time: 1, likes: 2, reply_count: 1, raw: { cid: 'c1' } },
      { comment_id: 'r1', parent_id: 'c1', level: 2, text: '回复', nickname: '乙', user_id: 'u2', create_time: 2, likes: 1, reply_count: 0, raw: { cid: 'r1' } },
    ],
  };
  saveCollection({
    databasePath,
    sourceUrl: 'https://www.douyin.com/video/7345678901234567890',
    resolved: { awemeId: '7345678901234567890', resolvedUrl: null },
    status: 'completed',
    state,
    outputDir: 'outputs/test',
  });
  const { db } = openDatabase(databasePath);
  try {
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM comments').get().count, 2);
    assert.equal(db.prepare('SELECT text FROM comments WHERE comment_id = ?').get('r1').text, '回复');
    assert.equal(db.prepare('SELECT status FROM collection_runs').get().status, 'completed');
  } finally {
    db.close();
  }
});

test('marks unknown likes without treating them as zero and pages high-like comments', async () => {
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'douyin-like-db-'));
  const databasePath = path.join(tempDir, 'comments.sqlite');
  saveCollection({
    databasePath,
    sourceUrl: 'https://www.douyin.com/video/7345678901234567890',
    resolved: { awemeId: '7345678901234567890', resolvedUrl: null },
    status: 'completed',
    state: {
      scope: 'top_level', topCount: 3, replyCount: 0, replyCountsAvailable: 0, nextCursor: null,
      records: [
        { comment_id: 'c99', parent_id: null, level: 1, text: '99', likes: 99, reply_count: 0, raw: {} },
        { comment_id: 'c100', parent_id: null, level: 1, text: '100', likes: 100, reply_count: 0, raw: {} },
        { comment_id: 'cunknown', parent_id: null, level: 1, text: 'unknown', likes: -1, reply_count: 0, raw: {} },
      ],
    },
  });
  const { listComments, updateComment } = require('./comment_library');
  const all = listComments({ databasePath, awemeId: '7345678901234567890', limit: 100, highLikeThreshold: 100 });
  assert.deepEqual(all.rows.map(row => row.comment_id), ['c100', 'c99', 'cunknown']);
  assert.equal(all.rows[0].high_like, true);
  assert.equal(all.rows[2].high_like, false);
  const high = listComments({ databasePath, awemeId: '7345678901234567890', limit: 100, highLikeOnly: true, highLikeThreshold: 100 });
  assert.deepEqual(high.rows.map(row => row.comment_id), ['c100']);
  updateComment({ databasePath, awemeId: '7345678901234567890', commentKey: 'c100', reviewed: true, note: '已整理', tags: '选题' });
  const reviewed = listComments({ databasePath, awemeId: '7345678901234567890', limit: 100, reviewedOnly: true });
  assert.deepEqual(reviewed.rows.map(row => row.comment_id), ['c100']);
  assert.equal(reviewed.rows[0].note, '已整理');
  assert.equal(reviewed.rows[0].tags, '选题');
});

test('exports filtered top-level comments as Markdown', async () => {
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'douyin-export-db-'));
  const databasePath = path.join(tempDir, 'comments.sqlite');
  saveCollection({
    databasePath,
    sourceUrl: 'https://www.douyin.com/video/7345678901234567890',
    resolved: { awemeId: '7345678901234567890', resolvedUrl: null },
    status: 'completed',
    videoInfo: { caption: '测试视频', topic: '测试主题', author_nickname: '作者' },
    state: {
      topCount: 2, replyCount: 0,
      records: [
        { comment_id: 'c1', parent_id: null, level: 1, text: '值得整理', nickname: '甲', likes: 120, tags: '', raw: {} },
        { comment_id: 'c2', parent_id: null, level: 1, text: '普通评论', nickname: '乙', likes: 2, tags: '', raw: {} },
      ],
    },
  });
  const { exportCommentsMarkdown } = require('./comment_library');
  const result = exportCommentsMarkdown({ databasePath, keyword: '整理', highLikeThreshold: 100 });
  assert.equal(result.count, 1);
  assert.match(result.markdown, /# 抖音一级评论导出/);
  assert.match(result.markdown, /值得整理/);
  assert.doesNotMatch(result.markdown, /普通评论/);
});
