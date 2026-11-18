"""评测本身的边界：留出题不能参与选值，误放优先，错误答案不算命中。"""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('public_benchmark', Path(__file__).parent / 'eval' / 'public_benchmark.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def row(expected, score, split='calibration'):
    return dict(expected=expected, top_score=score, split=split, span_hit_at_5=expected == 'answer')


class EvaluationTests(unittest.TestCase):
    def test_cross_chunk_answers_do_not_cross_documents(self):
        parts = [{'doc_id': 'a', 'chunk': 2, 'text': '第二部分。'},
                 {'doc_id': 'a', 'chunk': 1, 'text': '第一部分。'}]
        self.assertTrue(evaluation.answer_covered(['第一部分。第二部分。'], parts))
        parts[0]['doc_id'] = 'b'
        self.assertFalse(evaluation.answer_covered(['第一部分。第二部分。'], parts))
        self.assertFalse(evaluation.answer_covered([''], parts))

    def test_holdout_cannot_choose_threshold(self):
        with self.assertRaises(ValueError):
            evaluation.calibrate([row('answer', .8, 'holdout')])

    def test_threshold_rejects_false_positives_before_preserving_recall(self):
        rows = [row('answer', .7), row('answer', .9)] + [row('refuse', .8) for _ in range(10)]
        best, _ = evaluation.calibrate(rows)
        self.assertEqual(best['out_false_pass'], 0)
        self.assertEqual(best['in_pass'], 1)
        self.assertGreater(best['threshold'], .8)
        holdout = evaluation.rates([row('answer', .85, 'holdout'), row('refuse', .95, 'holdout')], best['threshold'])
        self.assertEqual(holdout['out_false_pass'], 1)

    def test_metrics_do_not_hide_bad_results(self):
        result = evaluation.rates([row('answer', .01), row('refuse', .9)], .12)
        self.assertEqual(result['in_pass_rate'], 0)
        self.assertEqual(result['out_block_rate'], 0)
        self.assertEqual(result['span_hit_rate'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
