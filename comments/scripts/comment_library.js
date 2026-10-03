#!/usr/bin/env node

const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');

const DEFAULT_DATABASE_PATH = path.resolve(__dirname, '..', 'data', 'douyin_comments.sqlite');

function openDatabase(databasePath = DEFAULT_DATABASE_PATH) {
  const resolvedPath = path.resolve(databasePath);
  fs.mkdirSync(path.dirname(resolvedPath), { recursive: true });
  const db = new DatabaseSync(resolvedPath);
  db.exec(`
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS videos (
      aweme_id TEXT PRIMARY KEY,
      source_url TEXT NOT NULL,
      resolved_url TEXT,
      first_collected_at TEXT NOT NULL,
      last_collected_at TEXT NOT NULL,
      last_status TEXT NOT NULL,
      scope TEXT NOT NULL DEFAULT 'top_level',
      top_comments INTEGER NOT NULL DEFAULT 0,
      replies INTEGER NOT NULL DEFAULT 0,
      reply_counts_available INTEGER NOT NULL DEFAULT 0,
      next_cursor INTEGER,
      total_records INTEGER NOT NULL DEFAULT 0,
      caption TEXT,
      topic TEXT,
      author_nickname TEXT,
      metadata_json TEXT,
      last_output_dir TEXT,
      last_error TEXT
    );
    CREATE TABLE IF NOT EXISTS comments (
      aweme_id TEXT NOT NULL,
      comment_key TEXT NOT NULL,
      comment_id TEXT,
      parent_id TEXT,
      level INTEGER NOT NULL,
      text TEXT NOT NULL,
      nickname TEXT,
      user_id TEXT,
      create_time TEXT,
      likes INTEGER NOT NULL DEFAULT 0,
      reply_count INTEGER NOT NULL DEFAULT 0,
      raw_json TEXT NOT NULL,
      first_seen_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      reviewed INTEGER NOT NULL DEFAULT 0,
      note TEXT NOT NULL DEFAULT '',
      tags TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (aweme_id, comment_key),
      FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id)
    );
    CREATE INDEX IF NOT EXISTS comments_video_time ON comments(aweme_id, create_time);
    CREATE INDEX IF NOT EXISTS comments_text ON comments(text);
    CREATE TABLE IF NOT EXISTS collection_runs (
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      aweme_id TEXT NOT NULL,
      source_url TEXT NOT NULL,
      collected_at TEXT NOT NULL,
      status TEXT NOT NULL,
      scope TEXT NOT NULL DEFAULT 'top_level',
      top_comments INTEGER NOT NULL DEFAULT 0,
      replies INTEGER NOT NULL DEFAULT 0,
      reply_counts_available INTEGER NOT NULL DEFAULT 0,
      next_cursor INTEGER,
      total_records INTEGER NOT NULL DEFAULT 0,
      output_dir TEXT,
      error TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    INSERT INTO settings (key, value) VALUES ('high_like_threshold', '100')
      ON CONFLICT(key) DO NOTHING;
  `);
  for (const column of [
    ['videos', 'caption', 'TEXT'],
    ['videos', 'topic', 'TEXT'],
    ['videos', 'author_nickname', 'TEXT'],
    ['videos', 'metadata_json', 'TEXT'],
    ['videos', 'scope', "TEXT NOT NULL DEFAULT 'top_level'"],
    ['videos', 'reply_counts_available', 'INTEGER NOT NULL DEFAULT 0'],
    ['videos', 'next_cursor', 'INTEGER'],
    ['comments', 'reviewed', 'INTEGER NOT NULL DEFAULT 0'],
    ['comments', 'note', "TEXT NOT NULL DEFAULT ''"],
    ['comments', 'tags', "TEXT NOT NULL DEFAULT ''"],
    ['collection_runs', 'scope', "TEXT NOT NULL DEFAULT 'top_level'"],
    ['collection_runs', 'reply_counts_available', 'INTEGER NOT NULL DEFAULT 0'],
    ['collection_runs', 'next_cursor', 'INTEGER'],
  ]) {
    try { db.exec(`ALTER TABLE ${column[0]} ADD COLUMN ${column[1]} ${column[2]}`); } catch { /* existing column */ }
  }
  try {
    const marker = db.prepare("SELECT value FROM settings WHERE key = 'likes_unknown_migrated'").get();
    if (!marker) {
      db.exec(`
        UPDATE comments
        SET likes = -1
        WHERE likes = 0
          AND json_extract(raw_json, '$.digg_count') IS NULL
          AND json_extract(raw_json, '$.likes') IS NULL
          AND json_extract(raw_json, '$.like_count') IS NULL
      `);
      db.prepare("INSERT INTO settings (key, value) VALUES ('likes_unknown_migrated', '1')").run();
    }
  } catch { /* Older SQLite builds may not include JSON functions. */ }
  return { db, path: resolvedPath };
}

