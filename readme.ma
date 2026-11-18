# 当前版本：学记个人学习备考助手（2026-11-17）

本节替代后文旧阶段中有冲突的描述。项目定位是单机学习助手：上传当前对话的教材或讲义，基于资料问答，把需要复习的问答收录到错题本，再按主题或错题生成练习。当前没有登录与账号隔离，不是已经上线的多用户服务。

## 本轮新增：公开资料评测与可靠性修复（2026-11-17）

不需要先准备个人备考文件，也不需要购买资料。已经接入百度官方 DuReader Checklist 的固定公开子集，120 份原文、120 道原始标注题，调参和留出各含 30 道有答案题、30 道无答案题。只在 `noim/public-eval` 下载和建库，未导入正式资料库；noim 已加入 Git 忽略规则。

本轮完成的基线结果：两个分组各 30 份资料同时入库，60 道有答案题的目标文档均进入前 5；但相关性门槛 0.12 在留出集只拦下 5/30 道无答案题。按调参集选择的 0.99480528 能在留出拦下 29/30，却只能保留 12/30 道有答案题，所以没有把它替换为正式门槛。相关性不能等同于可回答性，不能宣称已经达到两项 90%。完整基线报告在 `noim/public-eval/run-20261117-200044-807455/report.md`。

基线另跑了 24 道真实模型问答，发现会用相关背景替代所问操作的情况。因此默认问答及引用纠正提示现在明确核对主体、属性、操作、时间和关系：单个问题的关键答案缺失时提示资料不足；真正包含多个子问题时才允许部分作答。提示规则仍不是事实正确性的硬保证。另试验了 `retrieval.answerability_check`：额外调用本地模型核对直接依据并校验原文引句。对照中虽然减少误答，却将 12 道原始有答案题中的 4 道拒绝，6 道部分覆盖诊断题全部拒绝，因此该实验默认关闭，不能把它宣传为已解决可靠性。该核对不属于通用知识回答；通用回答仍须本次明确同意。

另外修复了两个实际发现的问题：出题 JSON 合法但选项、依据编号、原文引句不合规时，最多再纠错一次，总共最多两次生成调用，已经合格的题保留，引文允许中英文标点、引号和空白的排版差异，但仍校验数字、小数点和关键字词；Windows 临时占用注册表文件时，原子替换最多重试 5 次，仍失败则报错，绝不先删除原注册表。五组隔离回归测试通过。

本轮最终汇总见 `noim/public-eval/REPORT.md`。排版归一化后的出题专项请求 9 题，8 题通过结构、引句与模型审题，1 题仍被拒；这不是人工正确率。默认提示下的换表达和部分覆盖仍有失败，完整结果与未启用实验均在报告中，不能称为所有问题已修完。

可重复执行：

```powershell
cd "D:\python projects\agent\one_project\daima"
python -B tests/eval/prepare_public.py
python -B tests/eval/public_benchmark.py --answers-per-class 6
python -B tests/eval/public_diagnostics.py
```

`prepare_public.py` 固定数据包 SHA256，按原文哈希确定样本，原始问题和标准答案不混入资料；`public_benchmark.py` 分开报告多文档检索、门槛与最终问答，绝不自动修改线上门槛；`public_diagnostics.py` 对三份资料检查换表达、部分覆盖、资料外问题及实际出题。详细方法见 `daima/tests/eval/README.md`。公开数据包含旧网页和歧义标注，只用作评测，不等于经过核验的学习教材，也不等于真实备考效果。

## 启动与升级

在 PowerShell 中运行：

```powershell
cd "D:\python projects\agent\one_project\daima"
python -B -m uvicorn api.app:app --host 127.0.0.1 --port 8000
```

需要先启动 Ollama，并准备 `qwen3:8b`。如果 `python` 不是原环境，可使用 `& 'D:\tools\python\python.exe' -B -m uvicorn api.app:app --host 127.0.0.1 --port 8000`。浏览器访问 `http://127.0.0.1:8000/`。先停止旧服务再启动，网页按 Ctrl+F5 刷新。

本次直接覆盖源文件，没有清空正式数据库和原件。旧资料要在对应对话的学习资料列表点击“升级索引”，原件会重新切分并生成向量；新上传文件自动使用新版。处理完成前不要删除资料。重建后块编号可能改变，新块 ID 带版本，历史回答中的旧编号只代表当时的资料版本，不应拿它直接对应重建后的第 N 块。

页面采用你提供的新前端，并修复了刷新恢复对话、通用回答确认重复气泡、真实步骤显示、停止回答、上传错误与重复文件计数、错题筛选统计、试卷答案折叠、换对话清空试卷及持续显示出题错误。已移除主流程用不到的长时记忆入口，系统状态明确区分“已配置”和“尚未检测连接”。

## 模块地图与修改位置

| 文件（相对于 daima） | 作用 | 常见修改 |
|---|---|---|
| `web/index.html` | 页面结构、CSS、浏览器 JavaScript，包含聊天、资料上传、会话管理、错题本、试卷 | 页面布局、颜色、按钮、交互；给前端助手的主要文件 |
| `api/app.py` | HTTP 接口、聊天 SSE、上传、删除、重建索引、出题路由 | 请求字段、返回字段、前后端衔接 |
| `core/engine.py` | 当前问答入口：会话锁、超时、失败回滚、保存无依据提示 | 当前主流程调度 |
| `core/document_chat.py` | 资料优先问答、追问补全、引用检查、实验性直接依据核对、单次通用知识授权 | 回答规则、提示词、引用行为 |
| `rag/evidence.py` | 聊天与出题共用的向量 + BM25 检索与重排 | 检索候选、相关性过滤、来源字段 |
| `rag/chunker.py` | Markdown/数字标题切分、标题路径、长段落按句子切分 | 默认 800 字上限；改后旧文档需重建索引 |
| `rag/loader.py` | TXT、MD、PDF、DOCX 转文字 | 编码支持、文档解析；无文字 PDF 提示先 OCR |
| `rag/kb_manager.py` | 上传原件、注册表、后台索引、文档范围、缓存与删除 | 文档生命周期与索引版本 |
| `rag/retriever.py` | 重排模型包装及保留的旧检索兼容代码 | 重排模型加载与显式 Sigmoid 分数 |
| `core/exam_builder.py` | 生成题目、验证 JSON/题型/引句、模型审题、试卷拼装 | 出题和审题提示词、每题来源及拒绝原因 |
| `core/mistakes.py` | SQLite 错题卡片、掌握状态、收录摘要和导出 | 错题规则与数据字段 |
| `core/session.py` | SQLite 会话历史、名称、归档状态 | 会话持久化与删除 |
| `core/llm.py` | 本地/兼容模型调用、超时、重试和文字提取 | 协议调用；应用的 meta 字段不会传给模型 |
| `core/config.py`、`config.yaml` | 统一加载配置，支持隔离数据目录 | 模型名称、超时、检索参数、存储路径 |
| `core/credentials.py` | 本地模型配置和云端凭据读取 | 切换供应商及环境变量优先级 |
| `core/legacy_engine.py` | 移出的历史工具自主调用流程 | 仅保留参考及兼容测试，不是网页或 CLI 默认入口 |
| `tests/run.py` | 一条命令运行隔离回归测试 | 新增回归用例 |
| `tests/acceptance.py` | 使用真实本地模型与真实索引跑接口验收 | 实际问答、收录、出题和删除验收 |
| `tests/eval/run.py` | 真实 Chroma 检索评测、阈值扫描 | 评估检索效果，结果写 noim |

