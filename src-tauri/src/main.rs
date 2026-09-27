use tauri::Manager;
use tauri_plugin_shell::ShellExt;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let (_events, child) = app
                .shell()
                .sidecar("email-checker-backend")
                .expect("desktop backend sidecar was not bundled")
                .spawn()
                .expect("failed to start desktop backend sidecar");
            app.manage(child);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running desktop application")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit { .. }) {
                if let Some(child) = app.try_state::<tauri_plugin_shell::process::CommandChild>() {
                    let _ = child.kill();
                }
            }
        });
}
