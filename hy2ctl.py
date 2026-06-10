#!/usr/bin/env python3
import datetime as dt
import hashlib
import http.server
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import textwrap
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

APP_DIR = "/opt/hy2-manager"
ETC_DIR = "/etc/hy2-manager"
BACKUP_DIR = os.path.join(ETC_DIR, "backups")
HYSTERIA_DIR = "/etc/hysteria"
DB_PATH = os.path.join(ETC_DIR, "users.db")
CONFIG_PATH = os.path.join(HYSTERIA_DIR, "config.yaml")
CERT_PATH = os.path.join(ETC_DIR, "server.crt")
KEY_PATH = os.path.join(ETC_DIR, "server.key")
LOG_PATH = "/var/log/hy2-manager.log"
HYSTERIA_BIN = "/usr/local/bin/hysteria"
AUTH_HOST = "127.0.0.1"
AUTH_PORT = 28787
STATS_HOST = "127.0.0.1"
STATS_PORT = 28788
APP_VERSION = "1.2.3"
MAX_AUTH_BODY = 8192
DB_TIMEOUT = 10
DB_WRITE_LOCK = threading.Lock()
DEVICE_AUTH_WINDOW_SECONDS = 60
AUTH_DEVICE_LOCK = threading.Lock()
AUTH_DEVICE_CACHE = {}
TC_TABLE = "hy2_manager"
TC_DEFAULT_CLASS = "999"
TC_MARK_BASE = 0x1200
ALLOWED_UPDATE_FIELDS = {
    "password",
    "max_devices",
    "speed_down_bps",
    "traffic_limit_bytes",
    "reset_cycle",
    "expire_at",
}
SEPARATOR = "————————————"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RESET_LABELS = {
    "none": "不清零",
    "daily": "每天",
    "weekly": "每周",
    "monthly": "每月",
}
REASON_LABELS = {
    "ok": "通过",
    "not found": "接口不存在",
    "bad json": "请求格式错误",
    "bad auth": "认证格式错误",
    "invalid user": "用户不存在或密码错误",
    "disabled": "用户已禁用",
    "quota exceeded": "流量已超额",
    "expired": "用户已到期",
    "device limit": "设备数超限",
    "download bandwidth policy": "下载速率超出策略",
}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def log(message):
    line = f"{now_iso()} {message}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def display_width(text):
    text = ANSI_RE.sub("", str(text))
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return width


def cut_display(text, width):
    text = ANSI_RE.sub("", str(text))
    out = ""
    used = 0
    for ch in str(text):
        w = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if used + w > width:
            break
        out += ch
        used += w
    return out


def pad(text, width):
    text = str(text)
    visible = display_width(text)
    if visible > width:
        text = cut_display(text, width)
        visible = display_width(text)
    return text + " " * max(0, width - display_width(text))


