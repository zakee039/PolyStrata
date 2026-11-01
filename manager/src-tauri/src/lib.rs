use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    os::windows::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, Position, State, WindowEvent,
};
use winreg::{enums::HKEY_CURRENT_USER, RegKey};
mod security;

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
const RUN_NAME: &str = "PolyStrata Manager";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
struct Preferences {
    autostart: bool,
    auto_load_model: bool,
    manual_model_hold: bool,
    watchdog: bool,
    lan_access: bool,
    cf_enabled: bool,
    cf_binary: String,
    cf_hostname: String,
    registered_configs: Vec<String>,
    config_path: String,
    project_path: String,
    port: u16,
}
impl Default for Preferences {
    fn default() -> Self {
        let root = project_root();
        let one = root
            .parent()
            .unwrap_or(&root)
            .join("benchmark/configs/reserve1024-128-v1.json");
        let two = root.join("strata-gemma4-g4-meromero-26b-a4b-it-uncensored-heretic-q4_k_m.json");
        let config = if one.is_file() { one } else { two };
        Self {
            autostart: false,
            auto_load_model: false,
            manual_model_hold: false,
            watchdog: true,
            lan_access: false,
            cf_enabled: false,
            cf_binary: if Path::new("C:\\Program Files (x86)\\cloudflared\\cloudflared.exe")
                .is_file()
            {
                "C:\\Program Files (x86)\\cloudflared\\cloudflared.exe".into()
            } else {
                "cloudflared.exe".into()
            },
            cf_hostname: String::new(),
            registered_configs: Vec::new(),
            config_path: config.to_string_lossy().to_string(),
            project_path: root.to_string_lossy().to_string(),
            port: 18081,
        }
    }
}

struct Inner {
    prefs: Preferences,
    child: Option<Child>,
    cf_child: Option<Child>,
    cf_error: String,
    cf_latency_ms: Option<u64>,
    cf_test_status: String,
    desired: bool,
    stopping: bool,
    error: String,
    restarts: u8,
    last_restart: Option<Instant>,
}
struct AppState {
    inner: Mutex<Inner>,
}

fn project_root() -> PathBuf {
    if let Ok(p) = std::env::var("POLYSTRATA_HOME") {
        let root = PathBuf::from(p);
        if root.join("serve/server.py").is_file() {
            return root;
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf()
}
fn state_dir() -> PathBuf {
    PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".into()))
        .join("PolyStrataManager")
}
fn log_path() -> PathBuf {
    state_dir().join("service.log")
}
fn settings_file() -> PathBuf {
    state_dir().join("settings.json")
}

fn registry_has_autostart() -> bool {
    RegKey::predef(HKEY_CURRENT_USER)
        .open_subkey(RUN_KEY)
        .and_then(|key| key.get_value::<String, _>(RUN_NAME))
        .is_ok()
}
fn set_autostart(enabled: bool) -> Result<(), String> {
    let (key, _) = RegKey::predef(HKEY_CURRENT_USER)
        .create_subkey(RUN_KEY)
        .map_err(|e| e.to_string())?;
    if enabled {
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        key.set_value(RUN_NAME, &format!("\"{}\" --background", exe.display()))
            .map_err(|e| e.to_string())
    } else {
        match key.delete_value(RUN_NAME) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e.to_string()),
        }
    }
}
fn save_prefs(p: &Preferences) -> Result<(), String> {
    fs::create_dir_all(state_dir()).map_err(|e| e.to_string())?;
    let path = settings_file();
    let tmp = path.with_extension("json.tmp");
    fs::write(
        &tmp,
        serde_json::to_vec_pretty(p).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())?;
    fs::rename(tmp, path).map_err(|e| e.to_string())
}
fn initial_prefs() -> Preferences {
    let mut p: Preferences = fs::read(settings_file())
        .ok()
        .and_then(|data| serde_json::from_slice(&data).ok())
        .unwrap_or_default();
    p.autostart = registry_has_autostart();
    p
}

