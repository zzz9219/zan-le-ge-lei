#!/usr/bin/env node

/**
 * Read-only Douyin comment collector.
 *
 * The browser-side requests are made by the upstream Bridge + Tampermonkey
 * script. This file only paginates, deduplicates, and writes local files; it
 * never calls an LLM and never publishes or modifies comments.
 */

const fs = require('node:fs/promises');
const path = require('node:path');
const { DEFAULT_DATABASE_PATH, getSettings, saveCollection } = require('./comment_library');

const SITE = 'douyin.com';
const DEFAULT_PAGE_SIZE = 20;
const DEFAULT_PAGE_DELAY_MS = 3000;
const MAX_BRIDGE_IDLE_MS = 90 * 1000;
const DEFAULT_BRIDGE_DIR = path.resolve(__dirname, '..', 'douyin-upstream');
const DEFAULT_OUTPUT_ROOT = path.resolve(__dirname, '..', 'outputs');

function isMainDouyinConnection(connection) {
  const url = String(connection?.url || '');
  if (Number(connection?.protocol || 0) < 2 || connection?.alive === false) return false;
  if (!/^https?:\/\/(?:www\.)?douyin\.com(?::\d+)?(?:\/|$)/i.test(url)) return false;
  const lastActivity = Date.parse(connection?.lastActivity || '');
  return !Number.isFinite(lastActivity) || Date.now() - lastActivity <= MAX_BRIDGE_IDLE_MS;
}

function bridgeConnectionIndex(status) {
  const connections = status?.connections?.[SITE];
  if (!Array.isArray(connections)) return -1;
  let selected = -1;
  let latest = -1;
  for (let index = 0; index < connections.length; index += 1) {
    if (!isMainDouyinConnection(connections[index])) continue;
    const activity = Date.parse(connections[index].lastActivity || '') || 0;
    if (activity >= latest) { selected = index; latest = activity; }
  }
  return selected;
}

class CollectionError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = 'CollectionError';
    this.details = details;
  }
}

function parseArgs(argv) {
  const options = {
    bridgeDir: DEFAULT_BRIDGE_DIR,
    outputDir: null,
    pageSize: DEFAULT_PAGE_SIZE,
    pageDelayMs: DEFAULT_PAGE_DELAY_MS,
    resumeDir: null,
    connectionId: null,
    timeoutMs: 35000,
    maxComments: 0,
    help: false,
  };
  const positional = [];

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') {
      options.help = true;
    } else if (arg === '--bridge-dir') {
      const value = argv[++i];
      if (!value || value.startsWith('-')) throw new Error('--bridge-dir 需要目录');
      options.bridgeDir = path.resolve(value);
    } else if (arg === '--output-dir') {
      const value = argv[++i];
      if (!value || value.startsWith('-')) throw new Error('--output-dir 需要目录');
      options.outputDir = path.resolve(value);
    } else if (arg === '--resume-dir') {
      const value = argv[++i];
      if (!value || value.startsWith('-')) throw new Error('--resume-dir 需要目录');
      options.resumeDir = path.resolve(value);
    } else if (arg === '--connection-id') {
      const value = argv[++i];
      if (!value || value.startsWith('-') || value.length > 64) throw new Error('--connection-id 需要有效的浏览器连接 ID');
      options.connectionId = value;
    } else if (arg === '--page-size') {
      options.pageSize = positiveInteger(argv[++i], '--page-size');
    } else if (arg === '--max-comments') {
      options.maxComments = positiveInteger(argv[++i], '--max-comments');
    } else if (arg === '--timeout-ms') {
      options.timeoutMs = positiveInteger(argv[++i], '--timeout-ms');
    } else if (arg === '--page-delay-ms') {
      options.pageDelayMs = nonNegativeInteger(argv[++i], '--page-delay-ms');
    } else if (arg.startsWith('-')) {
      throw new Error(`未知选项: ${arg}`);
    } else {
      positional.push(arg);
    }
  }

  if (!options.help && positional.length !== 1) {
    throw new Error('用法: node scripts/collect_comments.js <抖音链接或分享文本> [--output-dir 目录]');
  }
  options.reference = positional[0] || '';
  return options;
}

function positiveInteger(value, flag) {
  const number = Number(value);
  if (!Number.isInteger(number) || number <= 0) {
    throw new Error(`${flag} 必须是正整数`);
  }
  return number;
}