function recordKey(awemeId, record) {
  if (record.comment_id) return String(record.comment_id);
  return `${awemeId}:${record.parent_id || 'top'}:${record.create_time || ''}:${record.text || ''}`;
}

function saveCollection({
  databasePath = DEFAULT_DATABASE_PATH,
  sourceUrl,
  resolved,
  status,
  state,
  outputDir,
  errorMessage = '',
  videoInfo = state.videoInfo || {},
  scope = 'top_level',
  replyCountsAvailable = state.replyCountsAvailable || 0,
  nextCursor = state.nextCursor ?? null,
  collectedAt = new Date().toISOString(),
}) {
  const { db, path: resolvedPath } = openDatabase(databasePath);
  const awemeId = String(resolved.awemeId || '');
  if (!awemeId) throw new Error('没有可写入数据库的视频 ID');

  const insertVideo = db.prepare(`
    INSERT INTO videos (
      aweme_id, source_url, resolved_url, first_collected_at, last_collected_at,
      last_status, scope, top_comments, replies, reply_counts_available, next_cursor,
      total_records, caption, topic, author_nickname, metadata_json, last_output_dir, last_error
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(aweme_id) DO UPDATE SET
      source_url = excluded.source_url,
      resolved_url = excluded.resolved_url,
      last_collected_at = excluded.last_collected_at,
      last_status = excluded.last_status,
      scope = excluded.scope,
      top_comments = excluded.top_comments,
      replies = excluded.replies,
      reply_counts_available = excluded.reply_counts_available,
      next_cursor = excluded.next_cursor,
      total_records = excluded.total_records,
      caption = excluded.caption,
      topic = excluded.topic,
      author_nickname = excluded.author_nickname,
      metadata_json = excluded.metadata_json,
      last_output_dir = excluded.last_output_dir,
      last_error = excluded.last_error
  `);
  const insertComment = db.prepare(`
    INSERT INTO comments (
      aweme_id, comment_key, comment_id, parent_id, level, text, nickname, user_id,
      create_time, likes, reply_count, raw_json, first_seen_at, last_seen_at, reviewed, note, tags
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', '')
    ON CONFLICT(aweme_id, comment_key) DO UPDATE SET
      comment_id = excluded.comment_id,
      parent_id = excluded.parent_id,
      level = excluded.level,
      text = excluded.text,
      nickname = excluded.nickname,
      user_id = excluded.user_id,
      create_time = excluded.create_time,
      likes = excluded.likes,
      reply_count = excluded.reply_count,
      raw_json = excluded.raw_json,
      last_seen_at = excluded.last_seen_at
  `);
  const insertRun = db.prepare(`
    INSERT INTO collection_runs (
      aweme_id, source_url, collected_at, status, top_comments, replies,
      scope, reply_counts_available, next_cursor, total_records, output_dir, error
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `);

  db.exec('BEGIN');
  try {
    insertVideo.run(awemeId, String(sourceUrl || ''), resolved.resolvedUrl || null, collectedAt, collectedAt,
      status, scope, state.topCount || 0, state.replyCount || 0, replyCountsAvailable, nextCursor, state.records.length,
      videoInfo.caption || null, videoInfo.topic || null, videoInfo.author_nickname || null,
      videoInfo.raw ? JSON.stringify(videoInfo.raw) : null, outputDir || null, errorMessage || null);
    for (const record of state.records) {
      insertComment.run(awemeId, recordKey(awemeId, record), record.comment_id || null, record.parent_id || null,
        record.level || 1, record.text || '', record.nickname || '', record.user_id || '',
        record.create_time === null || record.create_time === undefined ? null : String(record.create_time),
        record.likes === undefined || record.likes === null ? -1 : record.likes,
        record.reply_count || 0, JSON.stringify(record.raw || {}), collectedAt, collectedAt);
    }
    const totals = db.prepare(`
      SELECT COUNT(*) AS total_records,
             SUM(CASE WHEN parent_id IS NULL THEN 1 ELSE 0 END) AS top_comments,
             SUM(CASE WHEN parent_id IS NULL THEN 0 ELSE 1 END) AS replies
      FROM comments WHERE aweme_id = ?
    `).get(awemeId);
    db.prepare(`
      UPDATE videos SET top_comments = ?, replies = ?, total_records = ? WHERE aweme_id = ?
    `).run(Number(totals.top_comments || 0), Number(totals.replies || 0), Number(totals.total_records || 0), awemeId);
    insertRun.run(awemeId, String(sourceUrl || ''), collectedAt, status, state.topCount || 0,
      state.replyCount || 0, scope, replyCountsAvailable, nextCursor,
      state.records.length, outputDir || null, errorMessage || null);
    db.exec('COMMIT');
  } catch (error) {
    db.exec('ROLLBACK');
    throw error;
  } finally {
    db.close();
  }
  return resolvedPath;
}

