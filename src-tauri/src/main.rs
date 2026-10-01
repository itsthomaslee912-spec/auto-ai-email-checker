use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;

use tauri::Manager;
use tauri_plugin_shell::ShellExt;

struct PostgresPaths {
    control: PathBuf,
    data: PathBuf,
}

struct BackendChild(Mutex<Option<tauri_plugin_shell::process::CommandChild>>);

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let resource_dir = app.path().resource_dir()?;
            let app_data_dir = app.path().app_data_dir()?;
            std::fs::create_dir_all(&app_data_dir)?;
            let postgres_root = resource_dir.join("postgresql");
            let (_events, child) = app
                .shell()
                .sidecar("email-checker-backend")
                .expect("desktop backend sidecar was not bundled")
                .env("AUTO_EMAIL_POSTGRES_ROOT", postgres_root.as_os_str())
                .env("AUTO_EMAIL_DATA_DIR", app_data_dir.as_os_str())
                .env("AUTO_EMAIL_POSTGRES_PORT", "55432")
                .spawn()
                .expect("failed to start desktop backend sidecar");
            app.manage(BackendChild(Mutex::new(Some(child))));
            app.manage(PostgresPaths {
                control: postgres_root.join("bin").join("pg_ctl.exe"),
                data: app_data_dir.join("postgres-data"),
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running desktop application")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit { .. }) {
                if let Some(backend) = app.try_state::<BackendChild>() {
                    if let Ok(mut child) = backend.0.lock() {
                        if let Some(child) = child.take() {
                            let _ = child.kill();
                        }
                    }
                }
                if let Some(paths) = app.try_state::<PostgresPaths>() {
                    let _ = Command::new(&paths.control)
                        .args([
                            "-D",
                            paths.data.to_string_lossy().as_ref(),
                            "stop",
                            "-m",
                            "fast",
                        ])
                        .creation_flags(0x08000000)
                        .status();
                }
            }
        });
}