function nonNegativeInteger(value, flag) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 0) {
    throw new Error(`${flag} 必须是非负整数`);
  }
  return number;
}

function isDouyinHost(hostname) {
  const host = String(hostname || '').toLowerCase().replace(/\.$/, '');
  return host === 'douyin.com' || host.endsWith('.douyin.com');
}

function trimUrlPunctuation(value) {
  return String(value).replace(/[，。！？；：）)】》>]+$/g, '');
}

function extractAwemeId(value) {
  const text = String(value || '').trim();
  if (/^\d{8,25}$/.test(text)) return text;

  const patterns = [
    /\/video\/(\d{8,25})/i,
    /[?&](?:modal_id|aweme_id|item_id)=(\d{8,25})/i,
    /["']?(?:modal_id|aweme_id|item_id)["']?\s*[:=]\s*["']?(\d{8,25})/i,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match) return match[1];
  }

  // A copied share message sometimes contains the numeric ID without the URL.
  const standaloneId = text.match(/(?:^|\D)(\d{15,25})(?:\D|$)/);
  return standaloneId ? standaloneId[1] : null;
}

function findDouyinUrl(value) {
  const match = String(value || '').match(/https?:\/\/[^\s]+/i);
  return match ? trimUrlPunctuation(match[0]) : null;
}

async function resolveVideoReference(reference, fetchImpl = globalThis.fetch) {
  const original = String(reference || '').trim();
  const directId = extractAwemeId(original);
  if (directId) {
    return { awemeId: directId, sourceUrl: findDouyinUrl(original) || original, resolvedUrl: null };
  }

  const inputUrl = findDouyinUrl(original);
  if (!inputUrl) {
    throw new Error('没有识别到抖音视频链接或 aweme_id');
  }

  let parsed;
  try {
    parsed = new URL(inputUrl);
  } catch {
    throw new Error('抖音链接格式无效');
  }
  if (!isDouyinHost(parsed.hostname)) {
    throw new Error('只接受 douyin.com 或其子域名链接');
  }

  let response;
  try {
    response = await fetchImpl(inputUrl, {
      redirect: 'follow',
      headers: { 'user-agent': 'Mozilla/5.0 (compatible; DouyinCommentCollector/1.0)' },
    });
  } catch (error) {
    throw new Error(`短链接解析失败: ${error.message}`);
  }
  const resolvedUrl = response.url || inputUrl;
  if (!response.ok && response.status !== 301 && response.status !== 302) {
    throw new Error(`短链接解析失败: HTTP ${response.status}`);
  }
  try {
    if (!isDouyinHost(new URL(resolvedUrl).hostname)) {
      throw new Error('短链接跳转到了非抖音域名');
    }
  } catch (error) {
    if (error.message === '短链接跳转到了非抖音域名') throw error;
    throw new Error('短链接跳转地址无效');
  }
  const awemeId = extractAwemeId(resolvedUrl);
  if (!awemeId) {
    throw new Error('短链接没有跳转到可识别的视频地址');
  }
  return { awemeId, sourceUrl: inputUrl, resolvedUrl };
}

function readBridgeConfig(configPath) {
  let config;
  try {
    config = JSON.parse(require('node:fs').readFileSync(configPath, 'utf8'));
  } catch (error) {
    throw new Error(`无法读取 Bridge 配置 ${configPath}: ${error.message}`);
  }
  const bridge = config.bridge || {};
  return {
    host: bridge.host || '127.0.0.1',
    port: Number(bridge.port || 19422),
    token: bridge.token || '',
  };
}

class BridgeClient {
  constructor(config, timeoutMs = 35000, fetchImpl = globalThis.fetch) {
    this.config = config;
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
    this.connectionId = config.connectionId || null;
  }

  async status() {
    return this.request('GET', '/api/status');
  }

