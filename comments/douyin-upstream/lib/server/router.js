// lib/server/router.js — HTTP API 路由（/call, /status, /health）
//
// 认证：/api/call, /api/connect, /api/poll, /api/result 需要 token
//       /api/health, /api/status 公开（只读监控）
// CORS：仅允许 localhost 来源

const { randomUUID } = require('crypto');
const { validateCallRequest } = require('../shared/protocol');

// 允许的 CORS 来源
const ALLOWED_ORIGINS = [
  'http://127.0.0.1',
  'http://localhost',
  'https://www.douyin.com',
  'https://douyin.com',
];

class Router {
  /**
   * @param {object} options
   * @param {import('./registry').ConnectionRegistry} options.registry
   * @param {import('./ws-hub').WebSocketHub} options.wsHub
   * @param {number} options.requestTimeout
   * @param {string} options.token - 访问令牌
   */
  constructor(options) {
    this.registry = options.registry;
    this.wsHub = options.wsHub;
    this.requestTimeout = options.requestTimeout || 30000;
    this.token = options.token || '';

    /** @type {Map<string, { resolve: Function, reject: Function, timer: NodeJS.Timeout }>} */
    this._pending = new Map();

    // HTTP 轮询队列：connectionId → [{ msgId, expression, awaitPromise }]
    this._pollQueue = new Map();
    // HTTP 轮询等待者：connectionId → [{ res, timer }]
    this._pollWaiters = new Map();

    // 监听 ws-hub 的 result 事件
    this.wsHub.on('result', (msg) => {
      const pending = this._pending.get(msg.id);
      if (pending) {
        clearTimeout(pending.timer);
        this._pending.delete(msg.id);

        if (msg.error) {
          pending.reject(new Error(msg.error));
        } else {
          pending.resolve(msg.value);
        }
      }
    });
  }

