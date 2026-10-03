#!/usr/bin/env node

const { extractAwemeId } = require('./collect_comments');
const { DEFAULT_DATABASE_PATH, getSettings, listComments } = require('./comment_library');

function parseArgs(argv) {
  const options = {
    database: DEFAULT_DATABASE_PATH,
    video: '',
    keyword: '',
    limit: 100,
    offset: 0,
    sort: 'likes',
    highLikeOnly: false,
    threshold: null,
    json: false,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--database') options.database = argv[++i];
    else if (arg === '--video') options.video = argv[++i];
    else if (arg === '--keyword') options.keyword = argv[++i];
    else if (arg === '--limit') options.limit = Number(argv[++i]);
    else if (arg === '--offset') options.offset = Number(argv[++i]);
    else if (arg === '--sort') options.sort = argv[++i];
    else if (arg === '--high-like') options.highLikeOnly = true;
    else if (arg === '--threshold') options.threshold = Number(argv[++i]);
    else if (arg === '--json') options.json = true;
    else if (arg === '--help' || arg === '-h') options.help = true;
    else throw new Error(`未知选项: ${arg}`);
  }
  if (!options.help && (!options.video || !Number.isInteger(options.limit) || options.limit <= 0)) {
    throw new Error('用法: node scripts/query_comments.js --video <视频链接或 aweme_id> [--keyword 关键词] [--limit 100] [--offset 0] [--sort likes|created] [--high-like] [--threshold 100] [--json]');
  }
  return options;
}

function main(argv = process.argv.slice(2)) {
  const options = parseArgs(argv);
  if (options.help) {
    process.stdout.write('用法: node scripts/query_comments.js --video <视频链接或 aweme_id> [--keyword 关键词] [--limit 100] [--offset 0] [--sort likes|created] [--high-like] [--threshold 100] [--json]\n');
    return;
  }
  const awemeId = extractAwemeId(options.video) || String(options.video).trim();
  const settings = getSettings({ databasePath: options.database });
  const threshold = options.threshold === null ? settings.high_like_threshold : options.threshold;
  if (!['likes', 'created'].includes(options.sort)) throw new Error('--sort 只能是 likes 或 created');
  const result = listComments({
    databasePath: options.database,
    awemeId,
    keyword: options.keyword,
    limit: options.limit,
    offset: options.offset,
    sort: options.sort,
    highLikeOnly: options.highLikeOnly,
    highLikeThreshold: threshold,
  });
  if (options.json) process.stdout.write(`${JSON.stringify(result)}\n`);
  else {
    for (const row of result.rows) {
      const likes = Number(row.likes) < 0 ? '未知' : row.likes;
      process.stdout.write(`${row.high_like ? '[高赞] ' : ''}${row.nickname || '未知用户'}（赞 ${likes}）：${row.text}\n`);
    }
    process.stdout.write(`已加载 ${result.rows.length} 条，共 ${result.total} 条${result.has_more ? '，可继续翻页' : ''}\n`);
  }
}

try {
  main();
} catch (error) {
  process.stderr.write(`错误: ${error.message}\n`);
  process.exitCode = 2;
}
