fn main() {
    let pager = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../vendor/grok-build/crates/codegen/xai-grok-pager/Cargo.toml");
    if !pager.is_file() {
        panic!(
            "desmos-tui hosts grok-build ScrollbackPane. clone grok-build into vendor/grok-build"
        );
    }
    // `--version` has to say which build is running: a stale debug binary and
    // a fresh release one are otherwise indistinguishable from the outside.
    let profile = std::env::var("PROFILE").unwrap_or_else(|_| "unknown".into());
    println!("cargo:rustc-env=DESMOS_PROFILE={profile}");
}
