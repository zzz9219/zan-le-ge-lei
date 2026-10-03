#!/usr/bin/env node

// Prepare the local Bridge config and a Tampermonkey script with the same token.
// The generated files stay local; no credential is sent to a remote service.

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const packageRoot = path.resolve(__dirname, '..');
const bridgeRoot = path.join(packageRoot, 'douyin-upstream');
const configPath = path.join(bridgeRoot, 'config.json');
const examplePath = path.join(bridgeRoot, 'config.example.json');
const sourceUserScript = path.join(bridgeRoot, 'scripts', 'douyin.user.js');
const localUserScript = path.join(bridgeRoot, 'scripts', 'douyin.user.local.js');

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function main() {
  if (!fs.existsSync(configPath)) {
    fs.copyFileSync(examplePath, configPath);
  }
  const config = readJson(configPath);
  config.bridge = config.bridge || {};
  if (!config.bridge.token) {
    config.bridge.token = crypto.randomBytes(24).toString('hex');
  }
  fs.writeFileSync(configPath, `${JSON.stringify(config, null, 2)}\n`, 'utf8');

  const source = fs.readFileSync(sourceUserScript, 'utf8');
  const tokenLine = `token: '${config.bridge.token}',  // 填入 config.json 中的 bridge.token`;
  const generated = source.replace(
    /token:\s*'[^']*',\s*\/\/\s*填入 config\.json 中的 bridge\.token/,
    tokenLine,
  );
  if (generated === source) {
    throw new Error('无法在 douyin.user.js 中定位 token 配置行');
  }
  fs.writeFileSync(localUserScript, generated, 'utf8');

  process.stdout.write(`${JSON.stringify({
    config: configPath,
    userscript: localUserScript,
    bridge: `http://${config.bridge.host || '127.0.0.1'}:${config.bridge.port || 19422}`,
  })}\n`);
}

try {
  main();
} catch (error) {
  process.stderr.write(`错误: ${error.message}\n`);
  process.exitCode = 1;
}