旧工具循环中的联网、记忆自动提取、自动收录和反思开关已从当前配置移除，避免误以为修改它们会影响默认流程。历史实现仍在，不要用它的行为解释新版。CLI `/new` 现在创建独立空白对话，不再清空原对话。

## 在哪里换模型

主要编辑 `daima/config.yaml`：

```yaml
llm:
  provider: "ollama"
  base_url: "http://localhost:11434"
  model: "qwen3:8b"
  thinking: false
  chat_timeout: 300
  llm_timeout: 120
embedding:
  model: "moka-ai/m3e-base"
rerank:
  model: "BAAI/bge-reranker-large"
```

三种模型作用不同：`llm.model` 负责回答、追问改写、收录摘要、出题和审题；`embedding.model` 把文字转成检索向量；`rerank.model` 给候选段落重新打分。本地模式环境变量 `OLLAMA_MODEL`、`OLLAMA_BASE_URL` 优先于 YAML；改完后重启服务。换回答模型通常不需要重建索引，换嵌入模型需要迁移/重建整个向量库；维度变化时不能直接往旧 collection 写新向量，不建议仅改名称就使用旧库。

`retrieval.evidence_min_score: 0.12` 是显式 Sigmoid 0–1 分数的暂定门槛，不是“正确率 12%”。检索不够相关时程序询问用户，不自动调用通用回答。有历史的追问可能先调用模型补全问题，这是辅助理解，不是让模型跳过资料直接回答。

引用编号只证明对应到本次提供的资料块，不能证明模型每句话都完全正确。没有引用或编号越界会尝试修正一次；仍不合规则标“出处未验证”。试题另外要求原文引句匹配、题型结构合规。审题异常时保留合规题，但明确标为“未审核”；明确审题不通过的题会丢弃。

## 数据、删除与测试

SQLite 保留，聊天和错题仍持久化；Redis 镜像默认关闭，不要求购买 Redis 或 SQL 服务。向量库仍是本地 Chroma。文档按对话隔离，暂未升级科目空间。

删除文档会删除应用管理的原件、对应向量与文本块、注册表记录，以及 BM25 磁盘和内存缓存；删除失败不报告成功。删除整个对话再级联删除该对话的消息和来源错题。已有聊天回答、单独保存的试卷下载件不是文档索引；单删资料不会自动清除这些独立记录。应用层不可检索不等于 SSD、数据库空闲页或系统备份的字节级安全擦除。

```powershell
cd "D:\python projects\agent\one_project\daima"
python -B tests/run.py
python -B tests/acceptance.py
python -B tests/eval/run.py --document data/uploads/1a6a9ab785a4.md
```

`tests/run.py` 为每组测试创建独立目录，退出后清理。`XUEJI_DATA_DIR` 覆盖上传、Chroma、缓存、SQLite 和日志路径，并禁用 Redis。真实验收与评测结果保留在 `one_project/noim`，可删除；不要把测试环境变量留在正式服务终端里。

本轮验证记录：四组回归测试已通过；真实 `qwen3:8b` 在生成的采购讲义上跑通上传索引、资料内引用回答、资料外询问、明确同意通用回答、收录、两种出题及删除后向量归零。结果在 `noim/real-acceptance/result.json`。这是一轮小样本验收，不代表各种教材和题型均已验证。

浏览器授权后已完成真实页面验收：空库提示、通用回答确认不重复问题、文件选择上传测试讲义并就绪、资料问答与引用、收录与标为已掌握、按主题生成三题、答案折叠展开、Markdown 下载、服务重启后资料/聊天/收录/掌握状态恢复，以及 390×844 手机宽度无水平溢出。无资料出题错误持续显示，新对话清空试卷预览，停止按钮空闲时隐藏。首次加载向量模型现在显示明确阶段，知识库初始化移入工作线程并增加单例锁；修改后四组回归测试再次通过。前端 JavaScript 语法及元素 ID 检查通过。验收清单和截图分别为 `noim/browser-verification.json`、`noim/browser-verified.jpg`。

本次网页按错题生成两题曾因选项不合规和来源编号越界被拒绝，错误原因持续显示；随后按主题生成三题成功。这说明校验能拦截这些不合格输出，但不代表本地模型出题质量已稳定。操作系统文件拖拽、会话管理的完整浏览器流程、模型断连时的完整浏览器流程仍未覆盖，不能把接口测试当作全部页面验收。

暂时没有真实备考资料也能继续开发和功能验收，测试讲义足以验证上传、引用、删除等流程；它不能替代真实教材质量评测。后续再选几份实际使用的 PDF、DOCX、TXT 或 MD；PDF 应能提取文字，扫描图片需要先 OCR。多资料评测在提供真实资料后进行，不影响当前本地试用。

本轮重新运行评审的 36 题，使用真实 Chroma、m3e 嵌入与 BGE 重排：门槛 0.12 下，资料内通过 19/20，资料外误通过 0/10；部分覆盖 6 题单列。这只是检索门槛通过率，不是最终回答正确率。长文切分为 176 块，长度 97～800 字，无不足 20 字的碎块。详细结果位于 noim/evaluation-*/result.json。

