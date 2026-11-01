#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
fn main() {
    if std::env::args()
        .any(|arg| arg == "--self-test" || arg == "--self-test-full" || arg == "--self-test-model")
    {
        std::process::exit(polystrata_manager_lib::self_test());
    }
    polystrata_manager_lib::run();
}
