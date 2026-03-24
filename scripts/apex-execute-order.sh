#!/bin/bash
# Thin wrapper — delegates entirely to the Python order executor.
# All API calls, locking, and state management happen in Python.
exec /home/ubuntu/bin/python3 /home/ubuntu/.picoclaw/scripts/apex_order_executor.py "$@"