评审原有 36 题已保存为 `tests/eval/questions.jsonl`，属于单文档诊断集；章节标签尚需人工核对补齐。至少三份真实备考资料、多领域资料内/外题、调参集与留出集评测仍待完成；目前不能宣称达到双方 90% 指标。登录、多用户、科目空间、试卷服务器持久化与页码级引用仍未实现。

---

# 当前版本说明：资料优先、对话隔离、会话管理（2026-10-08）

本节是最新行为说明；后面的旧阶段说明用于了解模块演进，冲突时以本节为准。

## 如何使用当前版本

1. 重启 Python 服务，然后浏览器 Ctrl+F5 刷新。
2. 点击“新对话”，为这次学习上传资料。新对话为空，不再引用旧默认知识库或测试手册。
3. 等资料显示“已就绪”，再提问。程序先检索，再让模型根据检索段落组织答案。
4. 没有资料、没有足够相关内容、或模型判断无法从资料回答时，页面会提示，并提供“仅此问题使用模型通用知识回答”。没有点击就不自动转通用知识。
5. 左侧每个对话有重命名、归档/恢复、删除。归档列表可通过“查看已归档”打开。

当前采用“一个对话一份独立资料空间”。同一文件上传到两个对话会分别管理；删除一份不影响另一份。科目空间复用和用户登录尚未实现，不能把对话隔离等同于账号权限隔离。出题也限定当前对话的资料和错题。

## 数据保存与删除

- 聊天消息、标题、归档状态以 `daima/data/sessions.db` 的 SQLite 为主存储；重启可保留，Redis 可选作为镜像。
- 初次连接可从旧 Redis 导入已有会话；之前只保存在已退出进程内存里的聊天无法恢复。
- 原本全局 default 知识库的文件不会自动带入新对话，也不会在升级时擅自删除；需要在目标对话重新上传所需文件。旧文件仍在原数据目录。
- 删除资料会删除上传副本、Chroma 中对应的文本块及向量、注册表记录，并立即清理内存快照和 BM25 磁盘缓存。向量删除会核验；失败会报错，不能假报成功。
- 删除资料不删除已经生成的聊天回答和错题。这些是独立业务记录；新问答不把旧回答/旧工具结果作为资料依据。
- 删除整个对话会一并删除其资料、聊天消息、会话元信息和来源错题；索引中的资料需等处理完成才能删除。删除是不可撤销操作，界面会明确提示。
- 上述属于应用层删除与不可检索验证，不保证数据库底层空闲页、WAL、系统备份或 SSD 中的字节级不可恢复；不是磁盘安全擦除工具。

## 代码与验证

- `core/document_chat.py`：资料优先新入口、相关性门槛、基于资料回答、通用知识确认。
- `core/engine.py`：保留会话锁、超时和回滚，默认调度新入口；旧工具自治流程保留为历史实现，不是默认流程。
- `rag/kb_manager.py`：会话资料分区、分区内去重、删除核验与缓存清理。
- `core/session.py`：SQLite 持久存储和可选 Redis 镜像，标题/归档操作。
- `api/app.py`：对话管理、带 session_id 的文档接口和 `needs_general` SSE 事件。
- `web/index.html`：当前对话资料、通用知识确认按钮、会话操作。

相关性门槛在 `retrieval.evidence_min_score`，默认 0.0，是 reranker 的原始分数，不是“零概率”。相关性筛选与模型的依据判定仍可能误判，后续需要固定题集评估；不能保证每份资料都能完整召回。显示的出处为文件名和文本块编号，不是 PDF 原页码。

新增验证脚本：`python -B noim/test_scopes.py`，覆盖 SQLite 重启读取、不同对话隔离、真实 Chroma 记录删除及缓存/文件核验、删除失败、资料先于模型、无依据确认、对话 API 重命名/归档/恢复/删除。测试数据全部放在 noim，使用固定小向量和模拟模型，不请求实际推理。未完成本次浏览器视觉验收。

## 2026-10-25 更新：追问补全、BM25 通道、锁与性能

- **追问补全**：资料优先流程此前每轮只把当前问题发给模型，代词追问（"它的授权审批呢？"）会丢失指代。现在有历史对话时，先用最近几轮把追问改写为独立完整问题，再用于检索和作答（`retrieval.context_rewrite`，默认开启，可关）。旧回答、旧工具结果仍不进入回答上下文，删除资料后的隔离语义不变；改写调用失败自动退回原问题。网页步骤条新增"补全"一步。通用知识轮会带上最近两轮对话。
- **默认检索补上 BM25 通道**：`document_chat` 的检索原先只走向量 top8 + 重排，现在并入 BM25 关键词通道（`retrieval.bm25_top_k` 生效），精确术语、编号类查询改善。知识库快照因此增加 metadatas，旧格式 BM25 缓存会在下次使用时自动重建一次。
- **检索不再长时间持全局锁**：嵌入与重排移出 KBManager 锁外，仅 Chroma 查询段与删除互斥。检索期间其他会话的上传进度、文档列表轮询不再被阻塞。
- **会话列表提速**：消息条数/轮次在保存时冗余进 `sessions:index`，列出会话不再全量解析每个会话的完整消息；旧索引项首次读取时惰性补齐。
- **旧工具流程检索范围修正**：`_ensure_rag` 原写死 `kb_id="default"`，与按会话隔离的资料空间不一致（该流程非默认入口，此前未爆发），现改为当前会话作用域。
- **测试**：`test_scopes.py` 新增追问补全专项用例（改写后的问题同时驱动检索与作答）；`test_local_changes.py` 中走 `chat()` 的用例改打桩新入口——此前它们已因 10-08 默认入口切换整体失配，现在 11 项全部恢复通过，跑法不变。

---

# 以下为前几个阶段的模块讲解及历史记录

> 2026-10-08 界面更新：聊天页已补上传/资料状态/删除；收录可取消；界面改为简体中文。下方旧阶段记录中的“上传入口未补齐”已被本次更新替代。

# one_project 项目使用与代码阅读说明

更新时间：2026-10-08。本文对应 `one_project/daima` 当前代码。

文件名按你的要求保留为 **readme.ma**，内容使用 Markdown 格式。它是现在的使用说明；`shuoming/完善与改造说明文档.md` 更接近历次改造计划和实施记录，里面早期的配置、勾选框可能已经过时。

