# 资料优先问答评测

这里的脚本只写 `one_project/noim`，不会把公开数据导入正式资料库，也不会修改正式模型配置。

## 公开数据来源与复现

本轮使用百度官方 [DuReader Checklist](https://github.com/baidu/DuReader/tree/master/DuReader-Checklist)。官方任务包含给定篇章的有答案/无答案判断。原始网页内容和标注属于第三方评测材料，不是本项目编写的教材，也不代表事实经过本项目核验。原件、提取文档和模型输出均为可删除测试产物。

下载入口来自官方 `download.sh`，只下载数据，不执行脚本、不下载其基线模型：

```text
https://dataset-bj.cdn.bcebos.com/lic2021/dureader_checklist.dataset.tar.gz
SHA256 d51553cfe1e025f8706e40417da065cbd097240ab2bf99077744489ada0e0ede
```

PowerShell 中进入 `one_project/daima` 后执行：

```powershell
python -B tests/eval/prepare_public.py
python -B tests/eval/public_benchmark.py --answers-per-class 6
python -B tests/eval/public_diagnostics.py
python -B tests/eval/public_exam.py
```

需要已缓存的 m3e、BGE 模型及运行中的本地 Ollama。标准评测跑真实 Chroma、嵌入、BM25 与重排，不用随机向量或模型 mock。`--answers-per-class 6` 在每个分组的每个类别中各取 6 题，共 24 题做真实问答；可改成 30 跑完全部 120 题。必须同时报告实际问答样本数量，不把 120 题检索成绩当成 120 题回答成绩。

## 分组与标签

- 官方 train 用作 calibration，官方 dev 用作 holdout。每组按原文 SHA256 固定顺序选取 30 道有答案题和 30 道无答案题，原文长度限制为 200–5000 字。
- 两组原文哈希与问题文本去重。原文相同但提问不同的样本也不会跨组。没有保证所有近似主题完全不同，也不知道基础模型训练时是否见过公开题。
- 标题、问题、标准答案都不会混进索引；只索引官方 context。标题可能含答案，加入标题会改变官方“正文无答案”的语义。
- 无答案标签只对原文篇章成立，因此有/无答案判定测试使用一篇一库；将 30 篇有答案资料放进同一个库的多文档召回另测，避免其他资料意外包含答案造成假标签。
- `manifest.json` 记录每篇原件哈希、网页标题、路径和上游分组；`questions.jsonl` 保留原始问题 ID、标签、标准答案和应命中的文档。
- `diagnostic_cases.json` 是根据三篇调参资料编写的 18 题补充诊断：换表达、部分覆盖、相近领域资料外、无关资料外。其结果单列，不计入公开留出集，也不用于选门槛。缺失实体会先用全文搜索检查。

## 指标的含义

1. **检索召回**：多文档测试看目标文档是否进入前 5。标准答案覆盖率额外检查前 5 块能否包含答案；按同一原文块的顺序拼接，支持答案跨块，禁止跨文档拼接。它仍是字符串检查，不能代替语义判断。
2. **门槛**：只在 calibration 上扫描，在资料外误放不超过 10% 的约束下尽量保留有答案题。同样通过数时更少误放优先。固定选定值后只报告 holdout；不自动写入 `config.yaml`。
3. **最终问答**：走真实 `AgentEngine.chat`，保留回答、模式、耗时和异常。`gold_substring_hit` 仅指正文是否包含标准答案字符串，不是回答正确率。长答案复述、同义词可能导致漏记，包含答案但夹带错误也可能误记，必须结合原文人工复核。
4. **部分覆盖**：人工核对是否回答有依据的部分、说明缺失部分、是否凭空补全。不能仅凭高相关分数通过。
5. **出题**：三份公开资料各请求三题，记录结构/引句校验、审题结果及拒绝原因。模型审题通过不等于人工确认准确。

`public_exam.py` 直接给出原文单独检查生成、原文引句匹配和审题，保存模型原始输出以便定位问题；它不运行检索，不能计入检索或浏览器通过率。引句只归一化中英文标点、引号和空白，不忽略字词、数字、小数点或运算符。

## 可回答性核对的实验结论

正式配置 `retrieval.answerability_check: false`。默认仍先检索，再用更明确的回答指令判断资料范围；这不是回答正确率保证。实验开关打开时，会额外调用本地模型核对所问事实和原文事实，并验证直接引句确实存在；核对失败进入资料不足提示，未经同意不走通用知识。

本轮对照发现该实验虽然减少了部分答非所问，却把 12 道原始有答案题中的 4 道也拒绝了；人工补充的 6 道部分覆盖题全部被拒。故未作为默认上线。相关原始结果保存在 `diagnostic-20261117-202020-c39144/result.json`，不能只报告无答案题拦截增加。

复测时可以使用：

```powershell
python -B tests/eval/public_diagnostics.py --answerability-check enabled --baseline ../noim/public-eval/run-20261117-200044-807455/result.json
```

这是根据已有错误进行的回归对照，不是新的独立留出评测；不同运行的模型随机性也可能影响结果。正式配置不自动跟随实验开关变化。

一次留出集结果被查看后，后续根据其失败样本进行修改，就把它视为回归集；下一轮验收应换新的未见资料和问题，不能反复宣称“独立留出”。这里不是 DuReader 官方排行榜评测，也不证明真实考试教材达到 90% 正确率。

## 产物与故障

`noim/public-eval/run-*/result.json` 保存每题结果、门槛扫描、运行配置和清理状态。脚本结束后删除测试索引中的原件与向量，原始公开数据、可复现输入与报告保留在 noim。失败运行也保留，不能当作通过；`status`、`cleanup.remaining_vectors` 和 `cleanup.failures` 必须一起核对。

`noim/public-eval/diagnostic-*/result.json` 保存补充诊断与实际出题输出。

旧的 `run.py` 和 `questions.jsonl` 继续用于上一阶段那份 36 题单文档诊断，不与这轮公开评测混报。