function boundedInteger(value, fallback, maximum) {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 0) return fallback;
  return Math.min(maximum, number);
}

function listVideos({ databasePath = DEFAULT_DATABASE_PATH, keyword = '', limit = 50, offset = 0 } = {}) {
  const { db, path: resolvedPath } = openDatabase(databasePath);
  try {
    const term = `%${keyword}%`;
    const where = `WHERE (? = '' OR aweme_id LIKE ? OR caption LIKE ? OR topic LIKE ? OR author_nickname LIKE ?
      OR EXISTS (
        SELECT 1 FROM comments
        WHERE comments.aweme_id = videos.aweme_id
          AND (comments.text LIKE ? OR comments.nickname LIKE ? OR comments.tags LIKE ? OR comments.note LIKE ?)
      ))`;
    const safeLimit = boundedInteger(limit, 50, 200) || 50;
    const safeOffset = boundedInteger(offset, 0, 1000000000);
    const params = [keyword, term, term, term, term, term, term, term, term];
    const total = Number(db.prepare(`SELECT COUNT(*) AS count FROM videos ${where}`).get(...params).count || 0);
    return {
      databasePath: resolvedPath,
      rows: db.prepare(`
        SELECT aweme_id, source_url, resolved_url, caption, topic, author_nickname,
               last_collected_at, last_status, scope, top_comments, replies,
               reply_counts_available, next_cursor, total_records, last_error
        FROM videos
        ${where}
        ORDER BY last_collected_at DESC
        LIMIT ? OFFSET ?
      `).all(...params, safeLimit, safeOffset),
      total,
      limit: safeLimit,
      offset: safeOffset,
      has_more: safeOffset + safeLimit < total,
    };
  } finally { db.close(); }
}