## 1. 这个项目到底是做什么的？

这是一个运行在你电脑上的学习和知识问答助手。你可以上传教材、讲义、制度文件等资料，让它查资料后回答问题；把不理解的知识点收录成错题卡片，再按错题或主题生成练习题。

当前已有的主要能力：

| 能力 | 实际作用 | 当前情况 |
|---|---|---|
| 对话 | 像聊天助手一样回答问题，保留当前会话上下文 | 有命令行和网页两个入口 |
| 知识库问答 | 先从上传资料里找相关段落，再交给大模型组织答案 | 支持向量检索、关键词检索、重排 |
| 文档管理 | 上传、查看索引状态、删除资料 | 后端有接口，新网页的上传管理入口尚未补齐 |
| 多会话 | 将不同学习主题分成不同会话 | 有会话列表，网页删除入口尚未补齐 |
| 长时记忆 | 保存用户偏好或限制，之后注入提示词 | 读取已有记忆；自动提取默认关闭 |
| 错题本 | 保存问题、回答、混淆点、主题和掌握状态 | 可以手动收录；自动收录默认关闭 |
| 练习题 | 按主题或错题检索材料，生成并审核试卷 | 后端和页面已有主体功能，试卷下载入口待补 |
| 联网搜索 | 通过 Serper 获取外部搜索结果 | 默认关闭，需要密钥后启用 |

它并不是训练一个新模型，也不是把资料永久训练进 Qwen。当前方式叫 RAG：**问题来了以后，先找资料，再把找到的资料作为上下文交给模型**。

“多 Agent”在这个项目里主要表示角色分工：回答、检索审核、出题、审题、记忆提取等提示词/任务。它们可以使用同一个模型，不代表电脑上一定同时运行多个独立智能体进程。本次默认减少辅助角色调用，优先把本地问答跑稳。

## 2. 和原来的 多agent协作.py 有什么关系？

原来的 `agent/多agent协作.py` 把几乎所有事情写在一个文件里：创建模型客户端、加载文档、建立索引、记忆、工具、循环提问等。理解时可以从上往下读，但后面功能变多，修改和排错就容易相互影响。

现在不是把这套逻辑丢了，而是按职责拆到不同文件中。可以把 `core/engine.py` 看作原来 `main_loop()` 的主要接班人。

| 原单文件中的概念/函数 | 现在主要放在哪里 | 阅读时怎么理解 |
|---|---|---|
| `Anthropic(...)`、`client.messages.create(...)` | `daima/core/llm.py` | 所有大模型调用的公共入口 |
| 写死的模型名、token 上限、端口 | `daima/config.yaml` | 普通参数集中配置 |
| API key/base_url 读取 | `daima/core/credentials.py` | 决定读本地地址还是云端凭据 |
| `main_loop()` | `daima/core/engine.py` 的 `chat()` / `_chat_impl()` | 一轮问答的调度中心 |
| `input()` 循环、打印回答 | `daima/main.py` | 命令行外壳 |
| 原来没有的网页接口 | `daima/api/app.py` | 把网页请求转成 Python 调用 |
| 原来没有的网页 UI | `daima/web/index.html` | 用户看到的页面和按钮 |
| `generate_chunks()` | `rag/loader.py`、`chunker.py`、`kb_manager.py` | 读取、切块、入库分开处理 |
| `semantic_chunk()` / `my_chunk()` | `daima/rag/chunker.py` | 把长资料变成可检索的小段 |
| `cache_utils` / 写 Chroma / BM25 | `daima/rag/indexer.py`、`kb_manager.py` | 老固定资料流程与新上传流程 |
| `rag_retrive()` / `multi_retrive()` | `daima/rag/retriever.py` | 真正找资料和排序 |
| `query_rewrite()` | `daima/tools/knowledge_search.py` | 可选的拆题、同义查询 |
| `search_google_retry()` | `daima/tools/web_search.py` | 外部搜索工具 |
| `extract_and_entities()` / `load_memory()` | `daima/core/memory.py` | 用户偏好记忆 |
| `save_session()` / `load_session()` | `daima/core/session.py` | 会话历史存取 |
| `compress_context()` | `daima/core/context.py` | 长对话压缩和窗口控制 |
| `quchu()` | `engine.py` 的 `_strip_thinking()` | 不把思考块写进后续历史 |
| 反思 YES/NO | `engine.py` 的 `_reflect()` | 可选审核；本地默认不启用 |

**以后修改正式产品，以 `one_project/daima` 为准。** 旧脚本仍在原位置，这次没有把它同步改成第二套实现；避免后续两份代码越改越不一致。

## 3. 先跑起来：从哪里启动？

以下命令用 Windows PowerShell 执行。先切到代码目录：

```powershell
cd 'D:\python projects\agent\one_project\daima'
```

### 3.1 准备依赖

使用你运行项目的那个 Python 环境：

```powershell
python -m pip install -r requirements.txt
```

本次补齐了 `fastapi`、`uvicorn`、`httpx` 的依赖声明。已有环境不用每次重复安装。

### 3.2 确认 Ollama 和模型

```powershell
ollama list
```

默认模型是 `qwen3:8b`。本次检查时，你电脑上已经安装了它。如果以后换电脑且没有这个模型：

```powershell
ollama pull qwen3:8b
```

Ollama 应当运行在 `http://localhost:11434`。如果桌面程序没有启动服务，可以在单独的终端执行 `ollama serve`。服务已经运行时不要重复启动。

