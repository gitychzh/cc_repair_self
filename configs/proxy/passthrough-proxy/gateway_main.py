#!/usr/bin/env python3
"""Direct entry point for gateway proxy — used by Docker CMD.

Runs the modular gateway package as a standalone application.
"""
from gateway.app import main

main()