def print_table(headers, rows):
    widths = [display_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], min(display_width(cell), 50))
    print("  ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(pad(cell, widths[i]) for i, cell in enumerate(row)))


def c(text, color):
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else str(text)


def hi(text):
    return c(text, GREEN)


KEY_VALUE_LABELS = {
    "IP",
    "端口",
    "协议",
    "认证",
    "TLS",
    "SNI",
    "设备限制",
    "下载限速",
    "上传限速",
    "用户状态",
    "到期时间",
    "已用流量",
    "剩余流量",
    "总流量",
    "清零周期",
    "状态",
    "在线/设备",
    "流量",
    "公开地址",
    "客户端 SNI",
    "备份保留",
}


def maybe_hi_value(label, value):
    value = str(value)
    if ANSI_RE.search(value) or value in ("", "-"):
        return value
    if label in KEY_VALUE_LABELS or "限速" in str(label) or "流量" in str(label):
        return hi(value)
    return value


def color_state_text(text):
    text = str(text)
    if text in ("运行", "启用", "已安装", "已启动", "通过", "active", "enabled", "ok"):
        return c(text, GREEN)
    if text in ("警告", "启动中", "停止中", "未知"):
        return c(text, GREEN)
    if text in ("失败", "停止", "故障", "禁用", "未安装", "未启动", "inactive", "failed", "disabled"):
        return c(text, RED)
    return hi(text)


def info(message):
    print(f"{c('[信息]', GREEN)} {message}")


def error(message):
    print(f"{c('[错误]', RED)} {message}")


def tip(message):
    print(f"{c('[注意]', GREEN)} {message}")


def term_width():
    return max(48, min(96, shutil.get_terminal_size((80, 24)).columns))


def hr(char="-"):
    print(char * min(term_width(), 51))


def install_status_text():
    installed = os.path.exists(HYSTERIA_BIN) and os.path.exists(CONFIG_PATH)
    state = service_state("hysteria-server.service")
    if installed and state == "active":
        return f"{c('已安装', GREEN)} 并 {c('已启动', GREEN)}"
    elif installed:
        return f"{c('已安装', GREEN)} 但 {c('未启动', RED)}"
    return c("未安装", RED)


def runtime_status_text():
    try:
        with db() as con:
            users = con.execute("SELECT count(*) AS c FROM users").fetchone()["c"]
        online = stats_get("/online") or {}
        online_total = sum(int(v) for v in online.values()) if isinstance(online, dict) else 0
    except Exception:
        users = 0
        online_total = 0
    core = hysteria_version().replace("Hysteria2 ", "")
    return f"用户: {hi(users)} | 在线: {hi(online_total)} | 内核: {hi(core)}"


def menu_status_line():
    return f" 当前状态: {install_status_text()}"


def render_menu(title, items, range_text, return_label="退出", main=False):
    print()
    if main:
        print(f"  {title} {c(f'[v{APP_VERSION}]', RED)}")
        print(f"  {runtime_status_text()}")
        print()
    else:
        print(title)
        print()
    for number, label in items:
        if number == "":
            print(SEPARATOR)
        else:
            marker = f"{int(number):>2}." if str(number).isdigit() else f"{number:>3}"
            print(f"{c(marker, GREEN)} {label}")
    print(f"{c(' 0.', GREEN)} {return_label}")
    if main:
        print()
        print(menu_status_line())
    print()
    try:
        return input(f"请输入数字 [{range_text}]：").strip()
    except EOFError:
        return "0"


def input_choice(range_text="0-9"):
    try:
        return input(f"请输入数字 [{range_text}]：").strip()
    except EOFError:
        return "0"


def run_menu_action(func, pause_after=False):
    func()


def hysteria_version(path=HYSTERIA_BIN):
    if not os.path.exists(path):
        return "未安装"
    out = run(f"{path} version", check=False, capture=True)
    for line in out.splitlines():
        if line.startswith("Version:"):
            return "Hysteria2 " + line.split(":", 1)[1].strip()
    return "Hysteria2 已安装"


def print_kv_block(rows, label_width=10):
    width = term_width()
    value_width = max(20, width - label_width - 5)
    for label, value in rows:
        label_text = pad(label, label_width)
        value = maybe_hi_value(label, value)
        wrapped = textwrap.wrap(str(value), width=value_width, break_long_words=False, break_on_hyphens=False) or [""]
        print(f"{label_text} : {wrapped[0]}")
        for extra in wrapped[1:]:
            print(f"{' ' * label_width}   {extra}")


def run(cmd, check=True, capture=False):
    result = subprocess.run(
        cmd,
        shell=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if check and result.returncode != 0:
        output = (result.stdout or "").strip()
        raise SystemExit(f"命令执行失败: {cmd}\n{output}")
    return (result.stdout or "").strip()


def run_input(argv, data, check=True):
    result = subprocess.run(
        argv,
        input=data,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if check and result.returncode != 0:
        output = (result.stdout or "").strip()
        raise SystemExit(f"命令执行失败: {' '.join(argv)}\n{output}")
    return (result.stdout or "").strip()


def require_root():
    if os.geteuid() != 0:
        raise SystemExit("请使用 root 权限运行。")


def require_systemd():
    if not shutil.which("systemctl") or not os.path.isdir("/run/systemd/system"):
        raise SystemExit("当前系统未运行 systemd，暂不支持一键安装系统服务。")


def ensure_dirs():
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(ETC_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(HYSTERIA_DIR, exist_ok=True)
    os.chmod(ETC_DIR, 0o700)
    os.chmod(BACKUP_DIR, 0o700)
def db():
    ensure_dirs()
    con = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                max_devices INTEGER NOT NULL DEFAULT 0,
                speed_down_bps INTEGER NOT NULL DEFAULT 0,
                speed_up_bps INTEGER NOT NULL DEFAULT 0,
                traffic_limit_bytes INTEGER NOT NULL DEFAULT 0,
                traffic_used_bytes INTEGER NOT NULL DEFAULT 0,
                reset_cycle TEXT NOT NULL DEFAULT 'none',
                expire_at TEXT NOT NULL DEFAULT '',
                last_reset TEXT NOT NULL DEFAULT '',
                last_addr TEXT NOT NULL DEFAULT '',
                last_auth_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                addr TEXT NOT NULL,
                ip TEXT NOT NULL,
                tx INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_auth_events_user_time
                ON auth_events(username, created_at);
            CREATE INDEX IF NOT EXISTS idx_auth_events_ip_time
                ON auth_events(ip, created_at);
            """
        )
        user_cols = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
        if "expire_at" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN expire_at TEXT NOT NULL DEFAULT ''")
        defaults = {
            "api_secret": secrets.token_urlsafe(32),
            "public_host": public_host_default(),
            "client_sni": "www.bing.com",
            "backup_keep": "20",
            "last_auto_backup_date": "",
            "traffic_control_enabled": "0",
            "traffic_control_iface": "",
        }
        for key, value in defaults.items():
            con.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                (key, value),
            )
        con.execute(
            "DELETE FROM settings WHERE key IN ("
            "SELECT key FROM settings WHERE key=char(111,98,102,115)||'_password'"
            ")"
        )
        con.execute("UPDATE users SET speed_up_bps=0 WHERE speed_up_bps!=0")
    os.chmod(DB_PATH, 0o600)


def setting(key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def backup_db(reason="manual", quiet=False):
    init_db()
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_reason = "".join(c for c in reason if c.isalnum() or c in ("-", "_")) or "backup"
    path = os.path.join(BACKUP_DIR, f"users-{ts}-{safe_reason}.db")
    with db() as src, sqlite3.connect(path) as dst:
        src.backup(dst)
    os.chmod(path, 0o600)
    try:
        keep = max(1, int(setting("backup_keep", "20") or "20"))
    except ValueError:
        keep = 20
        set_setting("backup_keep", keep)
    backups = sorted(
        [os.path.join(BACKUP_DIR, name) for name in os.listdir(BACKUP_DIR) if name.endswith(".db")]
    )
    for old in backups[:-keep]:
        try:
            os.remove(old)
        except OSError:
            pass
    if not quiet:
        print(f"备份已生成: {path}")
    return path


def auto_backup_if_due():
    today = dt.datetime.now().strftime("%Y-%m-%d")
    if setting("last_auto_backup_date") == today:
        return
    backup_db("auto", quiet=True)
    set_setting("last_auto_backup_date", today)


def public_ip():
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                value = r.read().decode().strip()
            socket.inet_aton(value)
            return value
        except Exception:
            continue
    return ""


def public_host_default():
    detected = public_ip()
    if detected:
        return detected
    try:
        route_ip = run(
            "ip -o -4 route get 1.1.1.1 2>/dev/null | "
            "awk '{for(i=1;i<=NF;i++) if($i==\"src\") {print $(i+1); exit}}'",
            check=False,
            capture=True,
        ).strip()
        if route_ip:
            socket.inet_aton(route_ip)
            return route_ip
    except Exception:
        pass
    return "127.0.0.1"


def arch_name():
    machine = run("uname -m", capture=True)
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine.startswith("armv7"):
        return "armv7"
    raise SystemExit(f"不支持的系统架构: {machine}")


def install_hysteria_binary():
    arch = arch_name()
    url = f"https://download.hysteria.network/app/latest/hysteria-linux-{arch}"
    print(f"正在下载 Hysteria2 最新内核 linux-{arch}...")
    run(f"curl -LfsS --retry 3 -o {HYSTERIA_BIN}.tmp {url}")
    run(f"install -m 0755 {HYSTERIA_BIN}.tmp {HYSTERIA_BIN}")
    run(f"rm -f {HYSTERIA_BIN}.tmp", check=False)
    print(hysteria_version())


def update_hysteria_core():
    require_root()
    ensure_dirs()
    arch = arch_name()
    url = f"https://download.hysteria.network/app/latest/hysteria-linux-{arch}"
    tmp = f"/tmp/hysteria-linux-{arch}.{os.getpid()}"
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_bin = os.path.join(BACKUP_DIR, f"hysteria-{ts}")
    print(f"正在下载 Hysteria2 最新内核 linux-{arch}...")
    run(f"curl -LfsS --retry 3 -o {tmp} {url}")
    run(f"chmod 755 {tmp}")
    new_version = hysteria_version(tmp)
    old_version = hysteria_version() if os.path.exists(HYSTERIA_BIN) else ""
    if old_version and new_version == old_version:
        run(f"rm -f {tmp}", check=False)
        print("当前已是最新下载版本。")
        return
    backup_db("before-core-update", quiet=True)
    if os.path.exists(HYSTERIA_BIN):
        shutil.copy2(HYSTERIA_BIN, backup_bin)
        os.chmod(backup_bin, 0o755)
        print(f"旧内核已备份: {backup_bin}")
    run(f"install -m 0755 {tmp} {HYSTERIA_BIN}")
    run(f"rm -f {tmp}", check=False)
    run("systemctl restart hysteria-server.service", check=False)
    time.sleep(2)
    state = run("systemctl is-active hysteria-server.service 2>/dev/null", check=False, capture=True)
    if state != "active" and os.path.exists(backup_bin):
        error("新内核启动失败，正在自动回滚旧版本。")
        shutil.copy2(backup_bin, HYSTERIA_BIN)
        os.chmod(HYSTERIA_BIN, 0o755)
        run("systemctl restart hysteria-server.service", check=False)
        raise SystemExit("已回滚：hysteria-server 未能进入 active 状态。")
    print("内核更新完成，hysteria-server 已正常运行。")
    print(hysteria_version())


def ensure_cert():
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        return
    if not shutil.which("openssl"):
        raise SystemExit("缺少 openssl，无法生成自签证书。")
    run(
        "openssl req -x509 -newkey rsa:2048 -nodes "
        f"-keyout {KEY_PATH} -out {CERT_PATH} -days 3650 "
        "-subj '/CN=hysteria.local'"
    )
    os.chmod(KEY_PATH, 0o600)


def write_hysteria_config():
    api_secret = setting("api_secret")
    text = f"""listen: :443

tls:
  cert: {CERT_PATH}
  key: {KEY_PATH}
  sniGuard: disable

auth:
  type: http
  http:
    url: http://{AUTH_HOST}:{AUTH_PORT}/auth
    insecure: false

ignoreClientBandwidth: false

congestion:
  type: bbr
  bbrProfile: standard

trafficStats:
  listen: {STATS_HOST}:{STATS_PORT}
  secret: {api_secret}

masquerade:
  type: string
  string:
    content: ok
    statusCode: 200
    headers:
      content-type: text/plain
"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(CONFIG_PATH, 0o600)


def write_systemd_units():
    auth_unit = f"""[Unit]
Description=hy2-manager auth backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 {APP_DIR}/hy2ctl.py auth-server
Restart=always
RestartSec=2s
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectControlGroups=true
ProtectKernelModules=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
"""
    hysteria_unit = f"""[Unit]
Description=Hysteria 2 server managed by hy2-manager
After=network-online.target hy2-auth.service
Wants=network-online.target
Requires=hy2-auth.service

[Service]
Type=simple
ExecStart={HYSTERIA_BIN} server -c {CONFIG_PATH}
Restart=always
RestartSec=2s
LimitNOFILE=1048576
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectControlGroups=true
ProtectKernelModules=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
"""
    monitor_service = f"""[Unit]
Description=hy2-manager quota and device monitor

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {APP_DIR}/hy2ctl.py monitor
LimitNOFILE=65535
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectControlGroups=true
ProtectKernelModules=true
RestrictSUIDSGID=true
"""
    monitor_timer = """[Unit]
Description=Run hy2-manager monitor every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s
Unit=hy2-monitor.service

[Install]
WantedBy=timers.target
"""
    files = {
        "/etc/systemd/system/hy2-auth.service": auth_unit,
        "/etc/systemd/system/hysteria-server.service": hysteria_unit,
        "/etc/systemd/system/hy2-monitor.service": monitor_service,
        "/etc/systemd/system/hy2-monitor.timer": monitor_timer,
    }
    for path, content in files.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    run("systemctl daemon-reload")


def open_firewall():
    if shutil.which("ufw") and run("ufw status 2>/dev/null | head -1", check=False, capture=True).lower().endswith("active"):
        run("ufw allow 443/udp", check=False)
        return
    if shutil.which("firewall-cmd") and run("firewall-cmd --state 2>/dev/null", check=False, capture=True) == "running":
        run("firewall-cmd --permanent --add-port=443/udp", check=False)
        run("firewall-cmd --reload", check=False)
        return
    if shutil.which("iptables"):
        run("iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT -p udp --dport 443 -j ACCEPT", check=False)


def default_iface():
    iface = run(
        "ip -o -4 route show to default 2>/dev/null | awk '{print $5; exit}'",
        check=False,
        capture=True,
    ).strip()
    if iface:
        return iface
    return run(
        "ip -o -4 route get 1.1.1.1 2>/dev/null | "
        "awk '{for(i=1;i<=NF;i++) if($i==\"dev\") {print $(i+1); exit}}'",
        check=False,
        capture=True,
    ).strip()


def traffic_control_iface():
    return setting("traffic_control_iface", "").strip() or default_iface()


def traffic_control_enabled():
    return setting("traffic_control_enabled", "0") == "1"


def require_traffic_control_tools():
    missing = [name for name in ("tc", "nft", "ip") if not shutil.which(name)]
    if missing:
        raise SystemExit("缺少流控依赖: " + ", ".join(missing) + "，请安装 iproute2 和 nftables。")


def class_rate_mbit(bps):
    return max(0.1, int(bps or 0) * 8 / 1000 / 1000)


def parse_addr_port(addr):
    addr = str(addr or "").strip()
    if not addr:
        return "", 0
    if addr.startswith("[") and "]" in addr:
        host, _, tail = addr[1:].partition("]")
        if tail.startswith(":") and tail[1:].isdigit():
            return host, int(tail[1:])
        return host, 0
    if ":" not in addr:
        return addr, 0
    host, port = addr.rsplit(":", 1)
    return host, int(port) if port.isdigit() else 0


def traffic_control_targets():
    online = stats_get("/online") or {}
    if not isinstance(online, dict):
        online = {}
    targets = []
    with db() as con:
        users = con.execute(
            "SELECT username,speed_down_bps FROM users "
            "WHERE enabled=1 AND speed_down_bps>0 ORDER BY username"
        ).fetchall()
        for index, user in enumerate(users, 1):
            username = user["username"]
            if online and int(online.get(username, 0)) <= 0:
                continue
            rows = con.execute(
                """
                SELECT addr, max(created_at) AS seen_at
                FROM auth_events
                WHERE username=? AND ok=1 AND addr!=''
                GROUP BY addr
                ORDER BY seen_at DESC
                LIMIT 50
                """,
                (username,),
            ).fetchall()
            endpoints = []
            for row in rows:
                ip, port = parse_addr_port(row["addr"])
                if not ip or not port:
                    continue
                try:
                    parsed = ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if parsed.is_global:
                    endpoints.append((parsed.version, ip, port))
            if endpoints:
                classid = 1000 + index
                mark = TC_MARK_BASE + index
                targets.append({
                    "username": username,
                    "rate_mbit": class_rate_mbit(user["speed_down_bps"]),
                    "classid": classid,
                    "mark": mark,
                    "endpoints": endpoints,
                })
    return targets


def nft_script_for_targets(targets):
    elements4 = []
    elements6 = []
    for target in targets:
        mark = f"0x{target['mark']:x}"
        for version, ip, port in target["endpoints"]:
            item = f"{ip} . {port} : {mark} timeout 10m"
            if version == 6:
                elements6.append(item)
            else:
                elements4.append(item)
    element4_text = ""
    element6_text = ""
    if elements4:
        element4_text = "elements = { " + ", ".join(elements4) + " }"
    if elements6:
        element6_text = "elements = { " + ", ".join(elements6) + " }"
    return f"""table inet {TC_TABLE} {{
  map marks4 {{
    type ipv4_addr . inet_service : mark;
    flags timeout;
    {element4_text + ';' if element4_text else ''}
  }}
  map marks6 {{
    type ipv6_addr . inet_service : mark;
    flags timeout;
    {element6_text + ';' if element6_text else ''}
  }}
  chain output {{
    type route hook output priority mangle; policy accept;
    udp sport 443 meta mark set ip daddr . udp dport map @marks4;
    udp sport 443 meta mark set ip6 daddr . udp dport map @marks6;
  }}
}}
"""


def clear_traffic_control(quiet=False):
    iface = traffic_control_iface()
    if iface:
        run(f"tc qdisc del dev {shlex.quote(iface)} root", check=False)
    run(f"nft delete table inet {TC_TABLE}", check=False)
    if not quiet:
        info("服务端平滑限速规则已清理。")


def apply_traffic_control(quiet=False):
    require_root()
    require_traffic_control_tools()
    iface = traffic_control_iface()
    if not iface:
        raise SystemExit("未能识别默认出口网卡，请在流控设置中手动指定。")
    targets = traffic_control_targets()
    qiface = shlex.quote(iface)
    run(f"tc qdisc replace dev {qiface} root handle 1: htb default {TC_DEFAULT_CLASS}")
    run(
        f"tc class replace dev {qiface} parent 1: classid 1:{TC_DEFAULT_CLASS} "
        "htb rate 10000mbit ceil 10000mbit",
        check=False,
    )
    for prio, target in enumerate(targets, 1):
        rate = f"{target['rate_mbit']:.3f}mbit"
        classid = target["classid"]
        mark = target["mark"]
        run(f"tc class replace dev {qiface} parent 1: classid 1:{classid} htb rate {rate} ceil {rate}")
        run(f"tc filter replace dev {qiface} parent 1: protocol ip prio {prio} handle {mark} fw flowid 1:{classid}")
        run(f"tc filter replace dev {qiface} parent 1: protocol ipv6 prio {prio} handle {mark} fw flowid 1:{classid}", check=False)
    run(f"nft delete table inet {TC_TABLE}", check=False)
    run_input(["nft", "-f", "-"], nft_script_for_targets(targets))
    if not quiet:
        info(f"服务端平滑限速已应用，网卡: {iface}，用户规则: {len(targets)}。")


def refresh_traffic_control_if_enabled():
    if not traffic_control_enabled():
        return
    try:
        apply_traffic_control(quiet=True)
    except Exception as e:
        log(f"traffic control refresh failed: {type(e).__name__}: {e}")


def traffic_control_status():
    iface = traffic_control_iface()
    rows = [
        ["功能状态", "启用" if traffic_control_enabled() else "禁用"],
        ["出口网卡", iface or "未识别"],
        ["tc 命令", "可用" if shutil.which("tc") else "缺失"],
        ["nft 命令", "可用" if shutil.which("nft") else "缺失"],
    ]
    print_table(["项目", "状态"], rows)
    if iface:
        print()
        print(run(f"tc -s qdisc show dev {shlex.quote(iface)} 2>/dev/null", check=False, capture=True) or "暂无 tc qdisc。")
    print()
    print(run(f"nft list table inet {TC_TABLE} 2>/dev/null", check=False, capture=True) or "暂无 nft 规则。")


def traffic_control_menu():
    print()
    print("服务端平滑限速")
    hr("-")
    print_kv_block([
        ["功能状态", "启用" if traffic_control_enabled() else "禁用"],
        ["出口网卡", traffic_control_iface() or "未识别"],
    ], label_width=10)
    choice = render_menu("服务端平滑限速", [
        ("1", "启用并应用规则"),
        ("2", "关闭并清理规则"),
        ("3", "立即刷新规则"),
        ("4", "查看流控状态"),
        ("5", "设置出口网卡"),
    ], "0-5", return_label="返回上级菜单")
    if choice == "1":
        set_setting("traffic_control_enabled", "1")
        apply_traffic_control()
    elif choice == "2":
        set_setting("traffic_control_enabled", "0")
        clear_traffic_control()
    elif choice == "3":
        if not traffic_control_enabled():
            print("服务端平滑限速未启用。")
            return
        apply_traffic_control()
    elif choice == "4":
        traffic_control_status()
    elif choice == "5":
        value = input("请输入出口网卡名，例如 eth0/ens3\n(默认: 自动识别): ").strip()
        set_setting("traffic_control_iface", value)
        print("出口网卡设置已更新。")
    elif choice == "0":
        print("已取消。")
    else:
        print("请输入正确的数字 [0-5]")


def install():
    require_root()
    require_systemd()
    ensure_dirs()
    init_db()
    if not shutil.which("curl"):
        raise SystemExit("缺少 curl，无法下载安装内核。")
    install_hysteria_binary()
    ensure_cert()
    write_hysteria_config()
    write_systemd_units()
    open_firewall()
    run("systemctl enable --now hy2-auth.service hysteria-server.service hy2-monitor.timer")
    print("安装完成。输入 hy2 打开管理菜单。")


def uninstall():
    require_root()
    confirm = input("确认卸载 Hysteria2、hy2-manager，并删除配置、服务和数据库？[y/N]: ").strip().lower()
    if confirm != "y":
        print("已取消。")
        return
    run("systemctl disable --now hysteria-server.service hy2-auth.service hy2-monitor.timer hy2-monitor.service", check=False)
    clear_traffic_control(quiet=True)
    for path in (
        "/etc/systemd/system/hysteria-server.service",
        "/etc/systemd/system/hy2-auth.service",
        "/etc/systemd/system/hy2-monitor.service",
        "/etc/systemd/system/hy2-monitor.timer",
        "/usr/local/bin/hy2",
        HYSTERIA_BIN,
    ):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    run("systemctl daemon-reload", check=False)
    run("systemctl reset-failed hysteria-server.service hy2-auth.service hy2-monitor.service hy2-monitor.timer", check=False)
    run(f"rm -rf {ETC_DIR} {HYSTERIA_DIR} {APP_DIR}", check=False)
    info("Hysteria2、hy2-manager、配置、数据库和 systemd 服务已卸载干净。")
    tip("已安装的 BBR/fq 系统优化配置会保留。")


def parse_mbps(value):
    value = value.strip()
    if not value:
        return 0
    try:
        mbps = float(value)
    except ValueError:
        raise SystemExit("限速格式错误，请输入数字，单位 Mbps。")
    if mbps < 0:
        raise SystemExit("限速不能小于 0。")
    if mbps > 100000:
        raise SystemExit("限速数值过大，请检查输入。")
    return int(mbps * 1000 * 1000 / 8)


def parse_non_negative_int(value, label):
    value = value.strip()
    if not value:
        return 0
    try:
        number = int(value)
    except ValueError:
        raise SystemExit(f"{label} 必须是整数。")
    if number < 0:
        raise SystemExit(f"{label} 不能小于 0。")
    return number


def parse_limit_rows(value, default=30):
    value = value.strip()
    if not value:
        return default
    try:
        rows = int(value)
    except ValueError:
        raise SystemExit("显示行数必须是整数。")
    if rows < 1:
        raise SystemExit("显示行数必须大于 0。")
    return min(rows, 500)


def validate_username(username):
    if not username or ":" in username or any(ch.isspace() for ch in username):
        raise SystemExit("用户名不能为空，且不能包含空格或冒号。")
    if len(username) > 64:
        raise SystemExit("用户名过长，请控制在 64 个字符以内。")
    return username


def validate_password(password):
    if not password or any(ch.isspace() for ch in password):
        raise SystemExit("密码不能为空，且不能包含空白字符。")
    if len(password) > 128:
        raise SystemExit("密码过长，请控制在 128 个字符以内。")
    return password


def bps_to_mbps(bps):
    if not bps:
        return "无限制"
    return f"{bps * 8 / 1000 / 1000:.2f} Mbps"


def parse_gb(value):
    value = value.strip()
    if not value:
        return 0
    try:
        gb = float(value)
    except ValueError:
        raise SystemExit("流量格式错误，请输入数字，单位 GB。")
    if gb < 0:
        raise SystemExit("流量不能小于 0。")
    if gb > 1024 * 1024:
        raise SystemExit("流量数值过大，请检查输入。")
    return int(gb * 1024 ** 3)


def parse_expire_date(value):
    value = value.strip()
    if not value or value in ("0", "none", "永久", "无限"):
        return ""
    try:
        dt.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("到期时间格式错误，请使用 YYYY-MM-DD，例如 2026-12-31。")
    return value


def is_expired_value(value, now=None):
    if not value:
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        expire_day = dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return False
    return now.date() > expire_day


def expire_label(value):
    if not value:
        return "永久"
    return value + (" (已到期)" if is_expired_value(value) else "")


def bytes_human(n):
    n = int(n or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(n)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{n} B"


def quota_summary(row):
    limit = int(row["traffic_limit_bytes"] or 0)
    used = int(row["traffic_used_bytes"] or 0)
    if not limit:
        return bytes_human(used), "无限制", "无限制"
    remain = max(0, limit - used)
    return bytes_human(used), bytes_human(remain), bytes_human(limit)


def per_device_speed_bps(row, field):
    if field == "speed_up_bps":
        return 0
    return int(row[field] or 0)


def speed_policy_text(row):
    down = int(row["speed_down_bps"] or 0)
    return bps_to_mbps(down), "无限制（固定）"


def print_config_confirm(label, value):
    print()
    print(SEPARATOR)
    print(f"\t{label} : {c(value, GREEN)}")
    print(SEPARATOR)
    print()


def prompt_cancel(prompt):
    value = input(f"{prompt}\n(默认: 取消): ").strip()
    if not value:
        print("已取消。")
        return ""
    return value


def choose_user(prompt="请输入用户名"):
    list_users()
    return prompt_cancel(prompt)


def prompt_user_fields(existing=None):
    existing = existing or {}
    if existing.get("username"):
        username = existing["username"]
    else:
        username = input("请输入要设置的用户 用户名(请勿重复, 不支持空格和冒号)\n(默认: doubi): ").strip() or "doubi"
    username = validate_username(username)
    print_config_confirm("用户名", username)
    password = input("请输入要设置的用户 密码\n(默认: 随机生成/保持原值): ").strip()
    if not password:
        password = existing.get("password") or secrets.token_urlsafe(12)
    password = validate_password(password)
    print_config_confirm("密码", password)
    max_devices = input(f"请输入要设置的用户 设备数限制\n(默认: {existing.get('max_devices', 0)}，0 为无限制): ").strip()
    max_devices = parse_non_negative_int(max_devices, "设备数限制") if max_devices else int(existing.get("max_devices", 0))
    print_config_confirm("设备数限制", str(max_devices) if max_devices else "无限制")
    speed_down = input(f"请输入要设置的用户 下载限速上限(单位: Mbps)\n(默认: {bps_to_mbps(existing.get('speed_down_bps', 0))}，0 为无限制): ").strip()
    down_bps = parse_mbps(speed_down) if speed_down else int(existing.get("speed_down_bps", 0))
    up_bps = 0
    print_config_confirm("下载限速", bps_to_mbps(down_bps))
    print_config_confirm("上传限速", "无限制（固定）")
    traffic = input(f"请输入要设置的用户 可使用的总流量上限(单位: GB)\n(默认: {bytes_human(existing.get('traffic_limit_bytes', 0))}，0 为无限制): ").strip()
    traffic_bytes = parse_gb(traffic) if traffic else int(existing.get("traffic_limit_bytes", 0))
    print_config_confirm("用户总流量", bytes_human(traffic_bytes) if traffic_bytes else "无限制")
    current_reset = existing.get("reset_cycle", "none")
    reset = input(f"请输入流量清零周期 none/daily/weekly/monthly\n(默认: {current_reset}/{RESET_LABELS.get(current_reset, current_reset)}): ").strip().lower()
    reset = reset or existing.get("reset_cycle", "none")
    if reset not in ("none", "daily", "weekly", "monthly"):
        raise SystemExit("清零周期无效，只能输入 none/daily/weekly/monthly。")
    print_config_confirm("流量清零", RESET_LABELS.get(reset, reset))
    current_expire = existing.get("expire_at", "")
    expire = input(f"请输入用户到期时间 YYYY-MM-DD\n(默认: {expire_label(current_expire)}，留空保持/永久): ").strip()
    if expire:
        expire = parse_expire_date(expire)
    else:
        expire = current_expire
    print_config_confirm("到期时间", expire_label(expire))
    return {
        "username": username,
        "password": password,
        "max_devices": max_devices,
        "speed_down_bps": down_bps,
        "speed_up_bps": up_bps,
        "traffic_limit_bytes": traffic_bytes,
        "reset_cycle": reset,
        "expire_at": expire,
    }


def add_user():
    init_db()
    data = prompt_user_fields()
    t = now_iso()
    with db() as con:
        try:
            con.execute(
                """
                INSERT INTO users(username,password,enabled,max_devices,speed_down_bps,
                  speed_up_bps,traffic_limit_bytes,reset_cycle,expire_at,last_reset,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data["username"], data["password"], 1, data["max_devices"],
                    data["speed_down_bps"], data["speed_up_bps"],
                    data["traffic_limit_bytes"], data["reset_cycle"], data["expire_at"], t, t, t,
                ),
            )
        except sqlite3.IntegrityError:
            raise SystemExit("[错误] 用户已存在。")
    info("用户添加成功。")
    row = get_user(data["username"])
    if row:
        print_node_info(row)


def get_user(username):
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return row


def client_config_text(row):
    host = setting("public_host", public_host_default())
    sni = setting("client_sni", "www.bing.com")
    lines = [
        f"server: {host}:443",
        f"auth: {row['username']}:{row['password']}",
        "tls:",
        f"  sni: {sni}",
        "  insecure: true",
    ]
    speed_up = per_device_speed_bps(row, "speed_up_bps")
    speed_down = per_device_speed_bps(row, "speed_down_bps")
    if speed_up or speed_down:
        lines.append("bandwidth:")
        if speed_up:
            lines.append(f"  up: {speed_up * 8 / 1000 / 1000:.2f} mbps")
        if speed_down:
            lines.append(f"  down: {speed_down * 8 / 1000 / 1000:.2f} mbps")
    lines.extend([
        "socks5:",
        "  listen: 127.0.0.1:1080",
        "http:",
        "  listen: 127.0.0.1:8080",
    ])
    return "\n".join(lines) + "\n"


def client_uri(row):
    host = setting("public_host", public_host_default())
    sni = setting("client_sni", "www.bing.com")
    auth = urllib.parse.quote(f"{row['username']}:{row['password']}", safe=":")
    params = {
        "peer": sni,
        "insecure": "1",
        "sni": sni,
    }
    speed_up = per_device_speed_bps(row, "speed_up_bps")
    speed_down = per_device_speed_bps(row, "speed_down_bps")
    if speed_up:
        params["upmbps"] = f"{speed_up * 8 / 1000 / 1000:.2f}"
    if speed_down:
        params["downmbps"] = f"{speed_down * 8 / 1000 / 1000:.2f}"
    query = urllib.parse.urlencode(params)
    return f"hysteria2://{auth}@{host}:443/?{query}"


def print_node_info(row, include_config=True):
    host = setting("public_host", public_host_default())
    sni = setting("client_sni", "www.bing.com")
    used, remain, total = quota_summary(row)
    total_speed, each_speed = speed_policy_text(row)
    print()
    hr("=")
    print()
    print(f" 用户 [{hi(row['username'])}] 的配置信息：")
    print()
    print_kv_block(
        [
            ("IP", host),
            ("端口", "443/UDP"),
            ("协议", "Hysteria2"),
            ("认证", row["username"] + ":" + row["password"]),
            ("TLS", "自签证书(insecure)"),
            ("SNI", sni),
            ("设备限制", str(row["max_devices"]) if row["max_devices"] else "无限制"),
            ("下载限速", total_speed),
            ("上传限速", each_speed),
            ("用户状态", "启用" if row["enabled"] else "禁用"),
            ("到期时间", expire_label(row["expire_at"])),
        ],
        label_width=10,
    )
    print()
    print_kv_block(
        [
            ("已用流量", used),
            ("剩余流量", remain),
            ("总流量", total),
            ("清零周期", RESET_LABELS.get(row["reset_cycle"], row["reset_cycle"])),
        ],
        label_width=10,
    )
    print()
    print("Hysteria2 URI:")
    print(hi(client_uri(row)))
    print()
    tip("URI 已包含节点基础信息和下载限速参数；上传固定不限速。")
    print()
    hr("=")
    print()


def edit_user():
    init_db()
    username = input("请输入要修改的用户名\n(默认: 取消): ").strip()
    if not username:
        print("已取消。")
        return
    row = get_user(username)
    if not row:
        error("用户不存在。")
        return
    data = prompt_user_fields(dict(row))
    with db() as con:
        con.execute(
            """
            UPDATE users SET password=?, max_devices=?, speed_down_bps=?, speed_up_bps=?,
              traffic_limit_bytes=?, reset_cycle=?, expire_at=?, updated_at=? WHERE username=?
            """,
            (
                data["password"], data["max_devices"], data["speed_down_bps"],
                data["speed_up_bps"], data["traffic_limit_bytes"],
                data["reset_cycle"], data["expire_at"], now_iso(), username,
            ),
        )
    info("用户已更新。")
    refresh_traffic_control_if_enabled()


def update_user_field(username, field, value, label):
    if field not in ALLOWED_UPDATE_FIELDS:
        raise SystemExit("内部错误：不允许修改该字段。")
    with db() as con:
        con.execute(f"UPDATE users SET {field}=?, updated_at=? WHERE username=?", (value, now_iso(), username))
    info(f"用户 {label} 修改成功 [用户名: {username}]")
    if field == "speed_down_bps":
        refresh_traffic_control_if_enabled()


def modify_user_password():
    username = choose_user("请输入要修改密码的用户名")
    if not username:
        return
    row = get_user(username)
    if not row:
        error("用户不存在。")
        return
    password = input("请输入要设置的用户 密码\n(默认: 随机生成): ").strip() or secrets.token_urlsafe(12)
    password = validate_password(password)
    print_config_confirm("密码", password)
    update_user_field(username, "password", password, "密码")


def modify_user_devices():
    username = choose_user("请输入要修改设备数限制的用户名")
    if not username:
        return
    row = get_user(username)
    if not row:
        error("用户不存在。")
        return
    value = input("请输入要设置的用户 欲限制的设备数\n(默认: 无限): ").strip()
    devices = parse_non_negative_int(value, "设备数限制") if value else 0
    print_config_confirm("设备数限制", str(devices) if devices else "无限制")
    update_user_field(username, "max_devices", devices, "设备数限制")


def modify_user_down_speed():
    username = choose_user("请输入要修改下载限速的用户名")
    if not username:
        return
    row = get_user(username)
    if not row:
        error("用户不存在。")
        return
    value = input("请输入要设置的用户 下载限速上限(单位: Mbps)\n(默认: 无限): ").strip()
    bps = parse_mbps(value) if value else 0
    print_config_confirm("下载限速", bps_to_mbps(bps))
    update_user_field(username, "speed_down_bps", bps, "下载限速")


def modify_user_up_speed():
    print("上传限速已固定为无限制，不支持调整。")


def modify_user_traffic_limit():
    username = choose_user("请输入要修改总流量的用户名")
    if not username:
        return
    row = get_user(username)
    if not row:
        error("用户不存在。")
        return
    value = input("请输入要设置的用户 可使用的总流量上限(单位: GB)\n(默认: 无限): ").strip()
    traffic = parse_gb(value) if value else 0
    print_config_confirm("用户总流量", bytes_human(traffic) if traffic else "无限制")
    update_user_field(username, "traffic_limit_bytes", traffic, "总流量")


def modify_user_reset_cycle():
    username = choose_user("请输入要修改流量清零周期的用户名")
    if not username:
        return
    row = get_user(username)
    if not row:
        error("用户不存在。")
        return
    value = input("请输入流量清零周期 none/daily/weekly/monthly\n(默认: none): ").strip().lower() or "none"
    if value not in RESET_LABELS:
        error("清零周期无效。")
        return
    print_config_confirm("流量清零", RESET_LABELS.get(value, value))
    update_user_field(username, "reset_cycle", value, "流量清零周期")


def modify_user_expire_at():
    username = choose_user("请输入要修改到期时间的用户名")
    if not username:
        return
    row = get_user(username)
    if not row:
        error("用户不存在。")
        return
    value = input("请输入用户到期时间 YYYY-MM-DD\n(默认: 永久): ").strip()
    expire = parse_expire_date(value) if value else ""
    print_config_confirm("到期时间", expire_label(expire))
    update_user_field(username, "expire_at", expire, "到期时间")


def delete_user():
    list_users()
    username = input("请输入要删除的用户名\n(默认: 取消): ").strip()
    if not username:
        print("已取消。")
        return
    with db() as con:
        con.execute("DELETE FROM users WHERE username=?", (username,))
    kick_users([username])
    info("用户已删除。")


def toggle_user():
    list_users()
    username = input("请输入要启用/禁用的用户名\n(默认: 取消): ").strip()
    if not username:
        print("已取消。")
        return
    row = get_user(username)
    if not row:
        error("用户不存在。")
        return
    enabled = 0 if row["enabled"] else 1
    with db() as con:
        con.execute("UPDATE users SET enabled=?, updated_at=? WHERE username=?", (enabled, now_iso(), username))
    if not enabled:
        kick_users([username])
    print(f"{username} 当前状态: {'启用' if enabled else '禁用'}。")


def list_users():
    init_db()
    online = stats_get("/online") or {}
    with db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY username").fetchall()
    if not rows:
        error("没有发现 用户，请检查 !")
        return
    total_used = sum(int(r["traffic_used_bytes"] or 0) for r in rows)
    print()
    print(f"=== 用户总数 {c(str(len(rows)), GREEN)}")
    print()
    for r in rows:
        limit = bytes_human(r["traffic_limit_bytes"]) if r["traffic_limit_bytes"] else "无限制"
        online_count = int(online.get(r["username"], 0))
        max_devices = str(r["max_devices"]) if r["max_devices"] else "不限"
        status_label = "启用" if r["enabled"] else "禁用"
        print(f"用户: {c(r['username'], GREEN)}")
        total_speed, each_speed = speed_policy_text(r)
        print_kv_block(
            [
                ("状态", status_label),
                ("在线/设备", f"{online_count}/{max_devices}"),
                ("流量", f"{bytes_human(r['traffic_used_bytes'])} / {limit}"),
                ("下载限速", total_speed),
                ("上传限速", each_speed),
                ("清零周期", RESET_LABELS.get(r["reset_cycle"], r["reset_cycle"])),
                ("到期时间", expire_label(r["expire_at"])),
            ],
            label_width=10,
        )
        print()
    print(f"=== 当前所有用户已使用流量总和: {c(bytes_human(total_used), GREEN)}")
    print()


def show_client_config(username=None, show_user_list=True):
    if username is None and show_user_list:
        list_users()
    username = username or input("请输入要查看的用户名\n(默认: 取消): ").strip()
    if not username:
        print("已取消。")
        return
    r = get_user(username)
    if not r:
        error("用户不存在。")
        return
    print_node_info(r)


def stats_request(path, method="GET", body=None):
    secret = setting("api_secret")
    url = f"http://{STATS_HOST}:{STATS_PORT}{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", secret)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            raw = r.read().decode()
        return json.loads(raw) if raw else {}
    except Exception as e:
        log(f"stats error {path}: {e}")
        return None


def stats_get(path):
    return stats_request(path)


def kick_users(users):
    if users:
        stats_request("/kick", method="POST", body=list(users))


def split_ip(addr):
    if not addr:
        return ""
    if addr.startswith("[") and "]" in addr:
        return addr[1:].split("]", 1)[0]
    return addr.rsplit(":", 1)[0]


def record_auth_event(username, addr, ok, reason, tx=0):
    ip = split_ip(addr)
    try:
        with DB_WRITE_LOCK:
            with db() as con:
                con.execute(
                    """
                    INSERT INTO auth_events(username,addr,ip,tx,ok,reason,created_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (username or "-", addr or "", ip, int(tx or 0), 1 if ok else 0, reason or "", now_iso()),
                )
                if username and ok:
                    con.execute(
                        "UPDATE users SET last_addr=?,last_auth_at=?,updated_at=? WHERE username=?",
                        (ip, now_iso(), now_iso(), username),
                    )
    except Exception as e:
        log(f"record_auth_event failed: {e}")


def reserve_auth_device(username, ip, max_devices, window_seconds=DEVICE_AUTH_WINDOW_SECONDS):
    if not max_devices or not ip:
        return True
    now_ts = time.time()
    cutoff = now_ts - window_seconds
    with AUTH_DEVICE_LOCK:
        devices = AUTH_DEVICE_CACHE.setdefault(username, {})
        stale = [seen_ip for seen_ip, seen_at in devices.items() if seen_at < cutoff]
        for seen_ip in stale:
            devices.pop(seen_ip, None)
        if ip in devices or len(devices) < max_devices:
            devices[ip] = now_ts
            return True
        return False


def due_for_reset(row, now=None):
    cycle = row["reset_cycle"]
    if cycle == "none":
        return False
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        last = dt.datetime.fromisoformat(row["last_reset"])
    except Exception:
        return True
    if cycle == "daily":
        return now.date() > last.date()
    if cycle == "weekly":
        return now.isocalendar()[:2] != last.isocalendar()[:2]
    if cycle == "monthly":
        return (now.year, now.month) != (last.year, last.month)
    return False


def monitor():
    init_db()
    auto_backup_if_due()
    now = dt.datetime.now(dt.timezone.utc)
    traffic = stats_get("/traffic?clear=1") or {}
    online = stats_get("/online") or {}
    to_kick = []
    with db() as con:
        rows = con.execute("SELECT * FROM users").fetchall()
        for r in rows:
            username = r["username"]
            used_add = 0
            tx_add = 0
            rx_add = 0
            if username in traffic:
                tx_add = int(traffic[username].get("tx", 0))
                rx_add = int(traffic[username].get("rx", 0))
                used_add = tx_add + rx_add
            if due_for_reset(r, now):
                con.execute(
                    "UPDATE users SET traffic_used_bytes=0,last_reset=?,updated_at=? WHERE username=?",
                    (now.isoformat(), now_iso(), username),
                )
                current_used = 0
            else:
                current_used = int(r["traffic_used_bytes"])
            if used_add:
                current_used += used_add
                con.execute(
                    "UPDATE users SET traffic_used_bytes=?,updated_at=? WHERE username=?",
                    (current_used, now_iso(), username),
                )
            limit = int(r["traffic_limit_bytes"])
            if limit and current_used >= limit and r["enabled"]:
                con.execute("UPDATE users SET enabled=0,updated_at=? WHERE username=?", (now_iso(), username))
                to_kick.append(username)
                log(f"disabled {username}: traffic quota exceeded")
            if r["enabled"] and is_expired_value(r["expire_at"], now):
                con.execute("UPDATE users SET enabled=0,updated_at=? WHERE username=?", (now_iso(), username))
                to_kick.append(username)
                log(f"disabled {username}: expired")
            max_devices = int(r["max_devices"])
            if max_devices and int(online.get(username, 0)) > max_devices:
                to_kick.append(username)
                log(f"kicked {username}: device limit exceeded")
        con.execute(
            "DELETE FROM auth_events WHERE id NOT IN "
            "(SELECT id FROM auth_events ORDER BY id DESC LIMIT 10000)"
        )
    kick_users(sorted(set(to_kick)))
    refresh_traffic_control_if_enabled()


class AuthHandler(http.server.BaseHTTPRequestHandler):
    server_version = "hy2-manager-auth/1.0"

    def do_POST(self):
        try:
            self.handle_auth_post()
        except Exception as e:
            log(f"auth handler error: {type(e).__name__}: {e}")
            try:
                self.reply({"ok": False, "msg": "server error"})
            except Exception:
                pass

    def handle_auth_post(self):
        username = "-"
        addr = ""
        tx = 0

        def deny(reason):
            record_auth_event(username, addr, False, reason, tx)
            self.reply({"ok": False, "msg": reason})

        if self.path != "/auth":
            deny("not found")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > MAX_AUTH_BODY:
                deny("bad json")
                return
            payload = json.loads(self.rfile.read(size).decode() or "{}")
        except Exception:
            deny("bad json")
            return
        auth = str(payload.get("auth", ""))
        addr = str(payload.get("addr", ""))
        tx = int(payload.get("tx") or 0)
        if ":" not in auth:
            deny("bad auth")
            return
        username, password = auth.split(":", 1)
        row = get_user(username)
        if not row or password != row["password"]:
            deny("invalid user")
            return
        if not row["enabled"]:
            deny("disabled")
            return
        if is_expired_value(row["expire_at"]):
            deny("expired")
            return
        if row["traffic_limit_bytes"] and row["traffic_used_bytes"] >= row["traffic_limit_bytes"]:
            deny("quota exceeded")
            return
        max_devices = int(row["max_devices"])
        if max_devices:
            online = stats_get("/online") or {}
            if int(online.get(username, 0)) >= max_devices:
                deny("device limit")
                return
        if max_devices and not reserve_auth_device(username, split_ip(addr), max_devices):
            deny("device limit")
            return
        record_auth_event(username, addr, True, "ok", tx)
        self.reply({"ok": True, "id": username})

    def log_message(self, fmt, *args):
        log("auth " + fmt % args)

    def reply(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class AuthHTTPServer(http.server.ThreadingHTTPServer):
    request_queue_size = 512
    daemon_threads = True
    allow_reuse_address = True


def auth_server():
    init_db()
    server = AuthHTTPServer((AUTH_HOST, AUTH_PORT), AuthHandler)
    log(f"auth server listening on {AUTH_HOST}:{AUTH_PORT}")
    server.serve_forever()


def normalize_isp(*values):
    text = " ".join(str(v or "") for v in values)
    checks = [
        ("中国移动", ("移动", "CMCC", "China Mobile")),
        ("中国联通", ("联通", "Unicom", "China Unicom")),
        ("中国电信", ("电信", "China Telecom", "Chinanet", "CT")),
        ("中国广电", ("广电", "CBN", "China Broadcasting")),
        ("教育网", ("教育网", "CERNET")),
        ("鹏博士", ("鹏博士", "Dr.Peng")),
        ("阿里云", ("阿里", "Alibaba", "Aliyun")),
        ("腾讯云", ("腾讯", "Tencent")),
        ("华为云", ("华为", "Huawei")),
        ("百度云", ("百度", "Baidu")),
    ]
    lowered = text.lower()
    for label, needles in checks:
        if any(n.lower() in lowered for n in needles):
            return label
    return "本地网络"


def join_location_parts(*values):
    parts = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in parts:
            parts.append(value)
    return " ".join(parts) if parts else "中国"


def format_ip9_location(payload):
    data = payload.get("data") or {}
    country = str(data.get("country") or "").strip()
    country_code = str(data.get("country_code") or "").strip().lower()
    if country != "中国" and country_code != "cn":
        return c("未知", RED)
    location = join_location_parts(data.get("prov"), data.get("city"), data.get("area"))
    return f"{location} {hi(normalize_isp(data.get('isp')))}"


def format_cn_location(data):
    if data.get("countryCode") != "CN":
        return c("未知", RED)
    location = join_location_parts(data.get("regionName"), data.get("city"), data.get("district"))
    isp = normalize_isp(data.get("isp"), data.get("org"), data.get("as"))
    return f"{location} {hi(isp)}"


def geo_lookup(ip):
    if not ip:
        return c("未知", RED)
    ip_quoted = urllib.parse.quote(ip)
    url = "https://ip9.com.cn/get?ip=" + ip_quoted
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            data = json.loads(r.read().decode())
        if int(data.get("ret") or 0) == 200:
            return format_ip9_location(data)
    except Exception:
        pass
    fallback_url = (
        "http://ip-api.com/json/"
        + ip_quoted
        + "?lang=zh-CN&fields=status,countryCode,regionName,city,district,isp,org,as,query"
    )
    try:
        with urllib.request.urlopen(fallback_url, timeout=4) as r:
            data = json.loads(r.read().decode())
        if data.get("status") == "success":
            return format_cn_location(data)
    except Exception:
        pass
    return c("未知", RED)


def is_public_ip(ip):
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return False
    return addr.is_global


def show_online():
    online = stats_get("/online") or {}
    if not online:
        print("当前无在线用户，或统计接口尚未就绪。")
        return
    table = []
    for username, count in sorted(online.items()):
        with db() as con:
            rows = con.execute(
                """
                SELECT ip, max(created_at) AS seen_at
                FROM auth_events
                WHERE username=? AND ok=1 AND ip!=''
                GROUP BY ip
                ORDER BY seen_at DESC
                LIMIT 5
                """,
                (username,),
            ).fetchall()
        parts = []
        for r in rows:
            if not is_public_ip(r["ip"]):
                continue
            geo = geo_lookup(r["ip"])
            parts.append(f"{hi(r['ip'])} {geo}")
        table.append([hi(username), hi(str(count)), "；".join(parts) if parts else "-"])
    print_table(["用户名", "在线设备", "最近认证 IP / 中文地理位置 / 网络类型"], table)


def show_auth_history():
    init_db()
    username = input("用户名（留空=全部）: ").strip()
    limit = input("显示行数 [30]: ").strip()
    limit = parse_limit_rows(limit, 30)
    where = ""
    args = []
    if username:
        where = "WHERE username=?"
        args.append(username)
    args.append(limit)
    with db() as con:
        rows = con.execute(
            f"""
            SELECT username,ip,tx,ok,reason,created_at
            FROM auth_events
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            args,
        ).fetchall()
    if not rows:
        print("暂无认证历史。")
        return
    table = []
    for r in rows:
        result = "成功" if r["ok"] else "失败"
        table.append([
            r["created_at"][:19],
            hi(r["username"]),
            c(result, GREEN if r["ok"] else RED),
            str(int(r["tx"])),
            hi(r["ip"]) if r["ip"] else "-",
            REASON_LABELS.get(r["reason"], r["reason"] or "-"),
        ])
    print_table(["时间", "用户名", "结果", "tx(B/s)", "IP", "原因"], table)


def status():
    rows = []
    for svc in ("hy2-auth.service", "hysteria-server.service", "hy2-monitor.timer"):
        state = run(f"systemctl is-active {svc} 2>/dev/null", check=False, capture=True)
        rows.append([svc, color_state_text(service_state_label(state or "未知"))])
    rows.append(["内核版本", hi(hysteria_version())])
    print_table(["项目", "状态"], rows)


def doctor():
    init_db()
    failures = 0
    warnings = 0

    def check(name, ok, detail="", warn=False):
        nonlocal failures, warnings
        if ok:
            state = "通过"
            state_text = c(state, GREEN)
        elif warn:
            state = "警告"
            state_text = c(state, GREEN)
            warnings += 1
        else:
            state = "失败"
            state_text = c(state, RED)
            failures += 1
        detail_text = maybe_hi_value(name, detail) if detail else ""
        print(f"[{state_text}] {name}" + (f" - {detail_text}" if detail_text else ""))

    check("root 权限", os.geteuid() == 0)
    check("Hysteria2 内核文件", os.path.exists(HYSTERIA_BIN), HYSTERIA_BIN)
    for path, mode in (
        (ETC_DIR, 0o700),
        (DB_PATH, 0o600),
        (CONFIG_PATH, 0o600),
        (KEY_PATH, 0o600),
    ):
        if os.path.exists(path):
            actual = os.stat(path).st_mode & 0o777
            check(f"文件权限 {path}", actual == mode, oct(actual), warn=(actual & 0o077) == 0)
        else:
            check(f"文件存在 {path}", False)
    for svc in ("hy2-auth.service", "hysteria-server.service", "hy2-monitor.timer"):
        state = run(f"systemctl is-active {svc} 2>/dev/null", check=False, capture=True)
        check(f"systemd {svc}", state == "active", state or "未知")
        enabled = run(f"systemctl is-enabled {svc} 2>/dev/null", check=False, capture=True)
        check(f"开机自启 {svc}", enabled == "enabled", enabled or "未知")
    check("管理命令 /usr/local/bin/hy2", os.path.exists("/usr/local/bin/hy2"), "/usr/local/bin/hy2")
    integrity = "unknown"
    try:
        with db() as con:
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception as e:
        integrity = str(e)
    check("SQLite 数据库完整性", integrity == "ok", integrity)
    sockets = run("ss -H -lunpt 2>/dev/null; ss -H -lntp 2>/dev/null", check=False, capture=True)
    check("UDP 443 监听", ":443" in sockets and "hysteria" in sockets, "应为 hysteria 监听 :443/udp")
    check("认证后端仅本地监听", "127.0.0.1:28787" in sockets, "127.0.0.1:28787")
    check("统计接口仅本地监听", "127.0.0.1:28788" in sockets, "127.0.0.1:28788")
    auth_ok = False
    try:
        req = urllib.request.Request(f"http://{AUTH_HOST}:{AUTH_PORT}/auth", data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=3) as r:
            auth_ok = r.status == 200
    except Exception:
        auth_ok = False
    check("认证后端响应", auth_ok)
    stats_ok = stats_get("/online") is not None
    check("统计接口响应", stats_ok)
    bbr = run("sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc 2>/dev/null", check=False, capture=True)
    check("BBR/fq 已启用", "tcp_congestion_control = bbr" in bbr and "default_qdisc = fq" in bbr, "bbr/fq", warn=True)
    if traffic_control_enabled():
        check("流控依赖 tc", shutil.which("tc") is not None, "tc")
        check("流控依赖 nft", shutil.which("nft") is not None, "nft")
        check("流控出口网卡", bool(traffic_control_iface()), traffic_control_iface() or "未识别")
    else:
        check("服务端平滑限速", True, "未启用")
    disk = run("df -P / | awk 'NR==2 {print $5}' | tr -d '%'", check=False, capture=True)
    check("磁盘使用率", disk.isdigit() and int(disk) < 90, f"已用 {disk}%", warn=True)
    with db() as con:
        users = con.execute("SELECT count(*) AS c FROM users").fetchone()["c"]
        events = con.execute("SELECT count(*) AS c FROM auth_events").fetchone()["c"]
    print(f"用户数: {users}，认证记录: {events}，备份数: {len([n for n in os.listdir(BACKUP_DIR) if n.endswith('.db')])}")
    if failures:
        raise SystemExit(f"健康检查完成：{failures} 个失败，{warnings} 个警告。")
    print(f"健康检查完成：0 个失败，{warnings} 个警告。")


def install_bbr():
    require_root()
    available = run("sysctl net.ipv4.tcp_available_congestion_control 2>/dev/null", check=False, capture=True)
    if "bbr" not in available:
        error("当前内核未报告 BBR 支持。")
        print(f"可用拥塞控制算法: {available}")
        return
    with open("/etc/sysctl.d/99-hy2-bbr.conf", "w", encoding="utf-8") as f:
        f.write("net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n")
    run("sysctl --system >/dev/null")
    cc = run("sysctl -n net.ipv4.tcp_congestion_control", capture=True)
    qdisc = run("sysctl -n net.core.default_qdisc", capture=True)
    info("BBR 已启用。")
    print(f"当前拥塞控制: {cc}")
    print(f"默认队列算法: {qdisc}")


def reset_traffic():
    username = input("请输入要清零的用户名，输入 all 清零全部\n(默认: 取消): ").strip()
    if not username:
        print("已取消。")
        return
    with db() as con:
        if username != "all":
            row = con.execute("SELECT username FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                error("用户不存在。")
                return
            con.execute("UPDATE users SET traffic_used_bytes=0,last_reset=?,updated_at=? WHERE username=?", (now_iso(), now_iso(), username))
        else:
            con.execute("UPDATE users SET traffic_used_bytes=0,last_reset=?,updated_at=?", (now_iso(), now_iso()))
    stats_get("/traffic?clear=1")
    info("流量已清零。")


def restart_services():
    run("systemctl restart hy2-auth.service hysteria-server.service", check=False)
    run("systemctl restart hy2-monitor.timer", check=False)
    info("服务已重启。")


def start_services():
    run("systemctl start hy2-auth.service hysteria-server.service hy2-monitor.timer", check=False)
    info("服务已启动。")


def stop_services():
    run("systemctl stop hy2-monitor.timer hysteria-server.service hy2-auth.service", check=False)
    info("服务已停止。")


def service_state(name):
    return run(f"systemctl is-active {name} 2>/dev/null", check=False, capture=True) or "未知"


def service_state_label(state):
    return {
        "active": "运行",
        "inactive": "停止",
        "failed": "故障",
        "activating": "启动中",
        "deactivating": "停止中",
        "unknown": "未知",
        "未知": "未知",
    }.get(state, state)


def summary_line():
    try:
        with db() as con:
            users = con.execute("SELECT count(*) AS c FROM users").fetchone()["c"]
        online = stats_get("/online") or {}
        online_total = sum(int(v) for v in online.values()) if isinstance(online, dict) else 0
    except Exception:
        users = 0
        online_total = 0
    core = hysteria_version().replace("Hysteria2 ", "")
    server = service_state_label(service_state("hysteria-server.service"))
    return f"状态: {color_state_text(server)} | 用户: {hi(users)} | 在线: {hi(online_total)} | 内核: {hi(core)}"


def list_backups():
    init_db()
    files = []
    for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if not name.endswith(".db"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        st = os.stat(path)
        files.append([name, bytes_human(st.st_size), dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")])
    if not files:
        print("暂无备份。")
        return
    print_table(["备份文件", "大小", "时间"], files)


def restore_backup():
    init_db()
    backups = sorted([n for n in os.listdir(BACKUP_DIR) if n.endswith(".db")], reverse=True)
    if not backups:
        print("暂无可恢复的备份。")
        return
    list_backups()
    name = input("输入要恢复的备份文件名（留空取消）: ").strip()
    if not name:
        print("已取消。")
        return
    if name not in backups:
        print("备份文件不存在。")
        return
    confirm = input("恢复会覆盖当前用户数据库，确认？[y/N]: ").strip().lower()
    if confirm != "y":
        print("已取消。")
        return
    backup_db("before-restore", quiet=True)
    run("systemctl stop hy2-auth.service hysteria-server.service", check=False)
    shutil.copy2(os.path.join(BACKUP_DIR, name), DB_PATH)
    os.chmod(DB_PATH, 0o600)
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    run("systemctl start hy2-auth.service hysteria-server.service", check=False)
    info("数据库已恢复，服务已重启。")


def show_logs():
    rows = input("显示行数 [80]: ").strip()
    rows = parse_limit_rows(rows, 80)
    cmd = f"journalctl -u hysteria-server.service -u hy2-auth.service -n {rows} --no-pager"
    print(run(cmd, check=False, capture=True))


def clear_auth_history():
    confirm = input("确认清空认证历史？[y/N]: ").strip().lower()
    if confirm != "y":
        print("已取消。")
        return
    with db() as con:
        con.execute("DELETE FROM auth_events")
    info("认证历史已清空。")


def settings_menu():
    print()
    print("当前设置")
    hr("-")
    print_kv_block([
        ["公开地址", setting("public_host", public_host_default())],
        ["客户端 SNI", setting("client_sni", "www.bing.com")],
        ["备份保留", setting("backup_keep", "20") + " 份"],
    ], label_width=10)
    choice = render_menu("系统设置", [
        ("1", "修改公开地址"),
        ("2", "修改客户端 SNI"),
        ("3", "修改备份保留数量"),
    ], "0-3", return_label="返回上级菜单")
    if choice == "1":
        value = input("请输入公开地址/IP\n(默认: 取消): ").strip()
        if value:
            set_setting("public_host", value)
            print("公开地址已更新。")
        else:
            print("已取消。")
    elif choice == "2":
        value = input("请输入客户端 SNI\n(默认: 取消): ").strip()
        if value:
            set_setting("client_sni", value)
            print("客户端 SNI 已更新。")
        else:
            print("已取消。")
    elif choice == "3":
        value = input("请输入备份保留数量\n(默认: 20): ").strip() or "20"
        if value.isdigit() and int(value) >= 1:
            set_setting("backup_keep", value)
            print("备份保留数量已更新。")
        else:
            print("请输入大于 0 的数字。")
    elif choice == "0":
        print("已取消。")
    else:
        print("请输入正确的数字 [0-3]")


def user_menu():
    choice = render_menu("你要做什么？", [
        ("1", "添加 用户配置"),
        ("2", "删除 用户配置"),
        ("", ""),
        ("3", "修改 用户密码"),
        ("4", "修改 设备数限制"),
        ("5", "修改 下载限速"),
        ("6", "修改 用户总流量"),
        ("7", "修改 流量清零周期"),
        ("8", "修改 用户到期时间"),
        ("9", "修改 全部配置"),
        ("", ""),
        ("10", "启用 / 禁用 用户"),
        ("12", "查看 用户节点信息"),
    ], "0-12", return_label="返回上级菜单")
    try:
        if choice == "1":
            add_user()
        elif choice == "2":
            delete_user()
        elif choice == "3":
            modify_user_password()
        elif choice == "4":
            modify_user_devices()
        elif choice == "5":
            modify_user_down_speed()
        elif choice == "6":
            modify_user_traffic_limit()
        elif choice == "7":
            modify_user_reset_cycle()
        elif choice == "8":
            modify_user_expire_at()
        elif choice == "9":
            edit_user()
        elif choice == "10":
            toggle_user()
        elif choice == "12":
            show_client_config()
        elif choice == "0":
            print("已取消。")
        else:
            print("请输入正确的数字 [0-12]")
    except KeyboardInterrupt:
        print("\n已取消。")
    except Exception as e:
        print(f"错误: {e}")


def traffic_menu():
    choice = render_menu("流量与连接", [
        ("1", "显示在线 IP 和地理位置"),
        ("2", "查看认证历史"),
        ("3", "清零流量"),
        ("4", "清空认证历史"),
    ], "0-4", return_label="返回上级菜单")
    try:
        if choice == "1":
            show_online()
        elif choice == "2":
            show_auth_history()
        elif choice == "3":
            reset_traffic()
        elif choice == "4":
            clear_auth_history()
        elif choice == "0":
            print("已取消。")
        else:
            print("请输入正确的数字 [0-4]")
    except KeyboardInterrupt:
        print("\n已取消。")
    except Exception as e:
        print(f"错误: {e}")


def service_menu():
    choice = render_menu("服务管理", [
        ("1", "启动服务"),
        ("2", "停止服务"),
        ("3", "重启服务"),
        ("4", "查看服务状态"),
        ("5", "健康检查"),
    ], "0-5", return_label="返回上级菜单")
    try:
        if choice == "1":
            start_services()
        elif choice == "2":
            stop_services()
        elif choice == "3":
            restart_services()
        elif choice == "4":
            status()
        elif choice == "5":
            doctor()
        elif choice == "0":
            print("已取消。")
        else:
            print("请输入正确的数字 [0-5]")
    except KeyboardInterrupt:
        print("\n已取消。")
    except Exception as e:
        print(f"错误: {e}")


def tools_menu():
    choice = render_menu("系统工具", [
        ("1", "安装 / 启用 BBR"),
        ("2", "立即备份数据库"),
        ("3", "查看备份列表"),
        ("4", "恢复数据库备份"),
        ("5", "系统设置"),
        ("6", "查看服务日志"),
        ("7", "清空认证历史"),
        ("8", "服务端平滑限速"),
    ], "0-8", return_label="返回上级菜单")
    try:
        if choice == "1":
            install_bbr()
        elif choice == "2":
            backup_db("manual")
        elif choice == "3":
            list_backups()
        elif choice == "4":
            restore_backup()
        elif choice == "5":
            settings_menu()
        elif choice == "6":
            show_logs()
        elif choice == "7":
            clear_auth_history()
        elif choice == "8":
            traffic_control_menu()
        elif choice == "0":
            print("已取消。")
        else:
            print("请输入正确的数字 [0-8]")
    except KeyboardInterrupt:
        print("\n已取消。")
    except Exception as e:
        print(f"错误: {e}")


def menu():
    init_db()
    choice = render_menu("Hysteria2 多用户一键管理脚本", [
        ("1", "安装 Hysteria2"),
        ("2", "更新 Hysteria2 内核"),
        ("3", "卸载 Hysteria2"),
        ("", ""),
        ("4", "用户管理"),
        ("5", "显示在线 IP 和地理位置"),
        ("6", "查看认证历史"),
        ("7", "清零流量"),
        ("", ""),
        ("8", "启动 Hysteria2"),
        ("9", "停止 Hysteria2"),
        ("10", "重启 Hysteria2"),
        ("11", "查看服务状态"),
        ("", ""),
        ("12", "其他功能"),
        ("13", "健康检查"),
    ], "0-13", main=True)
    try:
        if choice == "1":
            install()
        elif choice == "2":
            update_hysteria_core()
        elif choice == "3":
            uninstall()
        elif choice == "4":
            user_menu()
        elif choice == "5":
            show_online()
        elif choice == "6":
            show_auth_history()
        elif choice == "7":
            reset_traffic()
        elif choice == "8":
            start_services()
        elif choice == "9":
            stop_services()
        elif choice == "10":
            restart_services()
        elif choice == "11":
            status()
        elif choice == "12":
            tools_menu()
        elif choice == "13":
            doctor()
        elif choice == "0":
            print("已取消。")
        else:
            print("请输入正确的数字 [0-13]")
    except KeyboardInterrupt:
        print("\n已取消。")
    except Exception as e:
        print(f"错误: {e}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "menu"
    if cmd == "client-config":
        show_client_config(sys.argv[2] if len(sys.argv) > 2 else None, show_user_list=len(sys.argv) <= 2)
        return
    commands = {
        "menu": menu,
        "user-menu": user_menu,
        "traffic-menu": traffic_menu,
        "service-menu": service_menu,
        "tools-menu": tools_menu,
        "install": install,
        "update-core": update_hysteria_core,
        "add-user": add_user,
        "delete-user": delete_user,
        "edit-user": edit_user,
        "list-users": list_users,
        "client-config": show_client_config,
        "online": show_online,
        "auth-history": show_auth_history,
        "clear-auth-history": clear_auth_history,
        "reset-traffic": reset_traffic,
        "backup": backup_db,
        "list-backups": list_backups,
        "restore-backup": restore_backup,
        "settings": settings_menu,
        "traffic-control": traffic_control_menu,
        "logs": show_logs,
        "bbr": install_bbr,
        "doctor": doctor,
        "start": start_services,
        "stop": stop_services,
        "status": status,
        "restart": restart_services,
        "uninstall": uninstall,
        "auth-server": auth_server,
        "monitor": monitor,
    }
    if cmd not in commands:
        print("可用命令: " + ", ".join(sorted(commands)))
        raise SystemExit(2)
    commands[cmd]()


if __name__ == "__main__":
    main()
