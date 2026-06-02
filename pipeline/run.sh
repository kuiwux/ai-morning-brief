#!/bin/bash
export https_proxy=http://172.23.80.1:7890
export http_proxy=http://172.23.80.1:7890
cd /tmp/ai_morning_brief
source ~/.hermes/.env
source /tmp/ai_morning_brief/.env
python pipeline.py
