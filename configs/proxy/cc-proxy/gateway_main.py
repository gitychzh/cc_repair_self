#!/usr/bin/env python3
"""Direct entry point for the cc-role gateway proxy — used by Docker CMD.

R31.5: cc-proxy is physically isolated (own image, own code dir). This serves
only Claude Code (Anthropic /v1/messages → glm5.2 v×k cycling).
"""
from gateway.app import main

main()
