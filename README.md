# Monitor PC

Monitor PC is a small Windows desktop dashboard for monitoring Linux/Raspberry devices and Windows servers over SSH.

It shows:

- online/offline state
- CPU load
- memory usage
- disk usage
- temperature when available
- top CPU and memory processes
- selected service states
- local CSV history

## Requirements

- Windows
- Python 3.10 or newer
- OpenSSH Client enabled on Windows
- SSH access to the devices you want to monitor

No external Python package is required to run the app. PyInstaller is only needed if you want to build an `.exe`.

## Run From Python

Open:

```bat
Avvia Monitor PC Python.cmd
```

Or run manually:

```bat
python monitor_pc.py
```

## Configuration

On first run, the app creates `monitor_pc_config.json`.

You can also copy:

```text
monitor_pc_config.example.json
```

to:

```text
monitor_pc_config.json
```

Then edit the devices:

```json
{
  "name": "Windows server",
  "host": "windows-hostname-or-ip",
  "user": "your-windows-user",
  "port": 22,
  "platform": "windows"
}
```

Supported platforms:

- `linux`
- `windows`
- `auto`

## Build EXE

Install PyInstaller:

```bat
python -m pip install pyinstaller
```

Then open:

```bat
build_exe.cmd
```

The executable will be created at:

```text
dist\MonitorPC.exe
```

## Notes

- Linux/Raspberry monitoring uses standard shell commands such as `free`, `df`, `ps`, and `systemctl`.
- Windows monitoring uses PowerShell over SSH.
- If a Windows check fails, the app may write `ultimo_controllo_debug.txt` next to the app.
- Local files such as `monitor_pc_config.json`, history CSV, debug logs, and build output are ignored by Git.
