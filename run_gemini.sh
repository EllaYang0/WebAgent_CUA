#!/bin/bash

# 设置 Google API Key（如果没有设置环境变量的话）
# export GOOGLE_API_KEY="your-google-api-key"

# 检查 API Key
if [ -z "$GOOGLE_API_KEY" ]; then
    echo "Error: GOOGLE_API_KEY 环境变量未设置"
    echo "请运行: export GOOGLE_API_KEY='your-key'"
    exit 1
fi

# 运行推理
cd /scr/rucnyz/projects/yefei_yang_web/WebAgent/NestBrowse
python infer_async_nestbrowse.py