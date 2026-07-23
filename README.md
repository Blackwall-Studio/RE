# RE — HexStrike AI + Synapse bundle

Two AI-driven tools, packaged for zero-friction setup on Windows 10/11.

## HexStrike AI (MCP security toolkit)

AI-powered penetration-testing MCP framework (upstream: [0x4m4/hexstrike-ai](https://github.com/0x4m4/hexstrike-ai), MIT) + a native control panel + a built-in control web page.

```bat
cd hexstrike-ai-master\hexstrike-ai-master
SETUP.bat                                          :: builds python env
powershell -ExecutionPolicy Bypass -File Install-Tools.ps1   :: downloads 25+ security tools + PATH
HexStrikeControl.exe                               :: start/stop/port picker
```

- Control page: <http://127.0.0.1:8888/> (target box + one-click scans, tools grid, process list)
- Optional GUI: `HexStrikeControl.exe` (start/stop server, open control page, pick port)
- `HexStrikeControl.cs` is the source of the GUI (compiles with stock .NET csc, no deps)

## Synapse (council + RE workbench + second brain)

FastAPI + uvicorn server with a galaxy-themed web UI.

```bat
cd synapse
SETUP.bat     :: builds python env + creates .env from template
START.bat     :: or START.bat 8277 to pick a port
```

- UI: <http://127.0.0.1:8000> (or your chosen port)
- Configure `.env` with your own ZenMux / Freebuff / Colibri keys (see `.env.example`)

## Requirements

- Windows 10/11, Python 3.10+
- Everything else installs via the included SETUP.bat scripts

## Notes

- No secrets in this repo — `.env` files are gitignored; use the provided `.env.example` templates.
- `SHIP-TO-TESTERS.txt`-style flow: everything is path-independent, unzip anywhere.