项目继续使用 Anthropic Messages 协议，通过 Ollama 的兼容接口调用本地模型；并非因为代码写着 Anthropic 就在请求云端。官方接口说明：[Ollama Anthropic compatibility](https://docs.ollama.com/api/anthropic-compatibility)。

### 3.3 启动网页版本

```powershell
python -B -m uvicorn api.app:app --host 127.0.0.1 --port 8010
```

浏览器打开：

- 聊天页面：http://127.0.0.1:8010/
- 接口文档：http://127.0.0.1:8010/docs
- 配置/状态检查：http://127.0.0.1:8010/health

`-B` 是不生成新的 Python 字节码缓存；不影响功能。启动后终端要保持运行，结束服务用 Ctrl+C。修改配置或代码后重启服务，已有客户端是单例，不会自动切换配置。

`/health` 会显示实际解析后的模型名、provider、地址和 Redis 持久化状态。**它不会请求模型做推理**；`llm_configured=true` 只能证明配置存在，`llm_reachable=not_checked` 表示没有探测连通性。是否能回答，要实际发一条消息。

网页还没补好的上传功能，可先通过 `/docs` 中 `POST /documents` 上传，然后用 `GET /documents` 等到状态为 `ready`。这比手工往 Chroma 数据目录复制文件更可靠。

### 3.4 命令行版本

```powershell
python -B main.py --check
python -B main.py
```

`--check` 只是配置检查，并不是完整模型推理测试。普通聊天不再因为旧的固定手册路径不存在就被阻止启动。

常用命令：

| 命令 | 作用 |
|---|---|
| `exit` | 退出 |
| `/new` | 清空当前默认会话，开始新的对话 |
| `/memory` | 查看保存的用户偏好 |
| `/mistakes` | 查看错题 |
| `/mistakes dedupe` | 合并相似错题，属于会修改错题数据的操作 |
| `python -B main.py --new` | 启动时清空默认会话 |

## 4. 最常改的地方：模型在哪里修改？

### 4.1 本地模型：首先改 config.yaml

```yaml
llm:
  provider: "ollama"
  base_url: "http://localhost:11434"
  model: "qwen3:8b"
  thinking: false
  retries: 1
  chat_timeout: 300
  max_tokens_answer: 4000
  max_tokens_util: 1200
  llm_timeout: 120
```

| 参数 | 含义 | 改它的场景 |
|---|---|---|
| `provider` | 运行模式，支持 `ollama` / `anthropic` | 切换本地与兼容云服务 |
| `base_url` | 本地 Ollama 服务根地址 | Ollama 在其他端口或机器上 |
| `model` | Ollama 中实际安装的模型标签 | 例如换成另一个 `ollama list` 中存在的模型 |
| `thinking` | 本地模式为 false 时向接口发送禁用思考选项 | 需要尝试推理模式时改 true；true 表示交给模型默认行为，并不设置思考预算 |
| `retries` | 一次调用总尝试次数，1 表示不重试 | 本地默认不要失败后反复等待 |
| `llm_timeout` | SDK 单次 HTTP 请求超时设置 | 首次加载慢时适当增加；它不是整轮总时限 |
| `chat_timeout` | 引擎一轮问答的等待预算 | 包含检索、辅助调用和回答；默认 300 秒 |
| `max_tokens_answer` | 正式回答的最大输出额度 | 回答被截断时可提高，不是输入上下文大小 |
| `max_tokens_util` | 拆解、总结、记忆等辅助任务输出额度 | 启用辅助任务后调整 |

例如只想换模型，改 `model` 一项，然后重启 Python 服务即可。工具问答要求新模型支持工具调用；能普通聊天不等于工具功能也可用。

**不要去每个 Python 文件里搜索 `qwen3:8b` 然后逐个替换。** 正式调用已经集中到 `core/llm.py`，它从配置读取名称。

### 4.2 本地配置的优先级

本地模式：

```text
模型：系统环境变量 OLLAMA_MODEL → daima/.env 的 OLLAMA_MODEL → config.yaml 的 llm.model
地址：系统环境变量 OLLAMA_BASE_URL → daima/.env 的 OLLAMA_BASE_URL → config.yaml 的 llm.base_url
```

没有特殊需求时，不必创建 `.env`。直接使用 `config.yaml` 最好理解。

本地模式不会读取旧云服务的 key/base_url，也不会使用旧 `LLM_MODEL` 覆盖本地模型。这样可以避免 Windows 凭据管理器里残留的云端地址把请求引到错误的服务。

本地接口使用 `ollama` 作为占位 key。回环地址 `localhost` / `127.0.0.1` / `::1` 的模型请求不走系统代理。

### 4.3 以后想切回云端

把 `llm.provider` 改为 `anthropic`，再设置云端模型和凭据。此模式只表示 **Anthropic Messages 协议兼容服务**，并不是所有任意厂商接口都能直接使用；只有 OpenAI Chat Completions 协议的服务不能靠改地址直接接入。

- 模型：系统环境变量/.env 的 `LLM_MODEL` 优先，再读 YAML 的 `llm.model`。
- key：Windows 凭据管理器中的 `MultiAgentApp/api_key` 优先，`ANTHROPIC_API_KEY` 兜底。
- 地址：Windows 凭据管理器中的 `MultiAgentApp/base_url` 优先，`ANTHROPIC_BASE_URL` 兜底；都没有则使用 Anthropic 官方地址。
- **云端模式不会使用 YAML 里的本地 `llm.base_url`。**
- 可以通过 `setup_credentials.py` 配置 Windows 凭据；也可以参考 `.env.example`。

### 4.4 项目实际有三种模型，不要混淆

| 配置位置 | 默认值 | 用途 |
|---|---|---|
| `llm.model` | `qwen3:8b` | 理解问题、决定调用工具、生成答案/练习题 |
| `embedding.model` | `moka-ai/m3e-base` | 把问题和文档编码成向量，方便搜索 |
| `rerank.model` | `BAAI/bge-reranker-large` | 对候选资料重新排序 |

换聊天模型通常不需要重建知识库。**换 embedding 模型则需要重建文档向量索引**，否则向量维度或语义空间可能不匹配。不能仅换配置后继续把新旧向量混用。

embedding/reranker 由 Sentence Transformers 加载，不是由 Ollama 加载；首次使用知识检索仍可能下载或加载模型。LLM 本地化也不自动代表整个程序完全离线：首次下载依赖/模型、以及主动开启联网搜索，仍可能需要网络。

## 5. 文件夹分别放什么？

```text
one_project/
├─ readme.ma                      ← 当前使用和阅读说明
├─ shuoming/
│  └─ 完善与改造说明文档.md        ← 历史计划、阶段记录、后续路线
├─ daima/                         ← 正式代码与重要数据
│  ├─ main.py                     ← 命令行入口
│  ├─ config.yaml                 ← 普通配置
│  ├─ requirements.txt            ← Python 依赖
│  ├─ .env.example                ← 可选环境变量模板
│  ├─ core/                       ← 问答引擎与公共能力
│  ├─ rag/                        ← 资料处理、索引和检索
│  ├─ tools/                      ← 模型可以请求的工具
│  ├─ api/app.py                  ← FastAPI 接口
│  ├─ web/index.html              ← 正在使用的网页
│  ├─ web/index_old.html          ← 旧页面参考
│  ├─ data/                       ← 上传原件、注册表、错题数据库
│  ├─ chroma_db/                  ← 文档向量数据库
│  └─ entity_memory.db            ← 用户长期记忆
└─ noim/                          ← 本次临时脚本、测试结果、日志和可重建缓存
   ├─ logs/agent.log
   ├─ cache/
   ├─ test_local_changes.py
   ├─ probe_local.py
   └─ integration_local.py
```

旧的 `_probe*`、`_mock*`、`_server.log` 等文件原本就存在于 `daima`，本次没有批量删除或搬走。它们不是正式入口；其中有些历史脚本可能写配置或凭据，不要因为名称像测试就随手执行。

`noim` 内本次创建的测试数据库、日志和脚本不承载你的正式业务资料，可以在测试/服务停止后删除。清掉缓存会导致后续重建或首次加载变慢，但不等于删除上传原件。

**不要把以下文件当作垃圾删除：** `data/uploads`、`data/kb_registry.json`、`data/mistakes.db`、`entity_memory.db`、`chroma_db`。它们分别涉及原始资料、注册信息、错题、记忆和索引。修改目录前先确认数据用途。

## 6. core：问答的主干

### core/engine.py：总调度

`AgentEngine` 是最值得先读的类。一个实例管理一个会话的消息列表、工具注册和问答过程。网页后端按 session_id 缓存引擎。

主要方法：

- `setup_tools()`：注册知识库检索，以及可选的联网搜索。当前工具很少，直接把 schema 提供给模型，不再为了选两个工具额外加载向量模型。
- `chat()`：对外入口。空问题检查、同一会话串行执行、整轮超时、失败/取消后回滚本轮消息。
- `_chat_impl()`：实际问答循环。
- `_execute_tools()`：检查参数，执行工具，并给每个调用附上对应的 tool_result。
- `_ensure_rag()`：第一次需要查资料时才初始化检索器。
- `_reflect()`：可选的检索结果审核。
- `_capture_mistake()`：可选的回答后自动收录。

修改“模型应当如何回答”主要看 `SYSTEM_PROMPT`。修改“先做什么再做什么、循环几轮”主要看 `_chat_impl()` 和 YAML 中相关开关。

### core/llm.py：统一模型入口

`call_llm()` 是同步版，`acall_llm()` 是异步版。所有正式模块通过这层发请求，因此改模型连接、请求参数、超时和重试，优先改这里。

此层负责：

1. 从 credentials/config 取实际模型和地址。
2. 创建同步/异步客户端，禁用 SDK 内部重试，避免和外层叠加。
3. 把历史里的 system 总结移到顶层 system 参数。
4. 本地轻量模式发送关闭思考选项。
5. 只对连接错误、限流和服务端错误等可恢复情况重试；认证/参数错误不盲目重试。
6. 记录每次调用耗时、stop_reason、输入输出 token。
7. `extract_text()` 只取正文 text 块，忽略 thinking/tool_use。

### core/config.py：配置读取

启动时读取 `daima/config.yaml`，提供 `config.get('llm.model')` 这种点号路径访问。`config.abspath()` 将相对路径转换成相对于 `daima` 的绝对路径，因此 `../noim/logs` 指向 `one_project/noim/logs`。

### core/credentials.py：凭据和连接选择

负责本地/云端模式分流、环境变量和 keyring 读取。它不执行问答。具体优先级见第 4 节。

### core/context.py：控制对话长度

对话过长时用模型总结旧内容；超出窗口时裁剪历史。本次增加按真实用户轮次边界裁剪，避免把 tool_use 和对应 tool_result 从中间拆开。

`compress_threshold_tokens` 是触发条件，并不等于严格的模型上下文上限。当前使用 `cl100k_base` 粗略估算，对 Qwen 不等于精确 token 数；为保全当前工具链，有时也会保留超过目标窗口的消息。超长单轮资料仍需要后续更精细的预算管理。

### core/memory.py：跨会话偏好

SQLite 中保存“偏好、限制”等实体。它与聊天记录不同：聊天记录保存你说过的话，长期记忆保存从话中提取的稳定信息。

本次新增 `aextract_from()`，异步等待模型，SQLite 写入仍在创建连接的线程完成。默认不自动提取，但已有记忆仍会读取并注入回答。

### core/session.py：聊天记录

保存会话索引、标题、消息、更新时间。现在仍然是 Redis 优先，连接不上时降级为进程内存。

**这次没有改成 SQLite 会话存储。** 因此 Redis 不可用时，当前进程里能切换会话，但重启服务后聊天历史会丢失。错题和长期记忆有自己的 SQLite，不随内存会话一起消失。

Redis 默认端口是 6380，注意不要和通常的 6379 混淆。下一阶段可以实现 SQLite 会话持久化，减少本地使用门槛。

### core/mistakes.py：错题本

保存问题、回答、混淆点、主题、掌握状态和来源会话，提供查询、删除、状态更新、去重和 Markdown 导出。`judge_exchange()` 使用模型判断/提炼知识薄弱点。

默认关闭自动收录后，手动“收录”功能仍然可用，它仍会调用模型。

### core/exam_builder.py：出题和审题

按错题或主题检索材料 → 调用模型出题 → 调用模型审核 → 拼装试卷 Markdown。它直接复用统一 LLM 模块，所以本次切换本地模型也会影响出题。

试卷是模型生成内容，当前没有做大规模题目准确率评测。现有部分引用 metadata 仍为空，不应把“有引用结构”理解成完整、可靠的逐页出处追踪。

### core/log.py：日志

统一控制台输出和滚动文件日志。现在会创建日志目录，默认写到 `one_project/noim/logs/agent.log`。排查耗时可搜索 `LLM model=` 和 `chat session=`。日志可能包含问题片段或业务状态，分享时注意其中的内容。

## 7. rag：资料如何变成可以搜索的知识库？

导入方向：

```text
上传 PDF/TXT/DOCX 等资料
  → api/app.py 接收上传
  → kb_manager.py 保存原件、登记文档
  → loader.py 读取内容
  → chunker.py 切成较小文本块
  → embedding 编码
  → Chroma 保存向量和文本
  → BM25/快照准备好
  → 文档状态 ready
```

查询方向：

```text
用户问题
  → tools/knowledge_search.py
  → rag/retriever.py
      ├─ 向量检索：找语义相近的段落
      └─ BM25：找关键词匹配的段落
  → 合并去重
  → reranker：按相关程度再排一次
  → 相关文本返回给大模型
  → 大模型组织答案
```

各文件职责：

| 文件 | 职责 |
|---|---|
| `loader.py` | TXT/Markdown 文本读取、PDF 转 Markdown、DOCX 段落和表格读取 |
| `chunker.py` | 根据标题和语义切分长文档 |
| `kb_manager.py` | 当前上传式知识库的核心：后台索引任务、文档注册表、向量集合、版本和快照 |
| `indexer.py` | 较早的固定源文件索引/缓存流程，主要用于旧 demo 和兼容代码 |
| `retriever.py` | `HybridRetriever` 混合检索和 `Reranker` 重排单例 |

`paths.source_docs` 保留给旧固定资料流程；正式上传知识库主要看 `kb_manager.py` 管理的数据。不要认为往 YAML 增加文件名就一定等价于通过上传接口导入。

## 8. tools：模型如何“调用函数”？

模型不会直接执行你的 Python。引擎先把工具名称、描述和参数结构告诉模型；模型返回 `tool_use`，引擎检查参数后代为执行，再把 `tool_result` 发回。

例如模型返回 `multi_retrieve(query='内部监督')`，引擎实际执行知识库检索，再把找出的段落交给模型。

- `knowledge_search.py`：知识库工具的外壳。默认直接用原问题检索；开启 `retrieval.rewrite_enabled` 才调用模型拆题、生成多个查询。
- `web_search.py`：通过 Serper 搜索外部网页摘要。与本地文档检索是两件事。

本次同时处理了未知/禁用工具、非对象参数和校验错误，错误会作为工具结果返回，而不会随意执行另一个函数。

## 9. api 和 web：前后端怎么连接？

`web/index.html` 是当前网页，HTML、CSS、JavaScript 都在里面。按钮事件通过 fetch 请求 `api/app.py`。后端找到对应会话的 `AgentEngine`，运行 Python 逻辑，再返回结果。

常用接口：

| 接口 | 作用 |
|---|---|
| `GET /health` | 配置和 Redis 状态，不做模型推理 |
| `POST /chat` | SSE 过程事件 + 最终答案 |
| `POST /chat/sync` | 等完整回答后返回 JSON，适合调试 |
| `GET/POST /sessions` | 列出/新建会话 |
| `GET /sessions/{sid}/messages` | 会话消息 |
| `DELETE /sessions/{sid}` | 删除会话 |
| `GET /memory`、`DELETE /memory/{key}` | 查看/删除记忆 |
| `POST/GET /documents`、`DELETE /documents/{doc_id}` | 上传/列表/删除资料 |
| `GET/POST /mistakes` | 列表/手动收录错题 |
| `PUT /mistakes/{mid}/status` | 改掌握状态 |
| `DELETE /mistakes/{mid}` | 删除错题 |
| `GET /mistakes/export` | 下载 Markdown 错题本 |
| `POST /exam/from-topic` | 按主题出题 |
| `POST /exam/from-mistakes` | 按错题出题 |

完整入参以运行中的 `/docs` 为准。

**当前 SSE 不等于答案逐字生成。** 页面会先收到 `status` 进度事件，等完整答案生成后收到 `answer`，最后收到 `done`。本次没有加入 token 级流式输出，也没有补停止按钮。

## 10. 一次完整问答到底经过哪些文件？

以“根据我的资料解释内部监督”为例：

1. 网页收集问题和 session_id，发到 `/chat`。
2. `api/app.py` 取得该会话引擎。
3. `engine.chat()` 对同一会话排队，保存本轮开始前的消息快照。
4. 读取已有长期记忆；只有开关开启才额外调用模型提取新记忆。
5. 添加本轮用户问题，必要时总结旧上下文。
6. `llm.acall_llm()` 向本地 Ollama 请求回复，附带知识检索工具描述。
7. 如果模型直接给正文，保存并返回；如果请求工具，进入下一步。
8. 引擎校验参数，在工作线程中运行知识检索，避免直接阻塞 Web 事件循环。
9. 检索结果以匹配 tool_use_id 的 tool_result 放入消息历史。
10. 再请求模型根据资料回答。默认最多执行两轮工具，随后不再提供 tools。
11. 得到完整正文后保存会话，再由 API 发给网页。

普通直接回答常常只需要一次模型调用；一次资料问答通常包含“请求工具”和“根据结果作答”两次调用，但如果模型再次检索、历史需要压缩，调用次数会增加。这里不是固定次数保证。

## 11. 本次改了什么？

- 默认从云端 `step-5-preview` 改为本地 `qwen3:8b`。
- 本地和云端配置分开，避免旧凭据意外覆盖本地地址。
- 本地默认禁用思考，并设置单次超时和整轮问答预算。
- 默认关闭自动记忆提取、查询改写、反思、自动错题收录、联网搜索；相关能力保留配置开关。
- 移除工具选择时额外的向量库依赖，简单对话启动更轻。
- 修复工具结果等待、上下文总结和可选记忆提取中的同步阻塞路径。
- 工具调用优先按是否存在 tool_use 判断，不再仅凭最后一个块的类型。
- 空内容、纯思考无正文、输出截断都有明确报错。
- 工具预算耗尽后不再传 tools；不配合的模型会被终止本轮。
- 同一会话串行问答；出错、超时、取消会回滚未完成的消息。
- 压缩时保持工具调用/回传的轮次边界，并正确传递 system 总结。
- 修复辅助查询中误丢编号列表的处理，并限制子问题/查询数量。
- `/health` 报告实际解析的模型配置，且不再为了检查健康状态初始化引擎。
- 补齐 Web 依赖；新日志、可重建缓存和临时验证文件放进 `noim`。

异步超时可以停止等待结果，但无法强制杀掉已经开始运行的 Python 工作线程，也不能保证 Ollama/底层模型立刻停止计算。多会话同时进行大型检索的显存/内存调度仍可继续优化。

## 12. 想恢复“完整辅助流程”怎么改？

在 `config.yaml` 中逐项启用，不建议第一次就全部打开：

```yaml
memory:
  auto_extract: true
retrieval:
  rewrite_enabled: true
reflection:
  enabled: true
  max_rounds: 2
mistakes:
  auto_capture: true
tools:
  web_search_enabled: true
```

这些片段是对应现有分组中的字段，不要直接在 YAML 末尾重复创建同名分组。联网还需要配置 Serper 密钥。开启后耗时会增加，应对照日志看收益。默认的工具总轮数上限依然有效，不会因为开启反思就无限搜索。

## 13. 建议的阅读顺序

1. 先读本文第 1～4 节，能运行并知道模型在哪改。
2. 读 `main.py`，看一轮用户输入怎么传给引擎。
3. 读 `core/engine.py` 的 `chat()` 和 `_chat_impl()`，先掌握主线。
4. 读 `core/llm.py`，理解统一模型调用和响应提取。
5. 读 `tools/knowledge_search.py` → `rag/retriever.py`，理解如何查资料。
6. 需要理解上传时，再读 `kb_manager.py`、`loader.py`、`chunker.py`。
7. 需要改页面时，再读 `api/app.py` 和 `web/index.html`。
8. 最后看记忆、上下文、错题和出题模块。

不用先把所有文件读完。先沿着“一条问题进来，答案怎么出去”追一遍，再按功能展开。

## 14. 常见问题与下一阶段

**改了 YAML，为什么模型没变？**

先重启服务，再看 `/health`。本地检查 `OLLAMA_MODEL` / `OLLAMA_BASE_URL`，云端检查 `LLM_MODEL` 和 Windows 凭据管理器。环境覆盖项可能比 YAML 优先。

**连接不上 localhost:11434？**

确认 Ollama 正在运行、地址端口正确。`ollama list` 能帮助确认本地服务与模型情况。项目不会自动下载模型或自动启动 Ollama。

**第一次查资料很慢？**

除 Qwen 外，还需要加载 embedding 和 reranker。默认 reranker 较大；先看日志卡在加载、检索还是 LLM，不能只凭页面等待时间认定是聊天模型慢。

**模型报 max_tokens？**

当前会把被截断的回复视作未完成，不会当作完整答案保存。可缩小问题、关闭思考或适当增加 token 额度，再重新发送。

**为什么重启聊天没了，但错题还在？**

会话可能走了内存降级，而错题走 SQLite。看 `/health` 的 `redis_persistent`。本次没有改变这个存储架构。

**现在该不该继续照 shuoming 加功能？**

下一阶段建议依次做：SQLite 会话持久化 → 真正逐字流式与停止 → 上传/记忆等前端入口 → 完整引用溯源 → 固定题集的回答与出题质量评测。当前代码仍是个人本地学习助手，不是已经验收完毕的多用户部署产品。

## 15. 本次验证记录（2026-10-08）

以下是本机这一轮验证的结果，不是所有模型/所有文档的性能保证。

| 验证 | 结果 |
|---|---|
| Python 源文件语法解析 | 通过 |
| 11 项隔离回归测试 | 全部通过：普通回答、空响应回滚、混合工具/文本、预算、输出截断、超时、会话串行、system 摘要、工具边界、禁用工具、错误参数 |
| 真实 `qwen3:8b` JSON 输出 | 通过，返回可解析 JSON |
| 真实工具调用和 tool_result 回传 | 通过，模型读取工具返回的测试暗号并回答 |
| FastAPI `/health` | 返回实际模型 qwen3:8b 和本地地址；不执行模型推理 |
| FastAPI `/chat/sync` + 本地模型 | 通过，测试短句约 1.8 秒 |
| FastAPI `/chat` + 现有知识库 + 本地模型 | 通过，实际检索 701 个 chunk 的知识库，候选重排后输出回答及 SSE 事件 |
| 首次知识库问答耗时 | 约 63.7 秒，包含 Sentence Transformers/embedding/reranker 的首次加载；本轮两次 LLM 调用约 0.47 秒和 2.32 秒 |

集成验证通过 ASGI 客户端调用真实 FastAPI 路由，实际请求本机 Ollama 并查询现有知识库；测试会话使用独立内存存储，测试记忆/错题数据库放在 `noim`，没有把测试对话写入正式会话历史。

这次没有逐按钮做浏览器操作验收，没有测试云端服务，也没有对生成的答案、错题和试卷做系统性的正确率评测。集成问答成功表示调用链通了，不表示检索内容一定全面或答案质量已达标。

可复跑的验证命令（在 one_project 目录）：

```powershell
python -B .\noim\test_local_changes.py
python -B .\noim\probe_local.py
python -B .\noim\integration_local.py
```

第一个使用模拟模型、不发推理请求；后两个使用真实本地 Ollama。集成脚本设置 Hugging Face 离线模式，依赖已经下载的 embedding/reranker。`noim/edit_local.py` 与 `edit_followup.py` 是本次一次性编辑辅助文件，不要重复执行；它们不是运行项目的入口。


## 16. 界面补全：上传、取消收录、简体中文（2026-10-08）

聊天页顶部现在有“学习资料 · 上传与管理”。点击“上传文件”，可多选 TXT、MD、PDF、DOCX；上传后自动刷新排队/解析/向量化进度，显示“已就绪”才可以查资料。不用再去 /docs 上传。支持资料删除，索引处理中暂不可删除。

每条回答的“收录错题本”对应被点击的问答，不再总是收录最后一轮。收录后按钮变为“已收录 · 点击取消”，再次点击会删除该问答的收录卡片；错题本页面也有“取消收录”。刷新时从服务器恢复收录状态。若旧问答已被上下文压缩移除，服务器会要求刷新，避免收错内容。

界面固定文案统一为简体中文，字体优先使用微软雅黑，取消远程字体加载；模型提示词也要求默认使用简体中文。已有历史回答和用户上传原文不会被批量转换或重写。

修改后重启 uvicorn，并在浏览器按 Ctrl+F5。若 Redis 不可用，重启会丢失当前内存聊天，错题数据库与上传资料保留。

验证：JavaScript/Python 语法检查通过；隔离 API 测试覆盖指定问答收录、防重复、取消、旧接口兼容、无效问答拒绝，以及 multipart 上传和状态接口契约（模拟索引器，不修改正式资料）。浏览器预览两次因自动审批超时未完成，本次未完成实际视觉/文件选择器验收。
