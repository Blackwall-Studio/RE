# 🔄 RE Toolkit

**Reverse Engineering & AI Security Workbench**

A zero-friction Windows toolkit bundling two powerful tools: **Synapse** — an AI-powered reverse engineering workbench with a galaxy-themed web UI — and **HexStrike AI** — a Model Context Protocol (MCP) penetration-testing framework with a native control panel.

## Tools

### Synapse

An AI research lab and "second brain" workspace built with FastAPI + uvicorn. Configure multiple AI provider keys (ZenMux, Freebuff, Colibri) and access a galaxy-themed web UI for research, analysis, and collaborative reasoning.

```bat
cd synapse
SETUP.bat
START.bat
```

### HexStrike AI

An AI-driven penetration-testing MCP framework with a built-in control panel and web dashboard. Automates security tool orchestration with one-click scans.

```bat
cd hexstrike-ai-master
SETUP.bat
powershell -ExecutionPolicy Bypass -File Install-Tools.ps1
HexStrikeControl.exe
```

Control page: http://127.0.0.1:8888/

## Features

- **Zero-Friction Setup** — Everything is path-independent; unzip anywhere and run SETUP.bat
- **25+ Security Tools** — Auto-downloaded and PATH-configured via HexStrike install script
- **AI-Integrated** — Both tools support multiple LLM providers for AI-assisted workflows
- **Native Control Panel** — HexStrike ships with a .NET GUI for server management
- **Web Dashboards** — Both Synapse and HexStrike include browser-based interfaces

## Requirements

- Windows 10/11
- Python 3.10+
- Everything else installs automatically via the included SETUP.bat scripts

## License

MIT

---

*Built by Blackwall Studio.*
