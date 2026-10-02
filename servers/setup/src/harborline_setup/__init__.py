"""One-shot setup for the Harborline (fictional) MCP servers.

Runs as the database owner. It creates each server's role and schema, migrates it and
seeds it when empty, so the servers themselves only ever connect as their own
least-privilege role. Every step is safe to repeat.
"""