function markdownText(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function exportCommentsMarkdown({
  databasePath = DEFAULT_DATABASE_PATH,
  awemeId = '',
  keyword = '',
  sort = 'likes',
  highLikeOnly = false,
  reviewedOnly = false,
  highLikeThreshold = 100,
} = {}) {
  const { db, path: resolvedPath } = openDatabase(databasePath);
  try {
    const term = `%${keyword}%`;
    const threshold = boundedInteger(highLikeThreshold, 100, 1000000000);
    const ordering = sort === 'created'
      ? 'v.last_collected_at DESC, CASE WHEN c.create_time IS NULL THEN 1 ELSE 0 END, c.create_time DESC, c.comment_id ASC'
      : 'v.last_collected_at DESC, CASE WHEN c.likes < 0 THEN 1 ELSE 0 END, c.likes DESC, c.comment_id ASC';
    const rows = db.prepare(`
      SELECT c.aweme_id, c.comment_key, c.comment_id, c.text, c.nickname, c.create_time,
             c.likes, c.reply_count, c.reviewed, c.note, c.tags,
             v.source_url, v.caption, v.topic, v.author_nickname, v.last_collected_at
      FROM comments c
      JOIN videos v ON v.aweme_id = c.aweme_id
      WHERE c.parent_id IS NULL
        AND (? = '' OR c.aweme_id = ?)
        AND (? = '' OR c.text LIKE ? OR c.nickname LIKE ? OR c.tags LIKE ? OR c.note LIKE ?
          OR v.caption LIKE ? OR v.topic LIKE ? OR v.author_nickname LIKE ?)
        AND (? = 0 OR c.likes >= ?)
        AND (? = 0 OR c.reviewed = 1)
      ORDER BY ${ordering}
    `).all(
      String(awemeId || ''), String(awemeId || ''), keyword,
      term, term, term, term, term, term, term,
      highLikeOnly ? 1 : 0, threshold, reviewedOnly ? 1 : 0,
    );

    const lines = [
      '# 抖音一级评论导出',
      '',
      `- 导出时间：${new Date().toISOString()}`,
      `- 导出范围：${awemeId ? `视频 ${markdownText(awemeId)}` : '知识库搜索结果'}`,
      `- 搜索条件：${keyword ? markdownText(keyword) : '无'}`,
      `- 高赞筛选：${highLikeOnly ? `仅导出不少于 ${threshold} 赞` : '未启用'}`,
      `- 一级评论：${rows.length} 条`,
      '',
    ];
    let currentVideo = '';
    let commentNumber = 0;
    for (const row of rows) {
      if (row.aweme_id !== currentVideo) {
        currentVideo = row.aweme_id;
        commentNumber = 0;
        lines.push(`## ${markdownText(row.topic || row.caption || row.aweme_id)}`, '');
        lines.push(`- 视频 ID：${markdownText(row.aweme_id)}`);
        if (row.author_nickname) lines.push(`- 作者：${markdownText(row.author_nickname)}`);
        if (row.source_url) lines.push(`- 来源：${markdownText(row.source_url)}`);
        lines.push('');
      }
      commentNumber += 1;
      const likes = Number(row.likes) < 0 ? '未知' : Number(row.likes).toLocaleString('zh-CN');
      lines.push(`### ${commentNumber}. ${markdownText(row.nickname || '未知用户')} · 赞 ${likes}${Number(row.likes) >= threshold ? ' · 高赞' : ''}`, '');
      const body = markdownText(row.text).split(/\r?\n/).map(line => `> ${line}`).join('\n');
      lines.push(body || '> ', '');
      if (row.tags) lines.push(`- 标签：${markdownText(row.tags)}`);
      if (row.note) lines.push(`- 备注：${markdownText(row.note)}`);
      lines.push(`- 复看状态：${row.reviewed ? '已复看' : '未复看'}`, '');
    }
    if (!rows.length) lines.push('没有符合当前条件的一级评论。', '');
    return {
      databasePath: resolvedPath,
      markdown: `${lines.join('\n')}\n`,
      count: rows.length,
      videoCount: new Set(rows.map(row => row.aweme_id)).size,
    };
  } finally { db.close(); }
}

function listComments({
  databasePath = DEFAULT_DATABASE_PATH,
  awemeId,
  keyword = '',
  limit = 100,
  offset = 0,
  sort = 'likes',
  highLikeOnly = false,
  reviewedOnly = false,
  highLikeThreshold = 100,
  includeReplies = false,
} = {}) {
  const { db, path: resolvedPath } = openDatabase(databasePath);
  try {
    const term = `%${keyword}%`;
    const safeLimit = boundedInteger(limit, 100, 100) || 100;
    const safeOffset = boundedInteger(offset, 0, 1000000000);
    const threshold = boundedInteger(highLikeThreshold, 100, 1000000000);
    const ordering = sort === 'created'
      ? 'CASE WHEN create_time IS NULL THEN 1 ELSE 0 END, create_time DESC, comment_id ASC'
      : 'CASE WHEN likes < 0 THEN 1 ELSE 0 END, likes DESC, comment_id ASC';
    const where = `
      WHERE aweme_id = ?
        AND (? = '' OR text LIKE ? OR nickname LIKE ? OR tags LIKE ? OR note LIKE ?)
        AND (? = 1 OR parent_id IS NULL)
        AND (? = 0 OR likes >= ?)
        AND (? = 0 OR reviewed = 1)
    `;
    const params = [String(awemeId || ''), keyword, term, term, term, term, includeReplies ? 1 : 0, highLikeOnly ? 1 : 0, threshold, reviewedOnly ? 1 : 0];
    const total = Number(db.prepare(`SELECT COUNT(*) AS count FROM comments ${where}`).get(...params).count || 0);
    const rows = db.prepare(`
        SELECT aweme_id, comment_key, comment_id, parent_id, level, text, nickname,
               user_id, create_time, likes, reply_count, reviewed, note, tags
        FROM comments
        ${where}
        ORDER BY ${ordering}
        LIMIT ? OFFSET ?
      `).all(...params, safeLimit, safeOffset).map(row => ({
        ...row,
        high_like: Number(row.likes) >= threshold,
      }));
    return {
      databasePath: resolvedPath,
      rows,
      total,
      limit: safeLimit,
      offset: safeOffset,
      has_more: safeOffset + safeLimit < total,
      high_like_threshold: threshold,
    };
  } finally { db.close(); }
}

function getSettings({ databasePath = DEFAULT_DATABASE_PATH } = {}) {
  const { db, path: resolvedPath } = openDatabase(databasePath);
  try {
    const rows = db.prepare('SELECT key, value FROM settings').all();
    const settings = Object.fromEntries(rows.map(row => [row.key, row.value]));
    const parsed = Number(settings.high_like_threshold);
    return {
      databasePath: resolvedPath,
      high_like_threshold: Number.isInteger(parsed) && parsed >= 0 ? parsed : 100,
    };
  } finally { db.close(); }
}

function updateSettings({ databasePath = DEFAULT_DATABASE_PATH, highLikeThreshold } = {}) {
  const threshold = Number(highLikeThreshold);
  if (!Number.isInteger(threshold) || threshold < 0 || threshold > 1000000000) {
    throw new Error('高赞门槛必须是 0 到 1000000000 之间的整数');
  }
  const { db, path: resolvedPath } = openDatabase(databasePath);
  try {
    db.prepare(`INSERT INTO settings (key, value) VALUES ('high_like_threshold', ?)
      ON CONFLICT(key) DO UPDATE SET value = excluded.value`).run(String(threshold));
    return { databasePath: resolvedPath, high_like_threshold: threshold };
  } finally { db.close(); }
}

function updateComment({ databasePath = DEFAULT_DATABASE_PATH, awemeId, commentKey, reviewed, note, tags }) {
  const { db, path: resolvedPath } = openDatabase(databasePath);
  try {
    const result = db.prepare(`
      UPDATE comments
      SET reviewed = COALESCE(?, reviewed), note = COALESCE(?, note), tags = COALESCE(?, tags)
      WHERE aweme_id = ? AND comment_key = ?
    `).run(reviewed === undefined ? null : (reviewed ? 1 : 0), note ?? null, tags ?? null, String(awemeId || ''), String(commentKey || ''));
    return { databasePath: resolvedPath, changed: Number(result.changes || 0) };
  } finally { db.close(); }
}

module.exports = {
  DEFAULT_DATABASE_PATH,
  exportCommentsMarkdown,
  getSettings,
  listComments,
  listVideos,
  openDatabase,
  saveCollection,
  updateComment,
  updateSettings,
};
