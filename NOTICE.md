# Third-party notices and changes

## Favorites collector

- Source: https://github.com/tars1230/douyin-favorites-to-knowledge
- Upstream version: 2.3.2
- Pinned commit: d51ab25cb64ff2abca841d9560545dc14b49630d (local checkout obtained via upstream Gitee mirror)
- License: MIT, Copyright (c) 2026 Cheng Chen (tars1230); original notice preserved in vendor/favorites/LICENSE.
- Local modifications include CPU ASR diagnostics/empty-recognition fallback, statistics metadata, and the independent standard-library core_bridge.py. The main collector is reused.
- The upstream douyin-knowledge-core 0.2.1 wheel has LicenseRef-Proprietary metadata and prohibits copying/modifying/distributing/incorporating without permission. This distribution omits it and contains no source taken from its implementation. Users do not need it.

## Optional comment bridge

Source: https://github.com/qinuoyun/douyin

The MIT bridge server, registry, router, protocol and source userscript are included in comments/douyin-upstream. Its original LICENSE is preserved there. The distribution includes only the bridge needed by the read-only collector; upstream posting and paid LLM configuration are not enabled. The local collector adds limits, progress, deduplication and SQLite/Markdown export.

Python/Node packages are installed by package managers, not bundled as binaries. Their respective licenses remain with those packages. User videos, comments, transcriptions and login profiles are not included in this source repository.
