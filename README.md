# ⚡ 赞了个雷

**别让有用的点赞，只留在“喜欢”里。**

把你喜欢的抖音视频变成本地学习笔记：实际视频音轨 → 本地语音转写 → Agent 清理、提炼、分类 → 可搜索网页与 Markdown。原始转写保留；娱乐、图文和失败项也保留来源。

这是本地工具与 Agent Skill 的组合，已验证 Windows + Python 3.12。Agent 需要有本地命令、文件读写和浏览器能力；纯聊天网页不能直接部署。Mac/Linux 尚未验收。

## 让你的 Agent 部署

复制这段给能操作你电脑的 Agent：

> 请从 https://github.com/zzz9219/zan-le-ge-lei 安装并部署“赞了个雷”。先读根目录 SKILL.md，复用合适的本地环境；让我自行登录我的抖音账号。用我的最近约200条点赞初始化学习库并生成适合我的主题，然后按阶段完成本地转写和AI整理。请先说明使用哪个AI模型与额度；暂不安装评论模块或开启定时任务。

仓库根目录就是 Skill，可安装到你的 Agent 支持的技能目录，也可克隆到独立项目后让 Agent 读取 `SKILL.md`。不要只复制提示词而漏掉脚本、网页与 vendor 文件。

## 第一次会发生什么

1. `setup.cmd` 安装独立 Python 环境、CPU Whisper 与模型；已有环境可由 Agent 检查后复用。
2. `start.cmd` 打开本地网页，`login.cmd` 弹出抖音扫码登录。
3. 冻结最近约200条喜欢作样本；不足200时按实际可访问数量。一生效就固定 ID，重跑不重选。
4. 一次完整扫描建立旧视频 ID 基线；仅收元数据，**不会下载全部历史视频**。之后日常只做前部增量扫描。
5. 集中转写样本，按样本内容归纳你的主题，再集中整理成学习笔记。

**每个人的主题不同。** 新库只有“未分类”，没有作者的AI/成长/商业分类模板。个人主题保存后可在网页新增，并修改任一笔记的归属。主题初次归纳使用明确标记的内容片段；全文摘要另按完整转写处理。

手动命令（在仓库根目录）：

```powershell
py -3.12 bootstrap.py
.venv\Scripts\python.exe entry_probe.py login
.venv\Scripts\python.exe onboarding.py collect --count 200
.venv\Scripts\python.exe library.py scan
.venv\Scripts\python.exe library.py transcribe --scope backfill --limit 200
.venv\Scripts\python.exe onboarding.py context
```

接下来由 Agent 根据返回文件生成个人主题并 `apply-themes`，再执行 `pending → AI → apply`。详见 [SKILL.md](SKILL.md)。Python 未安装时，可先让 Agent 安装官方 Python 3.12；没有适用的 Chrome/Edge 时初始化会准备 Playwright Chromium。

## 你能看到什么

网页默认 `http://127.0.0.1:19423/`，两列卡片、点击展开学习笔记、先看结论、完整原文、原视频链接、主题与全文搜索、互动数据/发布日期筛选、手动分类及 Markdown 下载。轻量动画与自定义菜单支持键盘及减少动态效果设置。

“待转写”是本地音轨处理尚未完成；“待提炼”已有正文，可打开阅读；“已整理”有AI摘要与分类。图文、视频无音轨、识别为空、下载失败分别显示原因，识别为空不能证明没有口播。入库日期是首次发现日期，不是准确点赞时间。

## 哪些步骤消耗AI额度

|步骤|在哪里运行|语言模型额度|
|---|---|---|
|登录、抓视频ID、元数据去重、下载|你的电脑|不使用|
|Whisper 转写|你的电脑 CPU|不使用|
|首次归纳个人主题|你选择的 Agent/模型|使用|
|清理、结论、摘要、分类|你选择的 Agent/模型|使用|
|网页、搜索、Markdown、评论采集|你的电脑|不使用|
|由AI宿主唤醒的定时检查|所选AI宿主|可能使用，即使没有新正文|

没有内置付费AI API，也没有强制模型。部署时按你的 Agent 能力选择模型；运行型号须以宿主实际配置为准。初始200条的模型整理不是免费的本地步骤。每批最多5条/12000字，长文分段，原始正文不截断；不读取全库历史。日常默认每日500条上限，积压留队列。

计时、音频时长、批次数留在本地。能取得真实 token 就记录，拿不到则明确 unknown。额度不足保留已下载/转写成果。视频临时 MP4 在转写文本保存校验成功后删除，保留失败文件便于排查。

## 新增同步与定时

`library.py daily` 检查列表前部至少1000个唯一ID，再连续3页无新ID就停止，最多100页。排序未严格验证，列表深处的异常新增可能漏掉；全量核对只在明确要求时 `library.py scan --full`。不会每半天重扫所有历史视频。

先本地转写，再集中AI提炼；已确认的最新范围优先，历史冻结队列随后继续。任务锁防重叠，评论锁独立。登录失效、验证码、限流停止该轮，恢复登录后续跑。

部署不会自动创建定时任务。想自动运行时，让你的 Agent 根据实际宿主配置执行聊天/自动化（如每日00:00/12:00），电脑与所用宿主需运行。后台本地调度与AI唤醒分别配置，维护聊天与执行聊天可分开。

## 可选：评论深挖

基础点赞、转写、知识库**无需篡改猴**。评论模块按需安装 Node 22.5+、本地浏览器桥与 Tampermonkey 用户脚本，然后在抖音页登录。详见 [评论连接说明](docs/comments.md)。

只读一级评论，默认最多1000条；列表前部采样后按赞数排序展示，不保证全评论区绝对前1000。页面显示实际进度、断点、分页与导出，不发评论，不自动AI分析评论。

## 验证与已知边界

测试：`.venv\Scripts\python.exe -m unittest test_library test_web test_onboarding`；可选评论 `node --test comments/scripts/collector.test.js`。发布验收记录见 [handoff.md](handoff.md)。

接口可访问性依赖本人登录及平台当前行为；其他人的真实账号、地区网络与中文识别效果仍需部署时验证。图文暂不做OCR；空识别需要核对；前部扫描不是全库保证。服务仅绑定本机，浏览器能查看不代表可直接部署成多人在线SaaS。

## 来源与许可

主体复用 [tars1230/douyin-favorites-to-knowledge](https://github.com/tars1230/douyin-favorites-to-knowledge) 的 MIT 采集器与 Whisper/Markdown流程（2.3.2 固定版本），增加本地学习库、阶段队列、个人主题、网页交互与可选评论。

上游另带的专有 `douyin-knowledge-core` wheel **不包含在这里，也无需用户另行下载**；所需工具函数由独立标准库实现替代。详情及第三方版权见 [NOTICE.md](NOTICE.md)、[LICENSE](LICENSE)、[vendor/favorites/LICENSE](vendor/favorites/LICENSE)。仓库不含作者的点赞数据、转写记录、登录信息或AI密钥。