  async call(expression) {
    const status = await this.status();
    const connections = status?.connections?.[SITE] || [];
    const connIndex = this.connectionId
      ? connections.findIndex(connection => connection.id === this.connectionId)
      : bridgeConnectionIndex(status);
    if (this.connectionId && !isMainDouyinConnection(connections[connIndex])) {
      throw new Error('本次采集使用的抖音页面已经断开；已停止采集，请恢复该页面后主动续采');
    }
    if (connIndex < 0) {
      throw new Error('没有检测到可调用的抖音视频页面；请在内置浏览器打开 www.douyin.com 的视频页后重试');
    }
    this.connectionId = connections[connIndex].id;
    if (!this.connectionId) throw new Error('浏览器桥缺少连接 ID，请重新打开已登录的抖音页面');
    const body = await this.request('POST', '/api/call', {
      site: SITE,
      expression,
      awaitPromise: true,
      connIndex,
      connectionId: this.connectionId,
      timeout: this.timeoutMs - 1000,
    });
    if (!body.ok) throw new Error(body.error || 'Bridge 调用失败');
    return body.value;
  }

  async request(method, requestPath, payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const headers = {};
    if (payload !== undefined) headers['content-type'] = 'application/json';
    if (this.config.token) headers.authorization = `Bearer ${this.config.token}`;
    const url = `http://${this.config.host}:${this.config.port}${requestPath}`;
    try {
      const response = await this.fetchImpl(url, {
        method,
        headers,
        body: payload === undefined ? undefined : JSON.stringify(payload),
        signal: controller.signal,
      });
      const text = await response.text();
      let body;
      try {
        body = JSON.parse(text);
      } catch {
        throw new Error(`Bridge 返回了非 JSON 数据: ${text.slice(0, 160)}`);
      }
      if (!response.ok) throw new Error(body.error || `Bridge HTTP ${response.status}`);
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error(`Bridge 请求超时（${this.timeoutMs}ms）`);
      if (error.message.includes('fetch failed')) {
        throw new Error(`Bridge Server 未启动或不可访问（${this.config.host}:${this.config.port}）`);
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }
}

function stringValue(value, fallback = '') {
  return value === undefined || value === null ? fallback : String(value);
}

function numberValue(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function numberOrUnknown(value) {
  if (value === undefined || value === null || value === '') return -1;
  const number = Number(value);
  return Number.isFinite(number) ? number : -1;
}

function commentId(raw) {
  const value = raw && (raw.cid ?? raw.comment_id ?? raw.id);
  return value === undefined || value === null || value === '' ? null : String(value);
}

function userInfo(raw) {
  return raw && (raw.user || raw.user_info || raw.author || {}) || {};
}

function replyCount(raw) {
  return numberValue(raw && (raw.reply_comment_total ?? raw.reply_count ?? raw.replies), 0);
}

function deriveTopic(caption, raw = {}) {
  const text = String(caption || '').replace(/\s+/g, ' ').trim();
  const hashtags = [];
  for (const item of raw.text_extra || raw.textExtra || []) {
    const name = item.hashtag_name || item.hashtagName;
    if (name && !hashtags.includes(String(name))) hashtags.push(String(name));
  }
  for (const match of text.matchAll(/#([^\s#]+)/g)) {
    if (!hashtags.includes(match[1])) hashtags.push(match[1]);
  }
  const firstSentence = (text.split(/[。！？!?\n]/)[0] || text).trim().slice(0, 120);
  return [firstSentence, hashtags.length ? `标签：${hashtags.slice(0, 8).join('、')}` : '']
    .filter(Boolean).join('；');
}

function normalizeVideoInfo(raw, awemeId) {
  const detail = raw?.aweme_detail || raw?.aweme || raw?.data?.aweme_detail || raw?.data?.aweme || raw || {};
  const caption = stringValue(detail.desc ?? detail.caption ?? detail.title ?? detail.share_info?.share_title);
  const author = detail.author || detail.author_info || {};
  return {
    aweme_id: String(detail.aweme_id || awemeId),
    caption,
    topic: deriveTopic(caption, detail),
    author_nickname: stringValue(author.nickname ?? author.name),
    author_id: stringValue(author.uid ?? author.sec_uid ?? author.user_id),
    create_time: detail.create_time ?? null,
    raw,
  };
}

function videoInfoFromReference(reference, awemeId) {
  const text = String(reference || '').replace(/https?:\/\/\S+/gi, ' ')
    .replace(/复制打开抖音[，,:：]?/g, ' ')
    .replace(/看看[【\[][^】\]]+[】\]]/g, ' ')
    .replace(/^\d+(?:\.\d+)?\s+/g, '')
    .replace(/\s+\d{1,2}\/\d{1,2}\s+.*$/g, ' ')
    .replace(/\s+/g, ' ').trim();
  return normalizeVideoInfo({ aweme_detail: { aweme_id: awemeId, desc: text } }, awemeId);
}

function normalizeComment(raw, parentId = null) {
  const user = userInfo(raw);
  return {
    comment_id: commentId(raw),
    parent_id: parentId,
    level: parentId ? 2 : 1,
    text: stringValue(raw && (raw.text ?? raw.content)),
    nickname: stringValue(user.nickname ?? user.name ?? (raw && raw.nickname)),
    user_id: stringValue(user.uid ?? user.user_id ?? user.sec_uid ?? (raw && (raw.uid ?? raw.user_id))),
    create_time: raw && (raw.create_time ?? raw.createTime ?? raw.time) || null,
    likes: numberOrUnknown(raw && (raw.digg_count ?? raw.likes ?? raw.like_count)),
    reply_count: replyCount(raw),
    raw,
  };
}

function dedupeKey(record) {
  if (record.comment_id) return `id:${record.comment_id}`;
  return `fallback:${record.parent_id || 'top'}:${record.create_time || ''}:${record.text}`;
}

function hasMore(data) {
  const marker = data && (data.has_more ?? data.hasMore ?? data.has_next ?? data.hasNext);
  return marker === true || marker === 1 || marker === '1' || marker === 'true';
}

function hasPaginationMarker(data) {
  return data && (
    Object.prototype.hasOwnProperty.call(data, 'has_more')
    || Object.prototype.hasOwnProperty.call(data, 'hasMore')
    || Object.prototype.hasOwnProperty.call(data, 'has_next')
    || Object.prototype.hasOwnProperty.call(data, 'hasNext')
  );
}

function nextCursor(data, current, pageSize, label) {
  const candidate = data && (data.cursor ?? data.next_cursor ?? data.nextCursor);
  const next = candidate === undefined || candidate === null || candidate === ''
    ? Number(current) + pageSize
    : Number(candidate);
  if (!Number.isFinite(next) || next === Number(current)) {
    throw new CollectionError(`${label}分页游标没有前进`, { reason: 'cursor_stalled' });
  }
  return next;
}

function assertPage(data, label) {
  if (!data || typeof data !== 'object' || !Array.isArray(data.comments)) {
    throw new CollectionError(`${label}接口返回格式异常`, { reason: 'invalid_response' });
  }
}

function classifyStopReason(message) {
  const text = String(message || '').toLowerCase();
  if (/验证码|captcha|challenge|verify/.test(text)) return 'verification_required';
  if (/429|rate.?limit|限流|频繁|too many/.test(text)) return 'rate_limited';
  if (/401|403|登录|login|unauthorized|forbidden|未授权/.test(text)) return 'login_or_permission';
  if (/timeout|超时|timed out/.test(text)) return 'timeout';
  return 'error';
}

function sleep(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function collectComments({
  awemeId,
  bridgeCall,
  videoInfoCall = null,
  pageSize = DEFAULT_PAGE_SIZE,
  pageDelayMs = DEFAULT_PAGE_DELAY_MS,
  onPage = null,
  startCursor = 0,
  existingRecords = [],
  maxComments = 0,
}) {
  const records = [...existingRecords];
  const seen = new Set();
  for (const record of records) seen.add(dedupeKey(record));
  const state = {
    scope: 'top_level',
    topPages: 0,
    replyPages: 0,
    topCount: records.filter(record => !record.parent_id).length,
    maxComments,
    replyCount: records.filter(record => record.parent_id).length,
    replyCountsAvailable: records.reduce((sum, record) => sum + Number(record.reply_count || 0), 0),
    nextCursor: startCursor,
    stopReason: null,
    diagnostics: [],
    videoInfo: null,
    records,
  };

  const add = (raw, parentId) => {
    const normalized = normalizeComment(raw, parentId);
    const key = dedupeKey(normalized);
    if (seen.has(key)) return null;
    seen.add(key);
    records.push(normalized);
    if (parentId) state.replyCount += 1;
    else state.topCount += 1;
    return normalized;
  };

  try {
    if (videoInfoCall) {
      const videoStartedAt = new Date().toISOString();
      try {
        const videoRaw = await videoInfoCall(`window.__bridge.getVideoInfo(${JSON.stringify(awemeId)})`);
        state.diagnostics.push({
          event: 'request',
          type: 'video_info',
          started_at: videoStartedAt,
          ended_at: new Date().toISOString(),
          ok: true,
        });
        if (!videoRaw || typeof videoRaw !== 'object') {
          throw new CollectionError('视频文案接口返回格式异常', { reason: 'invalid_video_info' });
        }
        state.videoInfo = normalizeVideoInfo(videoRaw, awemeId);
      } catch (error) {
        state.diagnostics.push({
          event: 'request_error',
          type: 'video_info',
          started_at: videoStartedAt,
          ended_at: new Date().toISOString(),
          ok: false,
          stop_reason: error.details?.reason || classifyStopReason(error.message),
        });
        throw error;
      }
    }
    if (maxComments && state.topCount >= maxComments) {
      state.stopReason = 'comment_limit';
      return state;
    }
    let cursor = Number(startCursor) || 0;
    const visitedCursors = new Set();
    while (true) {
      if (visitedCursors.has(cursor)) {
        throw new CollectionError('一级评论分页游标循环', { reason: 'cursor_cycle', next_cursor: cursor });
      }
      visitedCursors.add(cursor);
      state.nextCursor = cursor;
      const startedAt = new Date().toISOString();
      const count = maxComments ? Math.min(pageSize, maxComments-state.topCount) : pageSize;
      const expression = `window.__bridge.getComments(${JSON.stringify(awemeId)}, ${cursor}, ${count})`;
      let data;
      try {
        data = await bridgeCall(expression);
      } catch (error) {
        state.diagnostics.push({
          event: 'request_error',
          type: 'top_level_comments',
          cursor,
          started_at: startedAt,
          ended_at: new Date().toISOString(),
          ok: false,
          stop_reason: error.details?.reason || classifyStopReason(error.message),
        });
        throw error;
      }
      assertPage(data, '一级评论');
      state.topPages += 1;
      if (!hasPaginationMarker(data)) {
        throw new CollectionError('一级评论接口缺少分页标记', { reason: 'missing_pagination' });
      }
      if (data.comments.length === 0) {
        throw new CollectionError('一级评论返回空页但仍标记有下一页', { reason: 'empty_page' });
      }

      for (const raw of data.comments) {
        const record = add(raw, null);
        if (record) state.replyCountsAvailable += record.reply_count;
        if (maxComments && state.topCount >= maxComments) break;
      }

      const more = hasMore(data);
      state.nextCursor = more ? nextCursor(data, cursor, count, '一级评论') : null;
      if (more && maxComments && state.topCount >= maxComments) state.stopReason = 'comment_limit';
      state.diagnostics.push({
        event: 'page',
        type: 'top_level_comments',
        cursor,
        started_at: startedAt,
        ended_at: new Date().toISOString(),
        returned: data.comments.length,
        next_cursor: state.nextCursor,
      });
      if (onPage) await onPage(state);
      if (!more || state.stopReason === 'comment_limit') break;
      cursor = state.nextCursor;
      if (pageDelayMs > 0) await sleep(pageDelayMs);
    }
  } catch (error) {
    state.stopReason = error.details?.reason || classifyStopReason(error.message);
    if (error instanceof CollectionError) {
      error.details = { ...state, ...error.details };
      throw error;
    }
    throw new CollectionError(error.message, { ...state, reason: state.stopReason });
  }

  return state;
}

function csvValue(value) {
  const text = value === null || value === undefined ? '' : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function timestampForPath(date = new Date()) {
  return date.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z');
}

function safeFilePart(value) {
  return String(value).replace(/[^a-zA-Z0-9_-]/g, '_');
}

async function writeCheckpoint({ outputDir, input, resolved, state, status = 'partial', errorMessage = '' }) {
  await fs.mkdir(outputDir, { recursive: true });
  await fs.writeFile(path.join(outputDir, 'checkpoint.json'), `${JSON.stringify({
    aweme_id: resolved.awemeId,
    source_url: input,
    scope: 'top_level',
    status,
    next_cursor: state.nextCursor ?? null,
    top_pages: state.topPages || 0,
    top_comments: state.topCount || 0,
    max_comments: state.maxComments || 0,
    reply_counts_available: state.replyCountsAvailable || 0,
    stop_reason: state.stopReason || (status === 'completed' ? 'completed' : classifyStopReason(errorMessage)),
    error: errorMessage || null,
    updated_at: new Date().toISOString(),
  }, null, 2)}\n`, 'utf8');
}

async function writeOutputs({
  outputDir,
  input,
  resolved,
  status,
  state,
  errorMessage = '',
  databasePath = '',
  databaseError = '',
  highLikeThreshold = 100,
}) {
  await fs.mkdir(outputDir, { recursive: true });
  const jsonl = state.records.map(record => JSON.stringify({
    aweme_id: resolved.awemeId,
    scope: 'top_level',
    high_like_threshold: highLikeThreshold,
    high_like: record.likes >= highLikeThreshold,
    ...record,
  })).join('\n');
  const csvHeader = ['comment_id', 'parent_id', 'level', 'text', 'nickname', 'user_id', 'create_time', 'likes', 'high_like', 'high_like_threshold', 'reply_count'];
  const csvRows = state.records.map(record => [
    record.comment_id,
    record.parent_id,
    record.level,
    record.text,
    record.nickname,
    record.user_id,
    record.create_time,
    record.likes < 0 ? '' : record.likes,
    record.likes >= highLikeThreshold,
    highLikeThreshold,
    record.reply_count,
  ].map(csvValue).join(','));

  const handoff = [
    '# 抖音评论采集交接',
    '',
    `- 状态: ${status === 'completed' ? (state.stopReason === 'comment_limit' ? 'completed（达到本轮条数上限）' : 'completed（接口可访问数据采集结束）') : 'partial（未完成）'}`,
    `- 来源: ${input}`,
    `- 解析后视频 ID: ${resolved.awemeId || '未知'}`,
    `- 最终地址: ${resolved.resolvedUrl || '未发生跳转'}`,
    `- 采集时间: ${new Date().toISOString()}`,
    '- 范围: top_level（只采集一级评论；不请求、不展开回复）',
    `- 一级评论: ${state.topCount}`,
    '- 回复: 0（本次未采集回复）',
    `- 一级评论中接口返回的回复数量合计: ${state.replyCountsAvailable || 0}`,
    `- 总去重记录: ${state.records.length}`,
    `- 一级分页: ${state.topPages}`,
    '- 回复分页: 0（未调用回复接口）',
    `- 高赞门槛: ${highLikeThreshold}`,
    `- 视频文案: ${state.videoInfo?.caption || '未读取'}`,
    `- 推导主题: ${state.videoInfo?.topic || '未读取'}`,
    `- 结束原因: ${state.stopReason === 'comment_limit' ? '达到配置的 '+state.maxComments+' 条上限；只选取接口返回的前部评论' : (status === 'completed' ? '所有已返回分页均报告无下一页' : (state.stopReason || classifyStopReason(errorMessage)))}`,
    `- 错误信息: ${errorMessage || '无'}`,
    '',
    '## 文件',
    '',
    '- `comments.jsonl`：每行一条评论，保留 `raw` 原始接口对象。',
    '- `comments.csv`：便于查看的扁平字段。',
    '- `video.json`：视频文案、作者和推导主题；接口不可用时保留分享文案推导结果。',
    '- `handoff.md`：本次采集状态和边界。',
    '- `checkpoint.json`：按页保存的续采位置和停止原因。',
    '- `diagnostics.json`：分页请求的类型、游标、时间和结果数量，不含评论正文。',
    `- SQLite 库：${databasePath || '未写入'}${databaseError ? `（${databaseError}）` : ''}`,
    '',
    '## 边界',
    '',
    '结果表示采集期间当前登录态通过接口可访问的数据；已删除、隐藏或平台未返回的内容不在结果中。',
  ];
  if (status !== 'completed') {
    handoff.push('', '## 未完成项', '', `- ${errorMessage || '请检查 Bridge Server、浏览器登录态和 Tampermonkey 连接后重试。'}`);
  }

  const files = {
    jsonl: path.join(outputDir, 'comments.jsonl'),
    csv: path.join(outputDir, 'comments.csv'),
    handoff: path.join(outputDir, 'handoff.md'),
    video: path.join(outputDir, 'video.json'),
    checkpoint: path.join(outputDir, 'checkpoint.json'),
    diagnostics: path.join(outputDir, 'diagnostics.json'),
  };
  await Promise.all([
    fs.writeFile(files.jsonl, jsonl ? `${jsonl}\n` : '', 'utf8'),
    fs.writeFile(files.csv, `${csvHeader.join(',')}\r\n${csvRows.join('\r\n')}${csvRows.length ? '\r\n' : ''}`, 'utf8'),
    fs.writeFile(files.handoff, `${handoff.join('\n')}\n`, 'utf8'),
    fs.writeFile(files.video, `${JSON.stringify(state.videoInfo || { aweme_id: resolved.awemeId, caption: '', topic: '' }, null, 2)}\n`, 'utf8'),
    writeCheckpoint({ outputDir, input, resolved, state, status, errorMessage }),
    fs.writeFile(files.diagnostics, `${JSON.stringify(state.diagnostics || [], null, 2)}\n`, 'utf8'),
  ]);
  return files;
}

function printHelp() {
  process.stdout.write([
    '抖音评论采集（只读，不调用 LLM）',
    '',
    '用法:',
    '  node scripts/collect_comments.js <抖音链接或分享文本>',
    '',
    '选项:',
    '  --bridge-dir <目录>   Bridge Server 目录（默认 ../douyin-upstream）',
    '  --output-dir <目录>   输出目录（默认 outputs/<aweme_id>-<时间>）',
    '  --resume-dir <目录>   从该输出目录的 checkpoint.json 继续采集',
    '  --page-size <整数>    每页请求数量（默认 20）',
    '  --page-delay-ms <整数> 页间等待毫秒数（默认 3000）',
    '  --timeout-ms <整数>   单次 Bridge 请求超时（默认 35000）',
    '  --max-comments <整数> 最多采集的一级评论数（不传则不限）',
    '  --connection-id <ID>  使用已检查过的浏览器连接（供知识库入口调用）',
  ].join('\n') + '\n');
}

async function readResumeState(resumeDir) {
  const checkpointPath = path.join(resumeDir, 'checkpoint.json');
  let checkpoint;
  try {
    checkpoint = JSON.parse(await fs.readFile(checkpointPath, 'utf8'));
  } catch (error) {
    throw new Error(`无法读取续采 checkpoint.json: ${error.message}`);
  }
  if (checkpoint.next_cursor === null || checkpoint.status === 'completed') {
    throw new Error('这个 checkpoint 已经完成，没有可继续的分页；如需重新采集请新建任务');
  }
  let records = [];
  try {
    const text = await fs.readFile(path.join(resumeDir, 'comments.jsonl'), 'utf8');
    records = text.split(/\r?\n/).filter(Boolean).map(line => {
      const value = JSON.parse(line);
      return {
        comment_id: value.comment_id ?? null,
        parent_id: value.parent_id ?? null,
        level: value.level || 1,
        text: value.text || '',
        nickname: value.nickname || '',
        user_id: value.user_id || '',
        create_time: value.create_time ?? null,
        likes: value.likes === undefined || value.likes === null ? -1 : value.likes,
        reply_count: value.reply_count || 0,
        raw: value.raw || {},
      };
    });
  } catch (error) {
    throw new Error(`无法读取续采评论文件: ${error.message}`);
  }
  return {
    startCursor: Number(checkpoint.next_cursor),
    records,
    awemeId: checkpoint.aweme_id || null,
  };
}

async function main(argv = process.argv.slice(2)) {
  let options;
  try {
    options = parseArgs(argv);
  } catch (error) {
    process.stderr.write(`错误: ${error.message}\n`);
    process.exitCode = 2;
    return;
  }
  if (options.help) {
    printHelp();
    return;
  }

  let resolved = { awemeId: null, sourceUrl: options.reference, resolvedUrl: null };
  const state = {
    scope: 'top_level', topPages: 0, replyPages: 0, topCount: 0, replyCount: 0,
    replyCountsAvailable: 0, nextCursor: 0, stopReason: null, diagnostics: [], records: [],
    videoInfo: null,
  };
  let errorMessage = '';
  let status = 'partial';
  let outputDir = options.outputDir;
  let highLikeThreshold = 100;
  let resume = { startCursor: 0, records: [] };
  try {
    resolved = await resolveVideoReference(options.reference);
    if (options.resumeDir) {
      outputDir = options.resumeDir;
      resume = await readResumeState(options.resumeDir);
      if (resume.awemeId && resume.awemeId !== resolved.awemeId) {
        throw new Error(`续采 checkpoint 属于视频 ${resume.awemeId}，与当前链接 ${resolved.awemeId} 不一致`);
      }
    }
    state.videoInfo = videoInfoFromReference(options.reference, resolved.awemeId);
    outputDir = outputDir || path.join(
      DEFAULT_OUTPUT_ROOT,
      `${safeFilePart(resolved.awemeId || 'unknown')}-${timestampForPath()}`,
    );
    await fs.mkdir(outputDir, { recursive: true });
    const config = readBridgeConfig(path.join(options.bridgeDir, 'config.json'));
    const bridge = new BridgeClient({ ...config, connectionId: options.connectionId }, options.timeoutMs);
    const bridgeStatus = await bridge.status();
    if (bridgeConnectionIndex(bridgeStatus) < 0) {
      throw new Error('没有检测到可调用的抖音视频页面；请在已登录抖音的浏览器中打开 www.douyin.com 的视频页');
    }
    const result = await collectComments({
      awemeId: resolved.awemeId,
      pageSize: options.pageSize,
      pageDelayMs: options.pageDelayMs,
      startCursor: resume.startCursor,
      existingRecords: resume.records,
      maxComments: options.maxComments,
      bridgeCall: expression => bridge.call(expression),
      videoInfoCall: expression => bridge.call(expression),
      onPage: snapshot => writeCheckpoint({
        outputDir,
        input: options.reference,
        resolved,
        state: snapshot,
        status: snapshot.nextCursor === null || snapshot.stopReason === 'comment_limit' ? 'completed' : 'partial',
      }),
    });
    Object.assign(state, result);
    status = 'completed';
  } catch (error) {
    errorMessage = error.message;
    state.stopReason = error.details?.reason || classifyStopReason(error.message);
    if (error.details) {
      const fallbackVideoInfo = state.videoInfo;
      Object.assign(state, error.details);
      if (!state.videoInfo) state.videoInfo = fallbackVideoInfo;
    }
  }

  outputDir = outputDir || options.outputDir || path.join(
    DEFAULT_OUTPUT_ROOT,
    `${safeFilePart(resolved.awemeId || 'unknown')}-${timestampForPath()}`,
  );
  let databasePath = DEFAULT_DATABASE_PATH;
  let databaseError = '';
  try {
    highLikeThreshold = getSettings({ databasePath: DEFAULT_DATABASE_PATH }).high_like_threshold;
  } catch (error) {
    databaseError = `读取本地设置失败: ${error.message}`;
  }
  try {
    databasePath = saveCollection({
      sourceUrl: options.reference,
      resolved,
      status,
      state,
      outputDir,
      errorMessage,
      scope: 'top_level',
    });
  } catch (error) {
    databaseError = `数据库写入失败: ${error.message}`;
  }
  const files = await writeOutputs({
    outputDir,
    input: options.reference,
    resolved,
    status,
    state,
    errorMessage,
    databasePath,
    databaseError,
    highLikeThreshold,
  });
  const summary = {
    status,
    scope: 'top_level',
    aweme_id: resolved.awemeId,
    top_comments: state.topCount,
    max_comments: options.maxComments,
    replies: state.replyCount,
    reply_counts_available: state.replyCountsAvailable || 0,
    next_cursor: state.nextCursor ?? null,
    stop_reason: state.stopReason || (status === 'completed' ? 'completed' : classifyStopReason(errorMessage)),
    high_like_threshold: highLikeThreshold,
    total_records: state.records.length,
    output_dir: outputDir,
    files,
    database: databasePath,
  };
  if (errorMessage) summary.error = errorMessage;
  if (databaseError) summary.database_error = databaseError;
  process.stdout.write(`${JSON.stringify(summary)}\n`);
  if (status !== 'completed') process.exitCode = 1;
}

if (require.main === module) {
  main().catch(error => {
    process.stderr.write(`错误: ${error.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = {
  CollectionError,
  BridgeClient,
  bridgeConnectionIndex,
  classifyStopReason,
  collectComments,
  csvValue,
  deriveTopic,
  extractAwemeId,
  normalizeComment,
  videoInfoFromReference,
  normalizeVideoInfo,
  parseArgs,
  resolveVideoReference,
  writeOutputs,
};
