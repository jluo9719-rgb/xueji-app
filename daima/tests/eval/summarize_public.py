"""把已完成的公开评测转成可读报告；不会把失败运行或字符串命中当成准确率。"""
import argparse
from collections import Counter
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT.parent / 'noim' / 'public-eval'


def fraction(n, d):
    return f'{n}/{d}（{n/d:.1%}）' if d else '未测'


def summarize(path):
    report = json.loads(path.read_text(encoding='utf-8'))
    if report['status'] != 'complete':
        raise ValueError('该次运行未完成，不能出具通过报告')
    lines = ['# 公开资料评测结果', '', f'原始结果：`{path}`', '',
             '数据：百度 DuReader Checklist 的固定子集，120 份原文、120 道上游标注题。',
             '官方 train/dev 分别作为调参/留出，每组有答案、无答案各 30 题；原文与题目跨组去重。', '',
             '## 候选相关性门槛', '',
             '| 门槛 | 分组 | 有答案题通过 | 无答案题拦截 |',
             '|---|---|---:|---:|']
    for label, groups in [('当前 0.12', report['existing_threshold']),
                          (f"调参选择 {report['selected_threshold']:.8f}", {k: report[k] for k in ('calibration', 'holdout')})]:
        for split, r in groups.items():
            lines.append(f"| {label} | {split} | {fraction(r['in_pass'],r['in_total'])} | {fraction(r['out_total']-r['out_false_pass'],r['out_total'])} |")
    feasible = report['calibration']['in_pass_rate'] >= .9 and report['calibration']['out_block_rate'] >= .9
    lines += ['', '调参集能否仅用一个门槛同时达到两项 90%：' + ('能' if feasible else '**不能**') + '。',
              '候选值没有自动写入产品；相关性不等于可回答性。无答案标注只针对给定原文，故这部分每题单独资料空间。', '',
              '## 多文档召回（未应用门槛）', '',
              '| 分组 | 原文同时入库数量 | 目标文档进入前 5 | 标准答案在前 5 块覆盖 |',
              '|---|---:|---:|---:|']
    for split in ('calibration', 'holdout'):
        rows = [r for r in report['mixed_retrieval'] if r['split'] == split]
        lines.append(f"| {split} | 30 | {fraction(sum(r['document_hit_at_5'] for r in rows),len(rows))} | {fraction(sum(r['span_hit_at_5'] for r in rows),len(rows))} |")
    lines += ['', '## 真实模型回答（当前门槛 0.12）', '',
              '以下只统计实际运行过的问答。拒答按程序模式 `no_evidence` 统计，不把仅含缺失说明的回答自动算成拒答。', '',
              '| 分组 | 上游标签 | 实测数 | no_evidence | grounded | unverified | error |',
              '|---|---|---:|---:|---:|---:|---:|']
    for split in ('calibration', 'holdout'):
        for expected in ('answer', 'refuse'):
            rows = [r for r in report['answers'] if r['split'] == split and r['expected'] == expected]
            counts = Counter(r['mode'] for r in rows)
            lines.append(f"| {split} | {expected} | {len(rows)} | {counts['no_evidence']} | {counts['grounded']} | {counts['unverified']} | {counts['error']} |")
    lines += ['', '标准答案字符串检查不是正确率；正文、原文与原始标签须人工核对。',
              '样本中的“may是数字几”标为无答案，而原文含 May 与五月的对应，存在标注解释歧义。保留原始成绩，不据此静默改标签或剔除失败题。', '',
              '## 复现与限制', '',
              f"- 数据包 SHA256：`{report['archive_sha256']}`",
              f"- 问题清单 SHA256：`{report['questions_sha256']}`",
              f"- 嵌入：`{report['embedding']}`；重排：`{report['reranker']}`；模型配置：`{report['llm']}`。",
              f"- 切分版本：`{report['chunk_version']}`。",
              f"- 清理：剩余向量 {report['cleanup']['remaining_vectors']}，清理失败 {len(report['cleanup']['failures'])} 项。"]
    lines += ['- ' + limit for limit in report['limitations']]
    lines += ['', '补充的换表达、部分覆盖、资料外及出题诊断单独保存于 `diagnostic-*/result.json`，不能与独立留出成绩混合。', '']
    output = path.with_name('report.md')
    output.write_text('\n'.join(lines), encoding='utf-8')
    print(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result', type=Path)
    summarize(parser.parse_args().result)