  /**
   * 处理 HTTP 请求
   * @param {import('http').IncomingMessage} req
   * @param {import('http').ServerResponse} res
   */
  async handle(req, res) {
    // WebSocket 升级请求交给 ws-hub，router 不介入
    if (req.headers.upgrade && req.headers.upgrade.toLowerCase() === 'websocket') {
      return;
    }

    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const path = url.pathname;
    const method = req.method.toUpperCase();

    // CORS：仅允许已知来源（localhost + douyin.com）
    const origin = req.headers.origin || '';
    if (ALLOWED_ORIGINS.some(o => origin.startsWith(o)) || !origin) {
      res.setHeader('Access-Control-Allow-Origin', origin || '*');
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
      res.setHeader('Access-Control-Allow-Private-Network', 'true');
    }

    if (method === 'OPTIONS') {
      res.writeHead(204);
      res.end();
      return;
    }

    try {
      // 公开端点：无需认证
      if (method === 'GET' && path === '/api/health') {
        return this._health(res);
      }
      if (method === 'GET' && path === '/api/status') {
        return this._status(res);
      }

      // 受保护端点：需要 token 认证
      if (!this._authenticate(req, res)) return;

      if (method === 'POST' && path === '/api/call') {
        return await this._call(req, res);
      }
      if (method === 'POST' && path === '/api/connect') {
        return await this._connect(req, res);
      }
      if (method === 'GET' && path === '/api/poll') {
        return await this._poll(url, res);
      }
      if (method === 'POST' && path === '/api/poll') {
        return await this._pollPost(req, res);
      }
      if (method === 'POST' && path === '/api/result') {
        return await this._result(req, res);
      }

      // 404
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'Not found' }));
    } catch (e) {
      console.error(`[router] Unhandled error: ${e.message}`);
      if (!res.headersSent) {
        res.writeHead(500, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: e.message }));
      }
    }
  }

  /**
   * Token 认证检查
   * 支持 Authorization: Bearer <token> 或 ?token=<token> 查询参数
   */
  _authenticate(req, res) {
    if (!this.token) return true; // 未配置 token 则跳过认证

    const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
    const authHeader = req.headers.authorization || '';
    const queryToken = url.searchParams.get('token') || '';

    const token = authHeader.startsWith('Bearer ')
      ? authHeader.slice(7)
      : queryToken;

    if (token !== this.token) {
      res.writeHead(401, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'Unauthorized — 无效的 access token' }));
      return false;
    }
    return true;
  }

  _health(res) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ok: true,
      uptime: Math.floor(process.uptime()),
      version: '1.0.0',
      connections: this.registry.totalConnections,
    }));
  }

  _status(res) {
    this.registry.pruneStaleHttp();
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ok: true,
      connections: this.registry.list(),
      totalConnections: this.registry.totalConnections,
      uptime: Math.floor(process.uptime()),
    }));
  }

  async _call(req, res) {
    // 解析 body
    const body = await this._readBody(req);

    const result = validateCallRequest(body);
    if (!result.valid) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: result.error }));
      return;
    }

    const { site, expression, awaitPromise, connIndex, connectionId, timeout } = result.data;
    const msgId = randomUUID();
    const effectiveTimeout = timeout || this.requestTimeout;
    this.registry.pruneStaleHttp();

    // 方式1：WebSocket 路径
    let conn = connectionId ? this.registry.getById(connectionId) : this.registry.get(site, connIndex);
    if (conn?.site !== site) conn = null;
    console.error(JSON.stringify({ event: 'bridge_call', id: msgId, connection: conn?.id, at: new Date().toISOString() }));
    if (conn && conn.ws) {
      const pendingPromise = new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          this._pending.delete(msgId);
          reject(new Error(`Request timeout after ${effectiveTimeout}ms`));
        }, effectiveTimeout);
        this._pending.set(msgId, { resolve, reject, timer });
      });

      try {
        this.wsHub.sendEval(conn, msgId, expression, awaitPromise);
      } catch (e) {
        const p = this._pending.get(msgId);
        if (p) { clearTimeout(p.timer); this._pending.delete(msgId); }
      }

      if (this._pending.has(msgId)) {
        try {
          const value = await pendingPromise;
          res.writeHead(200, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ ok: true, value, connection: conn.id }));
          return;
        } catch (e) {
          // WS 超时，fallthrough 到 HTTP 轮询
        }
      }
    }

    if (!conn) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: `No connection for site ${site}` }));
      return;
    }

    // 方式2：HTTP 轮询 — 只投递给选中的 connection ID
    const pollKey = conn.id;
    const waiters = this._pollWaiters.get(pollKey);
    if (waiters && waiters.length > 0) {
      const waiter = waiters.shift();
      if (waiters.length === 0) this._pollWaiters.delete(pollKey);
      clearTimeout(waiter.timer);

      const pendingPromise = new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          this._pending.delete(msgId);
          reject(new Error(`Request timeout after ${effectiveTimeout}ms`));
        }, effectiveTimeout);
        this._pending.set(msgId, { resolve, reject, timer });
      });

      waiter.res.writeHead(200, { 'Content-Type': 'application/json' });
      console.error(JSON.stringify({ event: 'bridge_dispatch', id: msgId, connection: pollKey, at: new Date().toISOString() }));
      waiter.res.end(JSON.stringify({ ok: true, type: 'eval', id: msgId, expression, awaitPromise }));

      try {
        const value = await pendingPromise;
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, value, connection: pollKey }));
        return;
      } catch (e) {
        res.writeHead(503, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: false, error: e.message }));
        return;
      }
    }

    // 方式3：无等待者 → 放入队列，等 poll 来取
    if (!this._pollQueue.has(pollKey)) this._pollQueue.set(pollKey, []);
    this._pollQueue.get(pollKey).push({ msgId, expression, awaitPromise });

    const pendingPromise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(msgId);
        const queue = this._pollQueue.get(pollKey);
        if (queue) {
          const idx = queue.findIndex(c => c.msgId === msgId);
          if (idx !== -1) queue.splice(idx, 1);
        }
        reject(new Error(`Request timeout after ${effectiveTimeout}ms — no polling client connected`));
      }, effectiveTimeout);
      this._pending.set(msgId, { resolve, reject, timer });
    });

    try {
      const value = await pendingPromise;
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, value, connection: pollKey }));
    } catch (e) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: e.message }));
    }
  }

  // ── HTTP 轮询：注册 ──
  async _connect(req, res) {
    const body = await this._readBody(req);
    const site = body.site;
    if (!site) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'site required' }));
      return;
    }
    const meta = {
      url: body.url || '',
      title: body.title || '',
      userAgent: body.userAgent || '',
      protocol: Number(body.protocol || 1),
      clientVersion: body.clientVersion || '',
    };
    const conn = this.registry.register(site, null, meta);
    console.log(`[router] poll client: ${site} (${conn.id.slice(0, 8)})`);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, id: conn.id }));
  }

  // ── HTTP 轮询：等待命令（长轮询）──
  async _poll(url, res) {
    const site = url.searchParams.get('site');
    const connectionId = url.searchParams.get('connection_id');
    if (!site || !connectionId) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'site and connection_id required' }));
      return;
    }
    return this._pollConnection(site, connectionId, res);
  }

  // 协议 3：提交上一次结果并在同一个请求中等待下一条命令。
  async _pollPost(req, res) {
    const body = await this._readBody(req);
    const site = body.site;
    const connectionId = body.connection_id;
    if (!site || !connectionId) {
      res.writeHead(400, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'site and connection_id required' }));
      return;
    }
    if (body.result) this._resolveResult(body.result);
    return this._pollConnection(site, connectionId, res);
  }

  async _pollConnection(site, connectionId, res) {
    const conn = this.registry.getById(connectionId);
    if (!conn || conn.site !== site || conn.ws) {
      res.writeHead(404, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'poll connection not found' }));
      return;
    }
    this.registry.touch(connectionId);

    // 队列中有待处理命令 → 立即返回
    const queue = this._pollQueue.get(connectionId);
    if (queue && queue.length > 0) {
      const cmd = queue.shift();
      console.error(JSON.stringify({ event: 'bridge_dispatch', id: cmd.msgId, connection: connectionId, at: new Date().toISOString() }));
      if (queue.length === 0) this._pollQueue.delete(connectionId);
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, type: 'eval', id: cmd.msgId, expression: cmd.expression, awaitPromise: cmd.awaitPromise }));
      return;
    }

    // 无命令 → 长轮询等待（5s 超时）
    const timer = setTimeout(() => {
      const waiters = this._pollWaiters.get(connectionId);
      if (waiters) {
        const idx = waiters.findIndex(w => w.res === res);
        if (idx !== -1) waiters.splice(idx, 1);
        if (waiters.length === 0) this._pollWaiters.delete(connectionId);
      }
      if (!res.headersSent) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, type: 'idle' }));
      }
    }, 5000);

    if (!this._pollWaiters.has(connectionId)) this._pollWaiters.set(connectionId, []);
    this._pollWaiters.get(connectionId).push({ res, timer });

    res.on('close', () => {
      clearTimeout(timer);
      const waiters = this._pollWaiters.get(connectionId);
      if (waiters) {
        const idx = waiters.findIndex(w => w.res === res);
        if (idx !== -1) waiters.splice(idx, 1);
        if (waiters.length === 0) this._pollWaiters.delete(connectionId);
      }
    });
  }

  // ── HTTP 轮询：提交 eval 结果 ──
  async _result(req, res) {
    const body = await this._readBody(req);
    this._resolveResult(body);

    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true }));
  }

  _resolveResult(body) {
    const { id, value, error, connection_id: connectionId } = body || {};
    if (connectionId) this.registry.touch(connectionId);

    const pending = this._pending.get(id);
    const connection = this.registry.getById(connectionId);
    if (connection) {
      connection.lastResponseAt = new Date().toISOString();
      connection.lastResponseOnTime = Boolean(pending);
    }
    console.error(JSON.stringify({ event: 'bridge_result', id, connection: connectionId, matched: Boolean(pending), at: new Date().toISOString() }));
    if (pending) {
      clearTimeout(pending.timer);
      this._pending.delete(id);
      if (error) pending.reject(new Error(error));
      else pending.resolve(value);
    }
  }

  _readBody(req) {
    return new Promise((resolve, reject) => {
      let data = '';
      let rejected = false;
      req.on('data', chunk => {
        if (rejected) return;
        data += chunk;
        // 限制 body 大小 1MB
        if (data.length > 1024 * 1024) {
          rejected = true;
          req.destroy();
          reject(new Error('Request body too large'));
        }
      });
      req.on('end', () => {
        if (rejected) return;
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          reject(new Error('Invalid JSON in request body'));
        }
      });
      req.on('error', reject);
    });
  }
}

module.exports = { Router };
