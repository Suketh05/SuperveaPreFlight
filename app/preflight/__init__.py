"""Supervea Preflight Decision Layer.

A thin, deterministic decision layer that sits above existing AI gateways
and routers (OpenRouter, Kong, LiteLLM) and selects the best execution
route before handing off to the existing Supervea optimization layer.

This module is designed to be a self-contained plugin:
- It can be feature-flagged off entirely (SUPERVEA_PREFLIGHT_ENABLED).
- It fails open: any internal error falls back to existing routing.
- It never proxies customer traffic itself.
"""
