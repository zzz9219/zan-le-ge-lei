# MIT collector included in 赞了个雷

Derived from [tars1230/douyin-favorites-to-knowledge](https://github.com/tars1230/douyin-favorites-to-knowledge), version 2.3.2, commit `d51ab25cb64ff2abca841d9560545dc14b49630d`.

This distribution retains the MIT collector, security helpers, Markdown workflow and local Whisper adapter. The proprietary `douyin-knowledge-core` wheel is excluded. `core_bridge.py` is an independent standard-library implementation of the public caller contract; `pyproject.toml` no longer installs that wheel.

Optional upstream cloud ASR/Feishu/provider modules remain source only and are not enabled by the host application's bootstrap or skill. The host uses local CPU Whisper and its agent for text analysis.

See `LICENSE` for the upstream MIT notice and the repository root `NOTICE.md` for attribution and modifications.
