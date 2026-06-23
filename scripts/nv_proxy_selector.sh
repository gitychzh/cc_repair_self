#!/bin/bash
# Wrapper for nv_proxy_selector.py
# Tests all US proxy nodes against NV API, ranks by latency, assigns top5 to K1-K5
python3 "$(dirname "$0")/nv_proxy_selector.py" "$@"