fn port_open(port: u16) -> bool {
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    TcpStream::connect_timeout(&addr, Duration::from_millis(240)).is_ok()
}
// taskkill /T waits for the Python parent but Windows may leave the child
// listener alive briefly. A new config cannot bind until the old listener
// has actually gone away. Never kill an unknown process on this port.
fn wait_for_port_release(port: u16, timeout: Duration) -> Result<(), String> {
    let started = Instant::now();
    while port_open(port) {
        if started.elapsed() >= timeout {
            return Err(format!(
                "端口 {port} 在安全卸载后仍未释放（等待 {} 秒）。请检查端口占用进程。",
                timeout.as_secs()
            ));
        }
        thread::sleep(Duration::from_millis(150));
    }
    Ok(())
}
fn api_get(port: u16, path: &str) -> Result<Value, String> {
    let key = security::ensure_api_key()?;
    ureq::get(&format!("http://127.0.0.1:{port}{path}"))
        .set("Authorization", &format!("Bearer {key}"))
        .timeout(Duration::from_millis(950))
        .call()
        .map_err(|e| e.to_string())?
        .into_json()
        .map_err(|e| e.to_string())
}
fn api_post(port: u16, path: &str, timeout: u64) -> Result<Value, String> {
    let key = security::ensure_api_key()?;
    ureq::post(&format!("http://127.0.0.1:{port}{path}"))
        .set("Authorization", &format!("Bearer {key}"))
        .set("Origin", &format!("http://127.0.0.1:{port}"))
        .set("Content-Type", "application/json")
        .timeout(Duration::from_secs(timeout))
        .send_json(json!({}))
        .map_err(|e| e.to_string())?
        .into_json()
        .map_err(|e| e.to_string())
}
fn gpu_memory_without_service() -> Option<Value> {
    // Fall back to NVIDIA's system utility only while the API is offline.
    // All numbers are MiB from nvidia-smi; the UI consumes bytes.
    let output = Command::new("nvidia-smi.exe")
        .args([
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let line = String::from_utf8_lossy(&output.stdout);
    let mut cells = line.lines().next()?.split(',').map(str::trim);
    let used = cells.next()?.parse::<u64>().ok()?;
    let total = cells.next()?.parse::<u64>().ok()?;
    Some(json!({"gpu_mem_used": used * 1_048_576, "gpu_mem_total": total * 1_048_576}))
}

fn child_running(child: &mut Child) -> bool {
    matches!(child.try_wait(), Ok(None))
}
fn spawn_backend(p: &Preferences) -> Result<Child, String> {
    if port_open(p.port) {
        return Err(format!("端口 {} 已被占用。请先关闭旧版批处理启动的服务；为安全起见不会接管不属于管理器的进程。", p.port));
    }
    let root = PathBuf::from(&p.project_path);
    let python = root.join(".venv/Scripts/python.exe");
    let server = root.join("serve/server.py");
    if !python.is_file() || !server.is_file() {
        return Err("找不到 PolyStrata 推理服务或其 .venv Python 运行环境".into());
    }
    if !Path::new(&p.config_path).is_file() {
        return Err(format!("部署配置不存在：{}", p.config_path));
    }
    let api_key = security::ensure_api_key()?;
    if p.cf_enabled && security::read("cf-tunnel-token")?.is_none() {
        return Err("启用 Cloudflare Tunnel 前，请先在设置中保存 Tunnel Token".into());
    }
    fs::create_dir_all(state_dir()).map_err(|e| e.to_string())?;
    let mut logfile = OpenOptions::new()
        .append(true)
        .create(true)
        .open(log_path())
        .map_err(|e| e.to_string())?;
    writeln!(
        logfile,
        "\n===== Manager start {:?} =====",
        std::time::SystemTime::now()
    )
    .ok();
    let errlog = logfile.try_clone().map_err(|e| e.to_string())?;
    let mut cmd = Command::new(python);
    cmd.arg(server)
        .arg("--engine")
        .arg("strata")
        .arg("--config")
        .arg(&p.config_path)
        .arg("--port")
        .arg(p.port.to_string())
        .arg("--host")
        .arg(if p.lan_access { "0.0.0.0" } else { "127.0.0.1" })
        .env("STRATA_API_KEY", api_key)
        .env(
            "STRATA_MANUAL_MODEL_HOLD",
            if p.manual_model_hold { "1" } else { "0" },
        )
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::from(logfile))
        .stderr(Stdio::from(errlog))
        .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    if !p.auto_load_model || p.manual_model_hold {
        cmd.arg("--lazy");
    }
    cmd.spawn().map_err(|e| format!("服务启动失败：{e}"))
}
const CF_METRICS: &str = "http://127.0.0.1:23188/metrics";

fn cloudflared_binary(p: &Preferences) -> String {
    let candidates = [
        p.cf_binary.as_str(),
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        "cloudflared.exe",
    ];
    candidates
        .iter()
        .find(|s| {
            !s.trim().is_empty()
                && (Path::new(s).is_file()
                    || Command::new(s)
                        .arg("--version")
                        .creation_flags(CREATE_NO_WINDOW)
                        .output()
                        .is_ok_and(|o| o.status.success()))
        })
        .unwrap_or(&"cloudflared.exe")
        .to_string()
}

fn valid_hostname(value: &str) -> bool {
    let labels: Vec<_> = value.split('.').collect();
    value.len() <= 253
        && labels.len() >= 2
        && !value.eq_ignore_ascii_case("localhost")
        && !value.ends_with(".local")
        && value.parse::<std::net::IpAddr>().is_err()
        && labels.iter().all(|label| {
            !label.is_empty()
                && label.len() <= 63
                && !label.starts_with('-')
                && !label.ends_with('-')
                && label
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
}

fn cf_connection_count() -> Option<u32> {
    let body = ureq::get(CF_METRICS)
        .timeout(Duration::from_millis(450))
        .call()
        .ok()?
        .into_string()
        .ok()?;
    body.lines()
        .filter(|line| !line.starts_with('#'))
        .find_map(|line| {
            let (name, value) = line.split_once(' ')?;
            if !name.starts_with("cloudflared_tunnel_ha_connections") {
                return None;
            }
            value.trim().parse::<f64>().ok().map(|v| v.max(0.0) as u32)
        })
}

// A real end-to-end HTTPS probe. No API key or Tunnel Token ever leaves the PC.
// A matching 401 from PolyStrata proves that Cloudflare reached this backend.
fn cf_probe(hostname: &str) -> Result<(u64, bool, u16), String> {
    if !valid_hostname(hostname) {
        return Err("请输入有效的公开域名，例如 llm.example.com".into());
    }
    let url = format!("https://{hostname}/v1/models");
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_secs(7))
        .redirects(0)
        .build();
    let start = Instant::now();
    let response = agent.get(&url).call();
    let (code, mut body) = match response {
        Ok(resp) => {
            let code = resp.status();
            let mut s = String::new();
            resp.into_reader().take(4096).read_to_string(&mut s).ok();
            (code, s)
        }
        Err(ureq::Error::Status(code, resp)) => {
            let mut s = String::new();
            resp.into_reader().take(4096).read_to_string(&mut s).ok();
            (code, s)
        }
        Err(e) => return Err(format!("无法访问 HTTPS 域名：{e}")),
    };
    let latency = start.elapsed().as_millis().min(100_000) as u64;
    let ok = code == 401 && body.contains("missing or wrong API key");
    body.clear();
    Ok((latency, ok, code))
}

fn spawn_tunnel(p: &Preferences) -> Result<Child, String> {
    let token = security::read("cf-tunnel-token")?
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| "Cloudflare Tunnel Token 未配置".to_string())?;
    let binary = cloudflared_binary(p);
    fs::create_dir_all(state_dir()).map_err(|e| e.to_string())?;
    let file = OpenOptions::new()
        .append(true)
        .create(true)
        .open(state_dir().join("cloudflared.log"))
        .map_err(|e| e.to_string())?;
    let err = file.try_clone().map_err(|e| e.to_string())?;
    let mut cmd = Command::new(binary);
    cmd.args([
        "tunnel",
        "--metrics",
        "127.0.0.1:23188",
        "--no-autoupdate",
        "run",
    ])
    .env("TUNNEL_TOKEN", token)
    .stdin(Stdio::null())
    .stdout(Stdio::from(file))
    .stderr(Stdio::from(err))
    .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    cmd.spawn()
        .map_err(|e| format!("无法启动 cloudflared，请检查安装路径：{e}"))
}

fn stop_tunnel(child: &mut Option<Child>) {
    if let Some(mut p) = child.take() {
        let _ = p.kill();
        let _ = p.wait();
    }
}

fn start_backend(state: &AppState) -> Result<(), String> {
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    if inner.stopping {
        return Err("服务正在停止，请稍后再试".into());
    }
    if let Some(child) = inner.child.as_mut() {
        if child_running(child) {
            inner.desired = true;
            return Ok(());
        }
    }
    // The API service is the resident process; even a failed initial spawn
    // remains "desired" so the watchdog can retry it.
    inner.desired = true;
    let child = match spawn_backend(&inner.prefs) {
        Ok(child) => child,
        Err(e) => {
            inner.error = e.clone();
            return Err(e);
        }
    };
    inner.child = Some(child);
    inner.desired = true;
    inner.error.clear();
    if inner.prefs.cf_enabled {
        stop_tunnel(&mut inner.cf_child);
        let prefs = inner.prefs.clone();
        match spawn_tunnel(&prefs) {
            Ok(tunnel) => {
                inner.cf_child = Some(tunnel);
                inner.cf_error.clear();
            }
            Err(e) => {
                inner.cf_error = e;
            }
        }
    }
    Ok(())
}
fn stop_backend(state: &AppState) -> Result<(), String> {
    let (mut child, port) = {
        let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
        if inner.stopping {
            return Err("正在停止服务".into());
        }
        inner.desired = false;
        stop_tunnel(&mut inner.cf_child);
        let port = inner.prefs.port;
        let Some(child) = inner.child.take() else {
            return Ok(());
        };
        inner.stopping = true;
        (child, port)
    };
    let result = (|| -> Result<(), String> {
        if !child_running(&mut child) {
            return Ok(());
        }
        // Native server saves V1 active KV inside its FIFO before engine unload.
        // If it is busy, do not kill the model: report and let the user retry.
        let status = api_post(port, "/unload?wait=30", 36)
            .map_err(|e| format!("安全保存/卸载 KV 失败，未强行终止服务：{e}"))?;
        if !matches!(status["status"].as_str(), Some("unloaded" | "not loaded")) {
            return Err(format!("引擎仍在处理请求，停止已取消：{status}"));
        }
        let pid = child.id();
        let result = Command::new("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .map_err(|e| e.to_string())?;
        if !result.status.success() && child_running(&mut child) {
            return Err(format!(
                "服务停止失败：{}",
                String::from_utf8_lossy(&result.stderr)
            ));
        }
        let _ = child.wait();
        // A successful process-tree kill is not necessarily the instant the
        // Uvicorn subprocess releases its TCP listener on Windows. This also
        // protects a same-port config switch from an intermittent false error.
        wait_for_port_release(port, Duration::from_secs(12))
    })();
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    inner.stopping = false;
    if result.is_err() && child_running(&mut child) {
        // A failed safe-unload must not silently disable the API watchdog.
        inner.desired = true;
        inner.child = Some(child);
    } else {
        inner.child = None;
    }
    if let Err(ref e) = result {
        inner.error = e.clone();
    }
    result
}

#[tauri::command]
fn start_service(state: State<'_, AppState>) -> Result<(), String> {
    let mut guard = state.inner.lock().map_err(|e| e.to_string())?;
    guard.restarts = 0;
    guard.last_restart = None;
    guard.error.clear();
    drop(guard);
    start_backend(&state)
}

#[tauri::command]
async fn stop_service(app: tauri::AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || stop_backend(&app.state::<AppState>()))
        .await
        .map_err(|e| e.to_string())?
}

fn persist_manual_hold(state: &AppState, held: bool) -> Result<(), String> {
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    let mut updated = inner.prefs.clone();
    updated.manual_model_hold = held;
    save_prefs(&updated)?;
    inner.prefs = updated;
    Ok(())
}

#[tauri::command]
async fn load_model(app: tauri::AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let port = state.inner.lock().map_err(|e| e.to_string())?.prefs.port;
        let value = api_post(port, "/load", 120)?;
        if value["status"] != "loaded" {
            return Err(format!("模型加载失败：{value}"));
        }
        persist_manual_hold(&state, false)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn unload_model(app: tauri::AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let port = state.inner.lock().map_err(|e| e.to_string())?.prefs.port;
        // manual=1 blocks inference-triggered reloads; normal idle unload does not.
        let value = api_post(port, "/unload?wait=30&manual=1", 36)?;
        if !matches!(value["status"].as_str(), Some("unloaded" | "not loaded")) {
            return Err(format!("模型卸载未完成：{value}"));
        }
        persist_manual_hold(&state, true)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn get_status(state: State<'_, AppState>) -> Result<Value, String> {
    let (
        prefs,
        owned,
        stopping,
        error,
        desired,
        restarts,
        cf_running,
        cf_error,
        cf_latency_ms,
        cf_test_status,
    ) = {
        let mut guard = state.inner.lock().map_err(|e| e.to_string())?;
        let owned = guard.child.as_mut().is_some_and(child_running);
        let cf_running = guard.cf_child.as_mut().is_some_and(child_running);
        (
            guard.prefs.clone(),
            owned,
            guard.stopping,
            guard.error.clone(),
            guard.desired,
            guard.restarts,
            cf_running,
            guard.cf_error.clone(),
            guard.cf_latency_ms,
            guard.cf_test_status.clone(),
        )
    };
    let (metrics, host_gpu, cf_connections) = tauri::async_runtime::spawn_blocking(move || {
        let metrics = api_get(prefs.port, "/metrics").ok();
        let gpu = if metrics.is_none() {
            gpu_memory_without_service()
        } else {
            None
        };
        let connections = if cf_running {
            cf_connection_count()
        } else {
            None
        };
        (metrics, gpu, connections)
    })
    .await
    .map_err(|e| e.to_string())?;
    // Also capture explicit model operations made through the local API/Web UI.
    // An API crash/restart must not accidentally erase a user's manual hold.
    if owned {
        if let Some(actual) = metrics
            .as_ref()
            .and_then(|m| m["live"]["manual_hold"].as_bool())
        {
            let saved = state
                .inner
                .lock()
                .map_err(|e| e.to_string())?
                .prefs
                .manual_model_hold;
            if saved != actual {
                persist_manual_hold(&state, actual)?;
            }
        }
    }
    let phase = if stopping {
        "stopping"
    } else if owned && metrics.is_some() {
        metrics
            .as_ref()
            .and_then(|m| m["live"]["state"].as_str())
            .unwrap_or("idle")
    } else if owned {
        "starting"
    } else if metrics.is_some() {
        "external"
    } else if !error.is_empty() {
        "error"
    } else {
        "stopped"
    };
    Ok(
        json!({"phase":phase, "managed":owned, "desired":desired, "error":error,
        "restarts":restarts,"settings":state.inner.lock().map_err(|e| e.to_string())?.prefs.clone(),
        "metrics":metrics,"host_gpu":host_gpu,"cf_running":cf_running,
        "cf_connections":cf_connections,"cf_latency_ms":cf_latency_ms,"cf_test_status":cf_test_status,
        "cf_error":cf_error, "cf_token_present":security::read("cf-tunnel-token")?.is_some(),
        "api_key_present":security::read("api-key")?.is_some()}),
    )
}

#[tauri::command]
fn reveal_api_key() -> Result<String, String> {
    security::ensure_api_key()
}

#[tauri::command]
fn replace_api_key(state: State<'_, AppState>, new_key: String) -> Result<(), String> {
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    if inner.child.as_mut().is_some_and(child_running) {
        return Err("更换 API Key 前请先停止服务，避免在运行中断开现有客户端".into());
    }
    let key = new_key.trim();
    if key.len() < 24
        || key.len() > 256
        || !key
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_.".contains(c))
    {
        return Err("API Key 应为 24～256 位英数字、短横线、下划线或点号".into());
    }
    security::store("api-key", key)
}

#[tauri::command]
fn generate_api_key(state: State<'_, AppState>) -> Result<String, String> {
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    if inner.child.as_mut().is_some_and(child_running) {
        return Err("更换 API Key 前请先停止服务".into());
    }
    let key = security::random_api_key()?;
    security::store("api-key", &key)?;
    Ok(key)
}

#[tauri::command]
fn save_tunnel_token(state: State<'_, AppState>, token: String) -> Result<(), String> {
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    if inner.child.as_mut().is_some_and(child_running) {
        return Err("更新 Tunnel Token 前请先停止服务".into());
    }
    let token = token.trim();
    if token.len() < 50
        || token.len() > 4096
        || !token
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "-_=.".contains(c))
    {
        return Err(
            "Tunnel Token 格式不正确：请从 Cloudflare Tunnel 的“Add a replica”复制 eyJ... Token"
                .into(),
        );
    }
    security::store("cf-tunnel-token", token)
}

fn ensure_cloudflared_installed(p: &Preferences) -> Result<String, String> {
    let candidate = cloudflared_binary(p);
    if Command::new(&candidate)
        .arg("--version")
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .is_ok_and(|r| r.status.success())
    {
        return Ok(candidate);
    }
    let result = Command::new("winget.exe")
        .args([
            "install",
            "--id",
            "Cloudflare.cloudflared",
            "--exact",
            "--source",
            "winget",
            "--silent",
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--disable-interactivity",
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|e| format!("无法启动 Windows 软件安装器：{e}"))?;
    if !result.status.success() {
        return Err(
            "cloudflared 安装失败，请检查 winget 或手动安装 Cloudflare Tunnel 客户端".into(),
        );
    }
    let fresh = cloudflared_binary(p);
    if Command::new(&fresh)
        .arg("--version")
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .is_ok_and(|r| r.status.success())
    {
        Ok(fresh)
    } else {
        Err("安装完成，但未发现 cloudflared.exe；请重新启动 Manager".into())
    }
}

#[tauri::command]
async fn tunnel_install(
    app: tauri::AppHandle,
    hostname: String,
    token: String,
) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let hostname = hostname.trim().trim_end_matches('.').to_ascii_lowercase();
        if !valid_hostname(&hostname) {
            return Err("请输入有效的 Tunnel 公网域名".into());
        }
        let state = app.state::<AppState>();
        let prefs = state.inner.lock().map_err(|e| e.to_string())?.prefs.clone();
        let binary = ensure_cloudflared_installed(&prefs)?;
        if token.trim().is_empty() {
            if security::read("cf-tunnel-token")?.is_none() {
                return Err("请输入 Cloudflare Tunnel Token".into());
            }
        } else {
            let token = token.trim();
            if token.len() < 50
                || token.len() > 4096
                || !token
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_=.".contains(&b))
            {
                return Err("Tunnel Token 格式无效".into());
            }
            security::store("cf-tunnel-token", token)?;
        }
        let mut guard = state.inner.lock().map_err(|e| e.to_string())?;
        let mut updated = guard.prefs.clone();
        updated.cf_enabled = true;
        updated.cf_hostname = hostname;
        updated.cf_binary = binary;
        save_prefs(&updated)?;
        guard.prefs = updated.clone();
        guard.cf_latency_ms = None;
        guard.cf_test_status.clear();
        if guard.child.as_mut().is_some_and(child_running) {
            stop_tunnel(&mut guard.cf_child);
            match spawn_tunnel(&updated) {
                Ok(child) => {
                    guard.cf_child = Some(child);
                    guard.cf_error.clear();
                }
                Err(e) => {
                    guard.cf_error = e.clone();
                    return Err(e);
                }
            }
            Ok("Tunnel 已配置并启动连接器".into())
        } else {
            Ok("Tunnel 已安装，启动推理服务后自动连接".into())
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn tunnel_test(app: tauri::AppHandle) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let hostname = state
            .inner
            .lock()
            .map_err(|e| e.to_string())?
            .prefs
            .cf_hostname
            .clone();
        let result = cf_probe(&hostname);
        let mut guard = state.inner.lock().map_err(|e| e.to_string())?;
        match result {
            Ok((ms, ok, code)) => {
                guard.cf_latency_ms = Some(ms);
                guard.cf_test_status = if ok {
                    "外网可达".into()
                } else {
                    format!("HTTP {code} · 路由未验证")
                };
                Ok(json!({"reachable":ok,"http_status":code,"latency_ms":ms,
                           "message":guard.cf_test_status}))
            }
            Err(e) => {
                guard.cf_latency_ms = None;
                guard.cf_test_status = e.clone();
                Err(e)
            }
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn tunnel_restart(app: tauri::AppHandle) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let mut guard = state.inner.lock().map_err(|e| e.to_string())?;
        if !guard.prefs.cf_enabled {
            return Err("请先安装 Tunnel".into());
        }
        if !guard.child.as_mut().is_some_and(child_running) {
            return Err("推理服务未运行；启动服务后才能重启 Tunnel".into());
        }
        stop_tunnel(&mut guard.cf_child);
        guard.cf_latency_ms = None;
        guard.cf_test_status.clear();
        let prefs = guard.prefs.clone();
        match spawn_tunnel(&prefs) {
            Ok(child) => {
                guard.cf_child = Some(child);
                guard.cf_error.clear();
                Ok("连接器已重新启动".into())
            }
            Err(e) => {
                guard.cf_error = e.clone();
                Err(e)
            }
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
fn get_settings(state: State<'_, AppState>) -> Result<Preferences, String> {
    Ok(state.inner.lock().map_err(|e| e.to_string())?.prefs.clone())
}

#[tauri::command]
async fn save_settings(
    app: tauri::AppHandle,
    incoming: Preferences,
) -> Result<Preferences, String> {
    tauri::async_runtime::spawn_blocking(move || {
        if incoming.port < 1024 || incoming.config_path.trim().is_empty() {
            return Err("端口必须大于等于 1024，配置路径不能为空".into());
        }
        if !Path::new(&incoming.config_path).is_file() {
            return Err("配置文件不存在".into());
        }
        if !Path::new(&incoming.project_path)
            .join("serve/server.py")
            .is_file()
            || !Path::new(&incoming.project_path)
                .join(".venv/Scripts/python.exe")
                .is_file()
        {
            return Err(
                "部署目录不正确：必须包含 serve/server.py 和 .venv/Scripts/python.exe".into(),
            );
        }
        if incoming.cf_enabled && security::read("cf-tunnel-token")?.is_none() {
            return Err("请先保存 Cloudflare Tunnel Token，再启用 Tunnel".into());
        }
        let state = app.state::<AppState>();
        let (old, running) = {
            let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
            (
                inner.prefs.clone(),
                inner.child.as_mut().is_some_and(child_running),
            )
        };
        let needs_restart = incoming.config_path != old.config_path
            || incoming.project_path != old.project_path
            || incoming.port != old.port
            || incoming.lan_access != old.lan_access
            || incoming.cf_enabled != old.cf_enabled
            || incoming.cf_binary != old.cf_binary;
        if running && needs_restart {
            // Always persist active KV before changing the model or listener.
            if api_get(old.port, "/metrics").is_err() {
                return Err("API 尚未准备好，请稍后再切换配置".into());
            }
            stop_backend(&state)?;
        }
        let mut updated = incoming;
        // Runtime model hold is controlled only by model actions, not stale UI settings.
        updated.manual_model_hold = state
            .inner
            .lock()
            .map_err(|e| e.to_string())?
            .prefs
            .manual_model_hold;
        // Only user-registered configurations; do not rediscover unregistered JSONs.
        updated.registered_configs.sort_by_key(|x| x.to_lowercase());
        updated
            .registered_configs
            .dedup_by(|a, b| a.eq_ignore_ascii_case(b));
        let apply = (|| -> Result<(), String> {
            if updated.autostart != old.autostart {
                set_autostart(updated.autostart)?;
            }
            save_prefs(&updated)?;
            state.inner.lock().map_err(|e| e.to_string())?.prefs = updated.clone();
            Ok(())
        })();
        if let Err(e) = apply {
            if running && needs_restart {
                let _ = start_backend(&state);
            }
            return Err(e);
        }
        if running && needs_restart {
            // Manager owns the API lifespan; configuration changes restart the
            // API gracefully without forcing an active inference to end.
            start_backend(&state)?;
        }
        Ok(updated)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[derive(Serialize)]
struct ModelConfig {
    name: String,
    path: String,
    model: String,
    context: Option<u64>,
    quantization: Option<String>,
    engine: Option<String>,
    tokenizer: Option<String>,
    draft_model: Option<String>,
    vision_projector: Option<String>,
    kv_type: Option<String>,
    vram_reserve_mib: Option<u64>,
    idle_unload_s: Option<u64>,
    conversation_cache_mib: Option<String>,
}

fn config_arg<'a>(info: &'a Value, flag: &str) -> Option<&'a str> {
    info["args"]
        .as_array()?
        .windows(2)
        .find(|pair| pair[0].as_str() == Some(flag))
        .and_then(|pair| pair[1].as_str())
}

fn model_config_from_json(path: &Path, info: &Value) -> Option<ModelConfig> {
    let engine = info["exe"].as_str()?.to_string();
    let model = config_arg(info, "--model")
        .or_else(|| config_arg(info, "--native"))
        .unwrap_or_default()
        .to_string();
    let name = info["display_name"]
        .as_str()
        .or_else(|| info["model_name"].as_str())
        .unwrap_or("PolyStrata Model")
        .to_string();
    let context = config_arg(info, "--max-context").and_then(|s| s.parse::<u64>().ok());
    let kv_type = config_arg(info, "--kv").map(str::to_string);
    let quantization = Path::new(&model)
        .file_stem()
        .and_then(|s| s.to_str())
        .and_then(|s| s.rsplit_once("-Q").map(|(_, quant)| {
            // Multi-shard GGUFs end with "-00001-of-00002"; keep only the quant.
            format!("Q{}", quant.split("-00001-").next().unwrap_or(quant))
        }));
    Some(ModelConfig {
        name,
        path: path.to_string_lossy().to_string(),
        model,
        context,
        quantization,
        engine: Some(engine),
        tokenizer: info["tokenizer"].as_str().map(str::to_string),
        draft_model: config_arg(info, "--draft-model").map(str::to_string),
        vision_projector: info["vision"]["mmproj"].as_str().map(str::to_string),
        kv_type,
        vram_reserve_mib: config_arg(info, "--vram-reserve-mib").and_then(|s| s.parse().ok()),
        idle_unload_s: info["idle_unload_s"].as_u64(),
        conversation_cache_mib: config_arg(info, "--conversation-cache-mib").map(str::to_string),
    })
}

#[tauri::command]
fn register_config(state: State<'_, AppState>, path: String) -> Result<ModelConfig, String> {
    let file = PathBuf::from(path)
        .canonicalize()
        .map_err(|e| e.to_string())?;
    if !file
        .extension()
        .is_some_and(|x| x.eq_ignore_ascii_case("json"))
    {
        return Err("请选择 JSON 部署配置文件".into());
    }
    let data = fs::read(&file).map_err(|e| e.to_string())?;
    if data.len() > 2_000_000 {
        return Err("配置文件体积异常，请检查是否选错".into());
    }
    let value: Value = serde_json::from_slice(&data).map_err(|e| format!("JSON 无效：{e}"))?;
    let config = model_config_from_json(&file, &value)
        .ok_or_else(|| "不是 PolyStrata 部署配置：缺少 exe 字段".to_string())?;
    if config.model.is_empty() {
        return Err("配置缺少 --model 参数，无法注册".into());
    }
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    if !inner
        .prefs
        .registered_configs
        .iter()
        .any(|x| x.eq_ignore_ascii_case(&config.path))
    {
        let mut prefs = inner.prefs.clone();
        prefs.registered_configs.push(config.path.clone());
        save_prefs(&prefs)?;
        inner.prefs = prefs;
    }
    Ok(config)
}

#[tauri::command]
fn list_models(state: State<'_, AppState>) -> Result<Vec<ModelConfig>, String> {
    let prefs = state.inner.lock().map_err(|e| e.to_string())?.prefs.clone();
    let selected = prefs.config_path.clone();
    // The chooser lists registered configurations only. Scanning the project
    // directory used to bring back unregistered benchmark/source JSON files.
    let mut seen = std::collections::HashSet::new();
    let mut paths = vec![PathBuf::from(&selected)];
    paths.extend(prefs.registered_configs.into_iter().map(PathBuf::from));
    let mut models = Vec::new();
    for path in paths {
        if !path.is_file() || !seen.insert(path.to_string_lossy().to_ascii_lowercase()) {
            continue;
        }
        if let Ok(data) = fs::read(&path) {
            if let Ok(info) = serde_json::from_slice::<Value>(&data) {
                if info.get("exe").is_none() {
                    continue;
                }
                if let Some(config) = model_config_from_json(&path, &info) {
                    models.push(config);
                }
            }
        }
    }
    Ok(models)
}

#[tauri::command]
fn get_log() -> String {
    let mut buf = String::new();
    if let Ok(mut file) = File::open(log_path()) {
        let _ = file.read_to_string(&mut buf);
    }
    let len = buf.len();
    if len > 18000 {
        buf[buf.floor_char_boundary(len - 18000)..].to_string()
    } else {
        buf
    }
}

#[tauri::command]
fn open_chat(state: State<'_, AppState>) -> Result<(), String> {
    let port = state.inner.lock().map_err(|e| e.to_string())?.prefs.port;
    open_url(port)
}
fn open_url(port: u16) -> Result<(), String> {
    Command::new("rundll32.exe")
        .args([
            "url.dll,FileProtocolHandler",
            &format!("http://127.0.0.1:{port}/"),
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}

fn show(app: &tauri::AppHandle, position: Option<(i32, i32)>) {
    if let Some(w) = app.get_webview_window("main") {
        if let Some((x, y)) = position {
            if let (Ok(Some(monitor)), Ok(size)) = (w.current_monitor(), w.outer_size()) {
                let mpos = monitor.position();
                let msize = monitor.size();
                let mw = msize.width as i32;
                let mh = msize.height as i32;
                let ww = size.width as i32;
                let wh = size.height as i32;
                // Tray events contain physical screen coordinates; use physical window
                // dimensions too (important on 125%/150% HiDPI desktops).
                let px = (x - ww + 16).clamp(mpos.x, (mpos.x + mw - ww).max(mpos.x));
                let py = (y - wh - 12).clamp(mpos.y, (mpos.y + mh - wh).max(mpos.y));
                let _ = w.set_position(Position::Physical(tauri::PhysicalPosition::new(px, py)));
            }
        }
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn watchdog(app: tauri::AppHandle) {
    thread::spawn(move || loop {
        thread::sleep(Duration::from_secs(2));
        let state = app.state::<AppState>();
        let restart = {
            let mut guard = match state.inner.lock() {
                Ok(x) => x,
                Err(_) => return,
            };
            if guard.stopping {
                false
            } else {
                let exited = guard.child.as_mut().is_some_and(|x| !child_running(x));
                if exited {
                    guard.child = None;
                    guard.error = "推理服务意外退出，尝试恢复".into();
                }
                // Failed restart attempts are retried: a just-crashed TCP listener can
                // remain bound briefly after the Python process has exited.
                let ready = guard.last_restart.is_none_or(|t| {
                    t.elapsed() >= Duration::from_secs(2_u64.pow(u32::from(guard.restarts).min(4)))
                });
                let retry = guard.desired
                    && guard.child.is_none()
                    && guard.prefs.watchdog
                    && guard.restarts < 3
                    && ready;
                if retry {
                    guard.restarts += 1;
                    guard.last_restart = Some(Instant::now());
                } else if guard.desired && guard.child.is_none() && guard.restarts >= 3 {
                    guard.error = "服务反复退出，守护已停止重试（3/3），请检查日志".into();
                }
                retry
            }
        };
        if restart {
            if let Err(e) = start_backend(&state) {
                if let Ok(mut guard) = state.inner.lock() {
                    guard.error = format!("守护恢复失败：{e}");
                }
                let _ = app.emit("manager-error", e);
            }
        }
    });
}

pub fn self_test() -> i32 {
    let mut prefs = initial_prefs();
    // --self-test probes API lifecycle without loading GPU weights.
    // --self-test-full additionally validates real GPU model preloading.
    let full = std::env::args().any(|arg| arg == "--self-test-full");
    let model_toggle = std::env::args().any(|arg| arg == "--self-test-model");
    prefs.auto_load_model = full;
    prefs.manual_model_hold = false;
    prefs.lan_access = false;
    prefs.cf_enabled = false;
    let state = AppState {
        inner: Mutex::new(Inner {
            prefs: prefs.clone(),
            child: None,
            cf_child: None,
            cf_error: String::new(),
            cf_latency_ms: None,
            cf_test_status: String::new(),
            desired: false,
            stopping: false,
            error: String::new(),
            restarts: 0,
            last_restart: None,
        }),
    };
    let mut report = json!({"test":"manager start/stop", "port":prefs.port, "lazy":!full,
        "full_model_test":full, "model_toggle_test":model_toggle,
        "gpu_without_service":gpu_memory_without_service()});
    let result: Result<(), String> = (|| {
        start_backend(&state)?;
        let mut metrics = None;
        for _ in 0..if full { 240 } else { 60 } {
            thread::sleep(Duration::from_millis(500));
            if let Ok(data) = api_get(prefs.port, "/metrics") {
                if data.get("live").is_some() {
                    metrics = Some(data);
                    break;
                }
            }
        }
        let data = metrics.ok_or_else(|| "API did not become ready in 30s".to_string())?;
        report["model_state"] = data["live"]["state"].clone();
        report["api_started"] = json!(true);
        let expected = if full { "idle" } else { "unloaded" };
        if data["live"]["state"].as_str() != Some(expected) {
            return Err(format!(
                "Expected model state {expected}, got {}",
                data["live"]["state"]
            ));
        }
        if model_toggle {
            let loaded = api_post(prefs.port, "/load", 180)?;
            if loaded["status"] != "loaded" {
                return Err(format!("model LOAD failed: {loaded}"));
            }
            report["model_loaded"] = json!(true);
            let unloaded = api_post(prefs.port, "/unload?wait=30", 40)?;
            if !matches!(unloaded["status"].as_str(), Some("unloaded" | "not loaded")) {
                return Err(format!("model UNLOAD failed: {unloaded}"));
            }
            if !port_open(prefs.port) || api_get(prefs.port, "/metrics").is_err() {
                return Err("API went offline after model-only UNLOAD".into());
            }
            report["model_unloaded_api_online"] = json!(true);
        }
        stop_backend(&state)?;
        thread::sleep(Duration::from_millis(500));
        if port_open(prefs.port) {
            return Err("API port was still open after stop".into());
        }
        report["api_stopped"] = json!(true);
        Ok(())
    })();
    if result.is_err() {
        // Ensure a failed stop is visible; never force-kill an in-flight request.
        report["error"] = json!(result.as_ref().err().unwrap());
        let _ = stop_backend(&state);
    }
    report["passed"] = json!(result.is_ok());
    let _ = fs::create_dir_all(state_dir());
    let _ = fs::write(
        state_dir().join("self-test.json"),
        serde_json::to_vec_pretty(&report).unwrap_or_default(),
    );
    if result.is_ok() {
        0
    } else {
        1
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app, None);
        }))
        .manage(AppState {
            inner: Mutex::new(Inner {
                prefs: initial_prefs(),
                child: None,
                cf_child: None,
                cf_error: String::new(),
                cf_latency_ms: None,
                cf_test_status: String::new(),
                desired: false,
                stopping: false,
                error: String::new(),
                restarts: 0,
                last_restart: None,
            }),
        })
        .invoke_handler(tauri::generate_handler![
            start_service,
            stop_service,
            load_model,
            unload_model,
            get_status,
            get_settings,
            save_settings,
            reveal_api_key,
            replace_api_key,
            generate_api_key,
            save_tunnel_token,
            tunnel_install,
            tunnel_test,
            tunnel_restart,
            list_models,
            register_config,
            get_log,
            open_chat
        ])
        .setup(|app| {
            let app_handle = app.handle().clone();
            let menu = Menu::with_items(
                app,
                &[
                    &MenuItem::with_id(app, "open", "打开控制面板", true, None::<&str>)?,
                    &MenuItem::with_id(app, "load", "加载模型", true, None::<&str>)?,
                    &MenuItem::with_id(app, "unload", "卸载模型", true, None::<&str>)?,
                    &MenuItem::with_id(app, "chat", "打开 Chat", true, None::<&str>)?,
                    &MenuItem::with_id(app, "quit", "退出管理器", true, None::<&str>)?,
                ],
            )?;
            TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("PolyStrata Manager")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| {
                    let id = event.id().as_ref();
                    match id {
                        "open" => show(app, None),
                        "load" | "unload" => {
                            let app2 = app.clone();
                            let loading = id == "load";
                            thread::spawn(move || {
                                let port = match app2.state::<AppState>().inner.lock() {
                                    Ok(guard) => guard.prefs.port,
                                    Err(e) => {
                                        let _ = app2.emit("manager-error", e.to_string());
                                        return;
                                    }
                                };
                                let response = if loading {
                                    api_post(port, "/load", 120)
                                } else {
                                    api_post(port, "/unload?wait=30&manual=1", 36)
                                };
                                match response {
                                    Ok(value)
                                        if (loading && value["status"] == "loaded")
                                            || (!loading
                                                && matches!(
                                                    value["status"].as_str(),
                                                    Some("unloaded" | "not loaded")
                                                )) =>
                                    {
                                        if let Err(e) =
                                            persist_manual_hold(&app2.state::<AppState>(), !loading)
                                        {
                                            let _ = app2.emit("manager-error", e);
                                        }
                                    }
                                    Ok(value) => {
                                        let _ = app2.emit(
                                            "manager-error",
                                            format!("模型操作失败：{value}"),
                                        );
                                    }
                                    Err(e) => {
                                        let _ = app2.emit("manager-error", e);
                                    }
                                }
                            });
                        }
                        "chat" => {
                            let state = app.state::<AppState>();
                            if let Ok(g) = state.inner.lock() {
                                let _ = open_url(g.prefs.port);
                            };
                        }
                        "quit" => {
                            let app2 = app.clone();
                            thread::spawn(move || {
                                let result = stop_backend(&app2.state::<AppState>());
                                if result.is_ok() {
                                    app2.exit(0);
                                } else if let Err(e) = result {
                                    let _ = app2.emit("manager-error", e);
                                    show(&app2, None);
                                }
                            });
                        }
                        _ => {}
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::DoubleClick {
                        button: MouseButton::Left,
                        position,
                        ..
                    } = event
                    {
                        show(
                            tray.app_handle(),
                            Some((position.x as i32, position.y as i32)),
                        );
                    }
                })
                .build(app)?;
            if std::env::args().any(|x| x == "--background") {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
            }
            watchdog(app_handle.clone());
            // Always start the API and keep it resident while Manager is alive.
            // auto_load_model only decides whether the heavyweight engine is
            // loaded immediately or starts lazily without occupying VRAM.
            thread::spawn(move || {
                if let Err(e) = start_backend(&app_handle.state::<AppState>()) {
                    let _ = app_handle.emit("manager-error", e);
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("PolyStrata Manager startup failed");
}
