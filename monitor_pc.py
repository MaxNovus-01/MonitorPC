import csv
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "monitor_pc_config.json"
HISTORY_PATH = APP_DIR / "monitor_pc_history.csv"
DEBUG_PATH = APP_DIR / "ultimo_controllo_debug.txt"
ICON_PATH = APP_DIR / "assets" / "MonitorPC.ico"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


COLORS = {
    "bg": "#26292e",
    "panel": "#1f2227",
    "panel2": "#2c3036",
    "input": "#181b1f",
    "border": "#4b525c",
    "text": "#eef1f5",
    "muted": "#a9b0ba",
    "blue": "#50a0ff",
    "green": "#4ade80",
    "red": "#f87171",
    "yellow": "#fbbf24",
    "cyan": "#22d3ee",
    "violet": "#a78bfa",
    "orange": "#fb923c",
}


DEFAULT_CONFIG = {
    "refresh_seconds": 5,
    "heavy_check_seconds": 60,
    "temp_alert": 75,
    "memory_alert": 85,
    "disk_alert": 90,
    "linux_services": ["ssh", "docker", "cron", "nginx", "apache2"],
    "windows_services": ["sshd", "WinRM", "LanmanServer", "W32Time", "W3SVC", "MSSQLSERVER", "docker"],
    "devices": [],
}


