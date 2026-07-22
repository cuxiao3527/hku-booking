#!/bin/bash
# 港大预约系统 - 开发启动脚本
# 使用系统 Python 3.9（已有全部依赖，无需装包）
export PYTHONPATH="/Users/cuixiao/Library/Python/3.9/lib/python/site-packages:$PYTHONPATH"
cd "$(dirname "$0")/backend"
PYTHONPATH="$PYTHONPATH" /Library/Developer/CommandLineTools/usr/bin/python3.9 -c "
import sys
sys.path.insert(0, '/Users/cuixiao/Library/Python/3.9/lib/python/site-packages')
from main import app
import uvicorn
# 端口 8888（macOS 的 5353 被 mDNS 占用）
uvicorn.run(app, host='127.0.0.1', port=8888, log_level='info')
"
