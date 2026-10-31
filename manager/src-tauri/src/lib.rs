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

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
const RUN_KEY: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
const RUN_NAME: &str = "PolyStrata Manager";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
struct Preferences {
    autostart: bool,
    auto_load_model: bool,
    watchdog: bool,
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
            watchdog: true,
            config_path: config.to_string_lossy().to_string(),
            project_path: root.to_string_lossy().to_string(),
            port: 18081,
        }
    }
}

struct Inner {
    prefs: Preferences,
    child: Option<Child>,
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
fn api_get(port: u16, path: &str) -> Result<Value, String> {
    ureq::get(&format!("http://127.0.0.1:{port}{path}"))
        .timeout(Duration::from_millis(950))
        .call()
        .map_err(|e| e.to_string())?
        .into_json()
        .map_err(|e| e.to_string())
}
fn api_post(port: u16, path: &str, timeout: u64) -> Result<Value, String> {
    ureq::post(&format!("http://127.0.0.1:{port}{path}"))
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
        .arg("127.0.0.1")
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::from(logfile))
        .stderr(Stdio::from(errlog))
        .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    if !p.auto_load_model {
        cmd.arg("--lazy");
    }
    cmd.spawn().map_err(|e| format!("服务启动失败：{e}"))
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
    Ok(())
}
fn stop_backend(state: &AppState) -> Result<(), String> {
    let (mut child, port) = {
        let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
        if inner.stopping {
            return Err("正在停止服务".into());
        }
        inner.desired = false;
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
        Ok(())
    })();
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    inner.stopping = false;
    if result.is_err() && child_running(&mut child) {
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

#[tauri::command]
async fn load_model(state: State<'_, AppState>) -> Result<(), String> {
    let port = state.inner.lock().map_err(|e| e.to_string())?.prefs.port;
    tauri::async_runtime::spawn_blocking(move || {
        let value = api_post(port, "/load", 120)?;
        if value["status"] == "loaded" {
            Ok(())
        } else {
            Err(format!("模型加载失败：{value}"))
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn get_status(state: State<'_, AppState>) -> Result<Value, String> {
    let (prefs, owned, stopping, error, desired, restarts) = {
        let mut guard = state.inner.lock().map_err(|e| e.to_string())?;
        let owned = guard.child.as_mut().is_some_and(child_running);
        (
            guard.prefs.clone(),
            owned,
            guard.stopping,
            guard.error.clone(),
            guard.desired,
            guard.restarts,
        )
    };
    let (metrics, host_gpu) = tauri::async_runtime::spawn_blocking(move || {
        let metrics = api_get(prefs.port, "/metrics").ok();
        let gpu = if metrics.is_none() {
            gpu_memory_without_service()
        } else {
            None
        };
        (metrics, gpu)
    })
    .await
    .map_err(|e| e.to_string())?;
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
        "metrics":metrics,"host_gpu":host_gpu}),
    )
}

#[tauri::command]
fn get_settings(state: State<'_, AppState>) -> Result<Preferences, String> {
    Ok(state.inner.lock().map_err(|e| e.to_string())?.prefs.clone())
}

#[tauri::command]
fn save_settings(state: State<'_, AppState>, incoming: Preferences) -> Result<Preferences, String> {
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
        return Err("部署目录不正确：必须包含 serve/server.py 和 .venv/Scripts/python.exe".into());
    }
    let mut inner = state.inner.lock().map_err(|e| e.to_string())?;
    let is_running = inner.child.as_mut().is_some_and(child_running);
    if is_running
        && (incoming.config_path != inner.prefs.config_path
            || incoming.project_path != inner.prefs.project_path
            || incoming.port != inner.prefs.port)
    {
        return Err("停止服务后才能切换启动配置或端口".into());
    }
    if incoming.autostart != inner.prefs.autostart {
        set_autostart(incoming.autostart)?;
    }
    save_prefs(&incoming)?;
    inner.prefs = incoming.clone();
    Ok(incoming)
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
    let model = config_arg(info, "--model").unwrap_or_default().to_string();
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
        .and_then(|s| s.rsplit_once("-Q").map(|(_, quant)| format!("Q{quant}")));
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
fn list_models(state: State<'_, AppState>) -> Result<Vec<ModelConfig>, String> {
    let selected = state
        .inner
        .lock()
        .map_err(|e| e.to_string())?
        .prefs
        .config_path
        .clone();
    let root = PathBuf::from(
        &state
            .inner
            .lock()
            .map_err(|e| e.to_string())?
            .prefs
            .project_path,
    );
    let dirs = [
        root.clone(),
        root.parent().unwrap_or(&root).join("benchmark/configs"),
        PathBuf::from(&selected)
            .parent()
            .unwrap_or(&root)
            .to_path_buf(),
    ];
    let mut seen = std::collections::HashSet::new();
    let mut paths = vec![PathBuf::from(&selected)];
    for dir in dirs {
        if let Ok(entries) = fs::read_dir(&dir) {
            for entry in entries.flatten() {
                let p = entry.path();
                let prefix = p
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .starts_with("strata-");
                // The active deployment may have a custom filename and is already
                // inserted above. Do not flood the registry with benchmark JSONs.
                if p.extension().is_some_and(|x| x == "json") && prefix {
                    paths.push(p);
                }
            }
        }
    }
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
    prefs.auto_load_model = full;
    let state = AppState {
        inner: Mutex::new(Inner {
            prefs: prefs.clone(),
            child: None,
            desired: false,
            stopping: false,
            error: String::new(),
            restarts: 0,
            last_restart: None,
        }),
    };
    let mut report = json!({"test":"manager start/stop", "port":prefs.port, "lazy":!full,
        "full_model_test":full, "gpu_without_service":gpu_memory_without_service()});
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
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show(app, None);
        }))
        .manage(AppState {
            inner: Mutex::new(Inner {
                prefs: initial_prefs(),
                child: None,
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
            get_status,
            get_settings,
            save_settings,
            list_models,
            get_log,
            open_chat
        ])
        .setup(|app| {
            let app_handle = app.handle().clone();
            let menu = Menu::with_items(
                app,
                &[
                    &MenuItem::with_id(app, "open", "打开控制面板", true, None::<&str>)?,
                    &MenuItem::with_id(app, "start", "启动服务", true, None::<&str>)?,
                    &MenuItem::with_id(app, "stop", "停止服务", true, None::<&str>)?,
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
                        "start" => {
                            if let Err(e) = start_backend(&app.state::<AppState>()) {
                                let _ = app.emit("manager-error", e);
                                show(app, None);
                            }
                        }
                        "stop" => {
                            let app2 = app.clone();
                            thread::spawn(move || {
                                let result = stop_backend(&app2.state::<AppState>());
                                if let Err(e) = result {
                                    let _ = app2.emit("manager-error", e);
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
            if app_handle
                .state::<AppState>()
                .inner
                .lock()
                .unwrap()
                .prefs
                .auto_load_model
            {
                thread::spawn(move || {
                    let _ = start_backend(&app_handle.state::<AppState>());
                });
            }
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