@dataclass
class Device:
    name: str
    host: str
    user: str
    port: int = 22
    platform: str = "auto"

    @property
    def key(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"

    @property
    def label(self) -> str:
        return f"{self.name} ({self.user}:{self.port})" if self.name else f"{self.host} ({self.user}:{self.port})"


@dataclass
class Snapshot:
    device: Device
    online: bool
    platform: str = "auto"
    hostname: str = ""
    uptime: str = ""
    cpu: float | None = None
    memory: float | None = None
    disk: float | None = None
    temp: float | None = None
    services: str = ""
    alerts: list[str] = field(default_factory=list)
    latency: float = 0.0
    error: str = ""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if "devices" not in data and "raspberries" in data:
        data["devices"] = data.pop("raspberries")
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(data)
    if "linux_services" not in data:
        merged["linux_services"] = ["ssh", "docker", "cron", "nginx", "apache2"]
    if "windows_services" not in data:
        merged["windows_services"] = ["sshd", "WinRM", "LanmanServer", "W32Time", "W3SVC", "MSSQLSERVER", "docker"]
    merged["devices"] = data.get("devices", [])
    return merged


def save_config(config: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def device_from_dict(data: dict) -> Device:
    return Device(
        name=str(data.get("name") or data.get("Name") or data.get("host") or ""),
        host=str(data.get("host") or data.get("Host") or ""),
        user=str(data.get("user") or data.get("User") or ""),
        port=int(data.get("port") or data.get("Port") or 22),
        platform=str(data.get("platform") or data.get("Platform") or "auto").lower(),
    )


def device_to_dict(device: Device) -> dict:
    return {
        "name": device.name,
        "host": device.host,
        "user": device.user,
        "port": device.port,
        "platform": device.platform,
    }


def strip_ansi(text: str) -> str:
    text = re.sub(r"\x1b\][^\a]*(?:\a|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text


def clean_remote_text(text: str) -> str:
    if not text:
        return ""
    text = strip_ansi(text).replace("\r", "\n")
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            lines.append("")
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    marker_re = re.compile(r"__(HOSTNAME|UPTIME|LOAD|MEMORY|DISK|TEMP|SERVICES|LOGS|ERROR)__", re.I)
    for raw in clean_remote_text(text).splitlines():
        line = raw.strip()
        match = marker_re.search(line)
        if match:
            current = match.group(1).lower()
            sections.setdefault(current, [])
            tail = line[match.end():].strip()
            if tail:
                sections[current].append(tail)
            continue
        if current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def useful_lines(text: str) -> list[str]:
    bad = (
        "write-output",
        "get-process",
        "get-wmiobject",
        "foreach",
        "forEach-object",
        "powershell.exe",
        "$",
        "if(",
        "if (",
        "exit",
    )
    lines = []
    for line in clean_remote_text(text).splitlines():
        clean = line.strip()
        low = clean.lower()
        if not clean:
            continue
        if any(part.lower() in low for part in bad):
            continue
        if re.search(r"[A-Za-z0-9_.\\-]+@[A-Za-z0-9_.\\-]+ .*?>", clean):
            continue
        lines.append(clean)
    return lines


def first_number(text: str) -> float | None:
    for line in useful_lines(text):
        match = re.search(r"-?\d+(?:[.,]\d+)?", line)
        if match:
            try:
                return float(match.group(0).replace(",", "."))
            except ValueError:
                pass
    return None


def parse_cpu(text: str) -> float | None:
    return first_number(text)


def parse_memory(text: str) -> float | None:
    direct = first_number(text)
    if direct is not None and len(useful_lines(text)) <= 2:
        return direct
    for line in useful_lines(text):
        if line.lower().startswith("mem:"):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    total = float(parts[1])
                    used = float(parts[2])
                    if total > 0:
                        return round((used / total) * 100)
                except ValueError:
                    return None
    return direct


def parse_disk(text: str) -> float | None:
    direct = first_number(text)
    if direct is not None and len(useful_lines(text)) <= 2:
        return direct
    for line in useful_lines(text):
        match = re.search(r"\s(\d+)%\s", f" {line} ")
        if match:
            return float(match.group(1))
    return direct


def parse_temp(text: str) -> float | None:
    return first_number(text)


def filtered_services(text: str) -> str:
    keep = []
    for line in useful_lines(text):
        low = line.lower()
        if low in {"n/d", "nan"}:
            continue
        if ":" in line or line.upper().startswith("PROCESSI") or line.upper().startswith("SERVIZI") or "nessun" in low:
            keep.append(line)
    return "\n".join(keep).strip()


def run_process(args: list[str], input_text: str | None = None, timeout: int = 20) -> tuple[int, str, str, float]:
    start = time.time()
    try:
        completed = subprocess.run(
            args,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return completed.returncode, completed.stdout, completed.stderr, time.time() - start
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        return 124, str(out), str(err), time.time() - start
    except Exception as exc:
        return 1, "", str(exc), time.time() - start


def ssh_base(device: Device) -> list[str]:
    return [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=1",
        "-p",
        str(device.port),
        f"{device.user}@{device.host}",
    ]


def linux_command(config: dict, heavy: bool) -> str:
    services = " ".join(s.strip() for s in config.get("linux_services", []) if str(s).strip())
    lines = [
        "echo __HOSTNAME__; hostname",
        "echo __UPTIME__; uptime -p 2>/dev/null || uptime",
        "echo __LOAD__; cat /proc/loadavg",
        "echo __MEMORY__; free -m",
        "echo __DISK__; df -h /",
        "echo __TEMP__; if command -v vcgencmd >/dev/null 2>&1; then vcgencmd measure_temp; elif [ -f /sys/class/thermal/thermal_zone0/temp ]; then awk '{printf \"%.1f C\\n\", $1/1000}' /sys/class/thermal/thermal_zone0/temp; else echo N/D; fi",
        "echo __SERVICES__",
        "echo PROCESSI CPU",
        "ps -eo comm,pcpu,pmem --sort=-pcpu 2>/dev/null | head -n 8 || true",
        "echo",
        "echo PROCESSI MEMORIA",
        "ps -eo comm,pcpu,pmem --sort=-pmem 2>/dev/null | head -n 8 || true",
        "echo",
        "echo SERVIZI CONFIGURATI",
    ]
    if services:
        lines.append(f"for svc in {services}; do state=$(systemctl is-active \"$svc\" 2>/dev/null || true); [ -n \"$state\" ] && echo \"$svc: $state\"; done")
    else:
        lines.append("echo Nessun servizio configurato.")
    if heavy:
        lines += ["echo __LOGS__", "journalctl -n 12 --no-pager 2>/dev/null || tail -n 12 /var/log/syslog 2>/dev/null || true"]
    return " ; ".join(lines)


def windows_script(config: dict, heavy: bool) -> str:
    names = [str(s).strip() for s in config.get("windows_services", []) if str(s).strip()]
    if not names:
        names = ["sshd", "WinRM", "LanmanServer", "W32Time", "W3SVC", "docker", "MSSQLSERVER"]
    names = list(dict.fromkeys(names))
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    lines = [
        "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass",
        "$ErrorActionPreference='SilentlyContinue'",
        "$ProgressPreference='SilentlyContinue'",
        "Write-Output '__HOSTNAME__'",
        "if($env:COMPUTERNAME){Write-Output $env:COMPUTERNAME}else{hostname}",
        "Write-Output '__UPTIME__'",
        "$os=Get-WmiObject Win32_OperatingSystem -ErrorAction SilentlyContinue",
        "if($os -and $os.LastBootUpTime){Write-Output ([Management.ManagementDateTimeConverter]::ToDateTime($os.LastBootUpTime).ToString('s'))}else{Write-Output 'N/D'}",
        "Write-Output '__LOAD__'",
        "$p1=(Get-Process|Where-Object{$_.CPU -ne $null}|Measure-Object CPU -Sum).Sum",
        "Start-Sleep -Milliseconds 700",
        "$p2=(Get-Process|Where-Object{$_.CPU -ne $null}|Measure-Object CPU -Sum).Sum",
        "$cv=0",
        "if($p1 -ne $null -and $p2 -ne $null){$cv=(($p2-$p1)/0.7/[Environment]::ProcessorCount)*100}",
        "if($cv -lt 0){$cv=0}",
        "if($cv -gt 100){$cv=100}",
        "Write-Output ([math]::Round($cv,2).ToString([Globalization.CultureInfo]::InvariantCulture))",
        "Write-Output '__MEMORY__'",
        "$mv=$null",
        "try{Add-Type -AssemblyName Microsoft.VisualBasic;$ci=New-Object Microsoft.VisualBasic.Devices.ComputerInfo;if($ci.TotalPhysicalMemory -gt 0){$mv=(($ci.TotalPhysicalMemory-$ci.AvailablePhysicalMemory)/$ci.TotalPhysicalMemory)*100}}catch{}",
        "if($mv -eq $null -and $os -and $os.TotalVisibleMemorySize -gt 0){$mv=(($os.TotalVisibleMemorySize-$os.FreePhysicalMemory)/$os.TotalVisibleMemorySize)*100}",
        "if($mv -eq $null){try{$total=(Get-WmiObject Win32_ComputerSystem -ErrorAction SilentlyContinue).TotalPhysicalMemory;$avail=(Get-Counter '\\Memory\\Available Bytes' -ErrorAction SilentlyContinue).CounterSamples[0].CookedValue;if($total -gt 0){$mv=(($total-$avail)/$total)*100}}catch{}}",
        "if($mv -ne $null){Write-Output ([math]::Round($mv,0).ToString([Globalization.CultureInfo]::InvariantCulture))}else{Write-Output 'N/D'}",
        "Write-Output '__DISK__'",
        "$dv=$null",
        "try{$drive=[System.IO.DriveInfo]::GetDrives()|Where-Object{$_.Name -eq 'C:\\'}|Select-Object -First 1;if($drive -and $drive.TotalSize -gt 0){$dv=(($drive.TotalSize-$drive.AvailableFreeSpace)/$drive.TotalSize)*100}}catch{}",
        "if($dv -eq $null){$d=Get-WmiObject Win32_LogicalDisk -Filter \"DeviceID='C:'\" -ErrorAction SilentlyContinue;if($d -and $d.Size -gt 0){$dv=(($d.Size-$d.FreeSpace)/$d.Size)*100}}",
        "if($dv -ne $null){Write-Output ([math]::Round($dv,0).ToString([Globalization.CultureInfo]::InvariantCulture))}else{Write-Output 'N/D'}",
        "Write-Output '__TEMP__'",
        "Write-Output 'N/D'",
        "Write-Output '__SERVICES__'",
        "Write-Output 'PROCESSI CPU'",
        "Get-Process|Sort-Object CPU -Descending|Select-Object -First 8|ForEach-Object{Write-Output ($_.ProcessName+': CPU '+[math]::Round($_.CPU,1)+' RAM '+[math]::Round($_.WorkingSet64/1MB,0)+' MB')}",
        "Write-Output ''",
        "Write-Output 'PROCESSI MEMORIA'",
        "Get-Process|Sort-Object WorkingSet64 -Descending|Select-Object -First 8|ForEach-Object{Write-Output ($_.ProcessName+': RAM '+[math]::Round($_.WorkingSet64/1MB,0)+' MB')}",
        "Write-Output ''",
        "Write-Output 'SERVIZI'",
        f"$names=@({quoted})",
        "$found=$false",
        "foreach($n in $names){$s=Get-Service -Name $n -ErrorAction SilentlyContinue;if($s){$found=$true;Write-Output ($s.Name+': '+$s.Status)}}",
        "if(-not $found){Write-Output 'Nessun servizio configurato trovato.'}",
    ]
    if heavy:
        lines += [
            "Write-Output '__LOGS__'",
            "Get-EventLog -LogName System -Newest 10 -ErrorAction SilentlyContinue|ForEach-Object{Write-Output ($_.TimeGenerated.ToString('s')+' '+$_.EntryType+' '+$_.Source)}",
        ]
    lines += ["exit", "exit"]
    return "\r\n".join(lines) + "\r\n"


def windows_ps1(config: dict, heavy: bool) -> str:
    lines = windows_script(config, heavy).splitlines()
    if lines and lines[0].lower().startswith("powershell.exe"):
        lines = lines[1:]
    while lines and lines[-1].strip().lower() == "exit":
        lines.pop()
    lines.append("exit 0")
    return "\r\n".join(lines) + "\r\n"


def run_windows_check(device: Device, config: dict, heavy: bool) -> tuple[int, str, str, float]:
    temp_dir = APP_DIR / "_tmp"
    temp_dir.mkdir(exist_ok=True)
    local_script = temp_dir / f"monitor_pc_check_{re.sub(r'[^A-Za-z0-9_.-]+', '_', device.host)}.ps1"
    local_script.write_text(windows_ps1(config, heavy), encoding="utf-8-sig")

    remote_name = "monitor_pc_check.ps1"
    scp_args = [
        "scp",
        "-o",
        "ConnectTimeout=5",
        "-P",
        str(device.port),
        str(local_script),
        f"{device.user}@{device.host}:{remote_name}",
    ]
    scp_code, scp_out, scp_err, _ = run_process(scp_args, timeout=20)
    if scp_code == 0:
        direct_args = ssh_base(device) + [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            f".\\{remote_name}",
        ]
        direct_code, direct_out, direct_err, direct_latency = run_process(direct_args, timeout=24)
        if parse_sections(direct_out + "\n" + direct_err):
            return direct_code, direct_out, direct_err, direct_latency

        command = f"powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\\{remote_name}\r\nexit\r\n"
        args = ["ssh", "-tt", "-o", "ConnectTimeout=5", "-p", str(device.port), f"{device.user}@{device.host}"]
        code, stdout, stderr, latency = run_process(args, input_text=command, timeout=24)
        stderr = (stderr or "") + "\nTentativo diretto:\n" + clean_remote_text(direct_err or direct_out)
        return code, stdout, stderr, latency

    args = ["ssh", "-tt", "-o", "ConnectTimeout=5", "-p", str(device.port), f"{device.user}@{device.host}"]
    code, stdout, stderr, latency = run_process(args, input_text=windows_script(config, heavy), timeout=24)
    stderr = (stderr or "") + "\nSCP non riuscito:\n" + clean_remote_text(scp_err or scp_out)
    return code, stdout, stderr, latency


def build_snapshot(device: Device, platform: str, sections: dict[str, str], stderr: str, latency: float, config: dict) -> Snapshot:
    hostname = useful_lines(sections.get("hostname", ""))[0] if useful_lines(sections.get("hostname", "")) else device.host
    uptime_lines = useful_lines(sections.get("uptime", ""))
    uptime = uptime_lines[0] if uptime_lines else "N/D"
    snap = Snapshot(
        device=device,
        online=True,
        platform=platform,
        hostname=hostname,
        uptime=uptime,
        cpu=parse_cpu(sections.get("load", "")),
        memory=parse_memory(sections.get("memory", "")),
        disk=parse_disk(sections.get("disk", "")),
        temp=parse_temp(sections.get("temp", "")),
        services=filtered_services(sections.get("services", "")),
        latency=latency,
    )
    if snap.memory is not None and snap.memory >= config["memory_alert"]:
        snap.alerts.append("RAM alta")
    if snap.disk is not None and snap.disk >= config["disk_alert"]:
        snap.alerts.append("disco quasi pieno")
    if snap.temp is not None and snap.temp >= config["temp_alert"]:
        snap.alerts.append("temperatura alta")
    for line in snap.services.splitlines():
        if any(word in line.lower() for word in ("inactive", "failed", "stopped")):
            snap.alerts.append("servizio: " + line)
    return snap


def has_metrics(snap: Snapshot) -> bool:
    return snap.cpu is not None and (snap.memory is not None or snap.disk is not None or bool(snap.services.strip()))


def looks_windows(text: str) -> bool:
    low = text.lower()
    return any(part in low for part in ("microsoft windows", "powershell", "non e riconosciuto", "not recognized", "exec request failed"))


def check_device(device: Device, config: dict, heavy: bool) -> Snapshot:
    attempts = []
    if device.platform in ("linux", "windows"):
        attempts.append(device.platform)
    else:
        attempts.extend(["linux", "windows"])

    last_error = ""
    for platform in attempts:
        if platform == "linux":
            args = ssh_base(device) + ["sh", "-lc", linux_command(config, heavy)]
            code, stdout, stderr, latency = run_process(args, timeout=18)
        else:
            code, stdout, stderr, latency = run_windows_check(device, config, heavy)

        combined = clean_remote_text(stdout + "\n" + stderr)
        sections = parse_sections(combined)
        if sections:
            snap = build_snapshot(device, platform, sections, stderr, latency, config)
            if has_metrics(snap):
                return snap

        if platform == "linux" and looks_windows(combined) and "windows" not in attempts:
            attempts.append("windows")

        last_error = clean_remote_text(stderr or stdout)[:1200]
        if platform == "windows":
            DEBUG_PATH.write_text(
                "Dispositivo: " + device.label + "\n"
                + "Data: " + datetime.now().isoformat(timespec="seconds") + "\n"
                + "Sezioni: " + ", ".join(sections.keys()) + "\n\n"
                + "Output:\n" + combined + "\n",
                encoding="utf-8",
            )

    return Snapshot(
        device=device,
        online=False,
        latency=0.0,
        error=last_error or "Controllo non riuscito.",
        alerts=["offline"],
    )


class MonitorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Monitor PC")
        self.geometry("1320x820")
        self.minsize(1100, 700)
        self.configure(bg=COLORS["bg"])
        if ICON_PATH.exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except Exception:
                pass

        self.config_data = load_config()
        self.devices = [device_from_dict(d) for d in self.config_data.get("devices", [])]
        self.snapshots: dict[str, Snapshot] = {}
        self.history = {"CPU": [], "Memoria": [], "Disco": [], "Temperatura": []}
        self.result_queue: queue.Queue[Snapshot] = queue.Queue()
        self.last_heavy: dict[str, float] = {}
        self.current: Device | None = None
        self.checking: set[str] = set()

        self.create_widgets()
        self.reload_devices()
        self.after(250, self.process_results)
        self.after(1000, self.auto_tick)

    def create_widgets(self) -> None:
        self.style = ttk.Style(self)
        self.style.theme_use("default")
        self.style.configure("Treeview", background=COLORS["input"], foreground=COLORS["text"], fieldbackground=COLORS["input"], rowheight=24)
        self.style.configure("Treeview.Heading", background="#f0f0f0", foreground="#111")

        tk.Label(self, text="Monitor PC", bg=COLORS["bg"], fg=COLORS["text"], font=("Segoe UI", 20, "bold")).place(x=18, y=14)
        tk.Label(self, text="Dispositivo", bg=COLORS["bg"], fg=COLORS["text"], font=("Segoe UI", 10)).place(x=18, y=66)

        self.combo_var = tk.StringVar()
        self.combo = ttk.Combobox(self, textvariable=self.combo_var, state="readonly")
        self.combo.place(x=132, y=62, width=620, height=32)
        self.combo.bind("<<ComboboxSelected>>", lambda _e: self.select_current())

        self.real_time = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Tempo reale", variable=self.real_time, bg=COLORS["bg"], fg=COLORS["text"], selectcolor=COLORS["panel"], activebackground=COLORS["bg"]).place(x=790, y=18)

        self.button("Controlla", 770, 60, 130, 34, COLORS["blue"], self.refresh_all)
        self.button("Salva", 912, 60, 130, 34, COLORS["green"], self.add_device)
        self.button("Aggiorna", 1054, 60, 130, 34, COLORS["cyan"], self.update_device)
        self.button("Configura SSH", 770, 102, 272, 34, COLORS["violet"], self.configure_ssh)
        self.button("Rimuovi", 770, 144, 272, 34, COLORS["red"], self.remove_device)
        self.button("Terminale SSH", 770, 186, 272, 34, COLORS["yellow"], self.open_terminal)
        self.button("Test SSH", 1054, 186, 130, 34, COLORS["orange"], self.test_ssh)

        self.left = self.panel(18, 106, 282, 430)
        tk.Label(self.left, text="Stato dispositivi", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 12, "bold")).place(x=14, y=12)
        self.tree = ttk.Treeview(self.left, columns=("stato", "nome", "alert"), show="headings", height=5)
        for col, width in (("stato", 62), ("nome", 124), ("alert", 50)):
            self.tree.heading(col, text=col.capitalize())
            self.tree.column(col, width=width, stretch=False)
        self.tree.place(x=14, y=50, width=250, height=145)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.select_from_tree())

        tk.Label(self.left, text="Nuovo dispositivo", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 12, "bold")).place(x=14, y=215)
        self.name_var = tk.StringVar()
        self.host_var = tk.StringVar()
        self.user_var = tk.StringVar()
        self.port_var = tk.StringVar(value="22")
        self.field(self.left, "Etichetta", self.name_var, 258)
        self.field(self.left, "Host/VPN", self.host_var, 308)
        self.field(self.left, "Utente", self.user_var, 358)
        self.field(self.left, "Porta", self.port_var, 408)
        tk.Label(self.left, text="Prima autorizza SSH dal PC\nverso il dispositivo.", bg=COLORS["panel"], fg=COLORS["muted"], justify="left").place(x=14, y=462)

        self.status_panel = self.panel(320, 230, 660, 82)
        tk.Label(self.status_panel, text="Stato", bg=COLORS["panel"], fg=COLORS["text"]).place(x=14, y=8)
        self.status_label = tk.Label(self.status_panel, text="Seleziona un dispositivo", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 12, "bold"), anchor="w")
        self.status_label.place(x=14, y=32, width=520)
        self.latency_label = tk.Label(self.status_panel, text="", bg=COLORS["panel"], fg=COLORS["muted"], anchor="e")
        self.latency_label.place(x=548, y=34, width=90)

        self.metric_labels = {}
        for i, (name, color) in enumerate((("CPU", COLORS["blue"]), ("Memoria", COLORS["green"]), ("Disco", COLORS["yellow"]), ("Temperatura", COLORS["red"]))):
            p = self.metric_panel(320 + i * 168, 326, 158, 86, color)
            tk.Label(p, text=name, bg=COLORS["panel"], fg=color).place(x=10, y=8)
            lbl = tk.Label(p, text="N/D", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 14, "bold"), anchor="w")
            lbl.place(x=10, y=34, width=130)
            self.metric_labels[name] = lbl

        self.graph = tk.Canvas(self, bg=COLORS["panel2"], highlightbackground=COLORS["border"], highlightthickness=1)
        self.graph.place(x=320, y=438, width=760, height=330)

        self.services_box = self.text_panel("Servizi", 1095, 230, 310, 160)
        self.alerts_box = self.text_panel("Avvisi", 1095, 405, 310, 160)
        self.history_box = self.text_panel("Storico", 1095, 580, 310, 160)

        self.bind("<Configure>", lambda _e: self.draw_graphs())

    def button(self, text: str, x: int, y: int, w: int, h: int, color: str, cmd) -> None:
        b = tk.Button(self, text=text, command=cmd, bg=COLORS["panel2"], fg=COLORS["text"], activebackground=COLORS["panel"], activeforeground=COLORS["text"], bd=1, relief="solid", highlightbackground=color)
        b.place(x=x, y=y, width=w, height=h)

    def panel(self, x: int, y: int, w: int, h: int) -> tk.Frame:
        f = tk.Frame(self, bg=COLORS["panel"], highlightbackground=COLORS["border"], highlightthickness=1)
        f.place(x=x, y=y, width=w, height=h)
        return f

    def metric_panel(self, x: int, y: int, w: int, h: int, color: str) -> tk.Frame:
        f = self.panel(x, y, w, h)
        tk.Frame(f, bg=color).place(x=0, y=0, width=w, height=3)
        return f

    def field(self, parent: tk.Frame, label: str, var: tk.StringVar, y: int) -> None:
        tk.Label(parent, text=label, bg=COLORS["panel"], fg=COLORS["text"], anchor="w").place(x=14, y=y, width=130, height=28)
        tk.Entry(parent, textvariable=var, bg=COLORS["input"], fg=COLORS["text"], insertbackground=COLORS["text"], relief="solid", bd=1).place(x=150, y=y, width=110, height=28)

    def text_panel(self, title: str, x: int, y: int, w: int, h: int) -> tk.Text:
        frame = self.panel(x, y, w, h)
        tk.Label(frame, text=title, bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).place(x=10, y=8)
        box = tk.Text(frame, bg=COLORS["input"], fg=COLORS["text"], relief="flat", font=("Consolas", 9), wrap="word")
        box.place(x=10, y=34, width=w - 20, height=h - 44)
        box.configure(state="disabled")
        return box

    def set_text(self, box: tk.Text, text: str, color: str | None = None) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        if color:
            box.configure(fg=color)
        box.configure(state="disabled")

    def reload_devices(self) -> None:
        self.combo["values"] = [d.label for d in self.devices]
        self.update_tree()
        if self.devices and self.current is None:
            self.combo.current(0)
            self.select_current()

    def select_current(self) -> None:
        idx = self.combo.current()
        if idx < 0 or idx >= len(self.devices):
            return
        self.current = self.devices[idx]
        self.name_var.set(self.current.name)
        self.host_var.set(self.current.host)
        self.user_var.set(self.current.user)
        self.port_var.set(str(self.current.port))
        self.clear_graphs()
        self.refresh_current()

    def select_from_tree(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        key = selected[0]
        for i, device in enumerate(self.devices):
            if device.key == key:
                self.combo.current(i)
                self.select_current()
                break

    def add_device(self) -> None:
        device = self.device_from_fields()
        if not device:
            return
        self.devices.append(device)
        self.save_devices()
        self.reload_devices()
        self.combo.current(len(self.devices) - 1)
        self.select_current()

    def update_device(self) -> None:
        if self.current is None:
            return
        device = self.device_from_fields()
        if not device:
            return
        idx = self.devices.index(self.current)
        device.platform = self.current.platform
        self.devices[idx] = device
        self.current = device
        self.save_devices()
        self.reload_devices()

    def remove_device(self) -> None:
        if self.current is None:
            return
        if not messagebox.askyesno("Rimuovi", f"Rimuovere {self.current.name}?"):
            return
        self.devices = [d for d in self.devices if d.key != self.current.key]
        self.current = None
        self.save_devices()
        self.reload_devices()

    def device_from_fields(self) -> Device | None:
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        if not host or not user:
            messagebox.showwarning("Dati mancanti", "Inserisci almeno Host/VPN e Utente.")
            return None
        try:
            port = int(self.port_var.get().strip() or "22")
        except ValueError:
            port = 22
        return Device(self.name_var.get().strip() or host, host, user, port)

    def save_devices(self) -> None:
        self.config_data["devices"] = [device_to_dict(d) for d in self.devices]
        save_config(self.config_data)

    def refresh_all(self) -> None:
        for device in self.devices:
            self.start_check(device)
        self.status_label.configure(text="Controlli avviati...", fg=COLORS["text"])

    def refresh_current(self) -> None:
        if self.current:
            self.start_check(self.current)

    def start_check(self, device: Device) -> None:
        if device.key in self.checking:
            return
        self.checking.add(device.key)
        self.update_tree()
        heavy_every = max(10, int(self.config_data.get("heavy_check_seconds", 60)))
        heavy = time.time() - self.last_heavy.get(device.key, 0) >= heavy_every
        if heavy:
            self.last_heavy[device.key] = time.time()
        thread = threading.Thread(target=self.worker_check, args=(device, heavy), daemon=True)
        thread.start()

    def worker_check(self, device: Device, heavy: bool) -> None:
        snap = check_device(device, self.config_data, heavy)
        self.result_queue.put(snap)

    def process_results(self) -> None:
        while True:
            try:
                snap = self.result_queue.get_nowait()
            except queue.Empty:
                break
            self.checking.discard(snap.device.key)
            if snap.online:
                for device in self.devices:
                    if device.key == snap.device.key and device.platform != snap.platform:
                        device.platform = snap.platform
                self.save_devices()
            self.snapshots[snap.device.key] = snap
            self.write_history(snap)
            if self.current and self.current.key == snap.device.key:
                self.render_snapshot(snap)
            self.update_tree()
        self.after(250, self.process_results)

    def auto_tick(self) -> None:
        if self.real_time.get():
            self.refresh_all()
        self.after(max(2, int(self.config_data.get("refresh_seconds", 5))) * 1000, self.auto_tick)

    def render_snapshot(self, snap: Snapshot) -> None:
        self.latency_label.configure(text=f"{snap.latency:.1f}s" if snap.latency else "")
        if not snap.online:
            self.status_label.configure(text=f"Offline: {snap.device.name}", fg=COLORS["red"])
            for lbl in self.metric_labels.values():
                lbl.configure(text="N/D")
            self.set_text(self.services_box, "Servizi non disponibili: dispositivo offline.")
            self.set_text(self.alerts_box, "\n".join(snap.alerts + ([snap.error] if snap.error else [])), COLORS["yellow"])
            self.draw_graphs()
            return
        self.status_label.configure(text=f"Online: {snap.device.name}   {snap.hostname}   {snap.uptime}", fg=COLORS["green"])
        values = {
            "CPU": f"{snap.cpu:.2f}" if snap.cpu is not None else "N/D",
            "Memoria": f"{snap.memory:.0f}%" if snap.memory is not None else "N/D",
            "Disco": f"{snap.disk:.0f}%" if snap.disk is not None else "N/D",
            "Temperatura": f"{snap.temp:.1f} C" if snap.temp is not None else "N/D",
        }
        for k, v in values.items():
            self.metric_labels[k].configure(text=v)
        self.add_history("CPU", snap.cpu)
        self.add_history("Memoria", snap.memory)
        self.add_history("Disco", snap.disk)
        self.add_history("Temperatura", snap.temp)
        self.set_text(self.services_box, snap.services or "Nessun servizio trovato.")
        self.set_text(self.alerts_box, "Nessun avviso." if not snap.alerts else "\n".join(snap.alerts), COLORS["green"] if not snap.alerts else COLORS["yellow"])
        self.set_text(self.history_box, self.history_preview())
        self.draw_graphs()

    def update_tree(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for device in self.devices:
            if device.key in self.checking:
                state = "CHECK"
                alert = ""
            elif device.key in self.snapshots:
                snap = self.snapshots[device.key]
                state = "OK" if snap.online else "OFF"
                alert = f"{len(snap.alerts)}!" if snap.alerts else ""
            else:
                state = "?"
                alert = ""
            self.tree.insert("", "end", iid=device.key, values=(state, device.name, alert))

    def add_history(self, name: str, value: float | None) -> None:
        if value is None:
            return
        data = self.history[name]
        data.append(float(value))
        del data[:-60]

    def clear_graphs(self) -> None:
        for values in self.history.values():
            values.clear()
        self.draw_graphs()

    def draw_graphs(self) -> None:
        if not hasattr(self, "graph"):
            return
        self.graph.delete("all")
        w = max(self.graph.winfo_width(), 300)
        h = max(self.graph.winfo_height(), 200)
        pads = 14
        panels = [
            ("CPU", COLORS["blue"], 0, 0),
            ("Memoria", COLORS["green"], 1, 0),
            ("Disco", COLORS["yellow"], 0, 1),
            ("Temperatura", COLORS["red"], 1, 1),
        ]
        cell_w = (w - pads * 3) / 2
        cell_h = (h - pads * 3) / 2
        for name, color, col, row in panels:
            x = pads + col * (cell_w + pads)
            y = pads + row * (cell_h + pads)
            self.graph.create_rectangle(x, y, x + cell_w, y + cell_h, outline=COLORS["border"], fill=COLORS["panel2"])
            self.graph.create_text(x + 12, y + 18, text=name, anchor="w", fill=COLORS["muted"], font=("Segoe UI", 10))
            vals = self.history[name]
            label = "N/D" if not vals else f"{vals[-1]:.1f}"
            if name in ("Memoria", "Disco"):
                label = "N/D" if not vals else f"{vals[-1]:.0f}%"
            if name == "Temperatura":
                label = "N/D" if not vals else f"{vals[-1]:.1f} C"
            self.graph.create_text(x + 12, y + 44, text=label, anchor="w", fill=COLORS["text"], font=("Segoe UI", 16, "bold"))
            for i in range(1, 4):
                yy = y + 65 + i * ((cell_h - 80) / 4)
                self.graph.create_line(x + 12, yy, x + cell_w - 12, yy, fill="#394049")
            if len(vals) < 2:
                continue
            max_y = 100 if name in ("CPU", "Memoria", "Disco") else max(100, max(vals) + 10)
            pts = []
            for i, val in enumerate(vals[-60:]):
                px = x + 16 + i * ((cell_w - 32) / max(1, min(59, len(vals) - 1)))
                py = y + cell_h - 18 - (min(max(val, 0), max_y) / max_y) * (cell_h - 92)
                pts.extend([px, py])
            if len(pts) >= 4:
                self.graph.create_line(*pts, fill=color, width=2, smooth=True)

    def write_history(self, snap: Snapshot) -> None:
        new_file = not HISTORY_PATH.exists()
        with HISTORY_PATH.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(["timestamp", "name", "host", "online", "cpu", "memory", "disk", "temp", "alerts"])
            writer.writerow([
                datetime.now().isoformat(timespec="seconds"),
                snap.device.name,
                snap.device.host,
                snap.online,
                "" if snap.cpu is None else snap.cpu,
                "" if snap.memory is None else snap.memory,
                "" if snap.disk is None else snap.disk,
                "" if snap.temp is None else snap.temp,
                " | ".join(snap.alerts + ([snap.error] if snap.error else [])),
            ])

    def history_preview(self) -> str:
        if not HISTORY_PATH.exists():
            return "Nessuno storico salvato."
        lines = HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-18:])

    def open_terminal(self) -> None:
        if not self.current:
            return
        subprocess.Popen(["powershell.exe", "-NoExit", "-Command", f"ssh -p {self.current.port} {self.current.user}@{self.current.host}"], creationflags=0)

    def test_ssh(self) -> None:
        if not self.current:
            return
        subprocess.Popen(["powershell.exe", "-NoExit", "-Command", f"ssh -vvv -p {self.current.port} {self.current.user}@{self.current.host}"], creationflags=0)

    def configure_ssh(self) -> None:
        if not self.current:
            return
        msg = (
            "Si apre un terminale SSH.\n\n"
            "Se chiede la password, inseriscila.\n"
            "Quando entra correttamente, l'accesso base funziona.\n\n"
            "Per ora questa versione Python non modifica il server."
        )
        messagebox.showinfo("Configura SSH", msg)
        self.open_terminal()


if __name__ == "__main__":
    app = MonitorApp()
    app.mainloop()
