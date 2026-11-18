"""一条命令运行离线回归测试；每组测试使用独立数据目录。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
NOIM = ROOT.parent / 'noim'


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    NOIM.mkdir(exist_ok=True)
    sandbox = Path(tempfile.mkdtemp(prefix='regression-', dir=NOIM)).resolve()
    results = []
    try:
        for name in ('test_local_changes.py', 'test_scopes.py', 'test_ui_api.py', 'test_reliability.py', 'test_evaluation.py'):
            target = sandbox / Path(name).stem
            target.mkdir()
            env = dict(os.environ, XUEJI_DATA_DIR=str(target), PYTHONDONTWRITEBYTECODE='1',
                       PYTHONIOENCODING='utf-8', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
            started = time.perf_counter()
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'tests' / name)],
                                    cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=180)
            results.append({'test': name, 'exit_code': result.returncode,
                            'seconds': round(time.perf_counter()-started, 2),
                            'stdout': result.stdout, 'stderr': result.stderr})
            print(f"{name}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
            if result.returncode:
                print(result.stdout[-6000:] + result.stderr[-6000:])
    finally:
        # 子进程退出后才清理 Chroma/SQLite 句柄；路径必须位于 noim。
        if sandbox.parent != NOIM.resolve():
            raise RuntimeError('拒绝清理测试目录外路径')
        shutil.rmtree(sandbox)
        (NOIM/'regression-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if len(results) == 5 and all(r['exit_code']==0 for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
