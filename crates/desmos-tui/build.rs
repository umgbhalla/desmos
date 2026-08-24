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

    // Which source this binary came from. A profile alone cannot tell a
    // front built two commits ago from the one that ships the fix you are
    // looking for, and the answer has to come from the running image --
    // comparing file mtimes guesses, and guesses wrongly after a rebuild.
    let git = |args: &[&str]| -> Option<String> {
        let out = std::process::Command::new("git").args(args).output().ok()?;
        out.status.success().then(|| {
            String::from_utf8_lossy(&out.stdout).trim().to_string()
        })
    };
    let mut commit = git(&["rev-parse", "--short", "HEAD"]).unwrap_or_else(|| "unknown".into());
    // Tracked changes only. Untracked files -- notes, scratch, a downloaded
    // book -- do not go into the binary, and counting them marks every build
    // dirty forever, which makes the stamp stop meaning anything.
    if git(&["status", "--porcelain", "--untracked-files=no"]).is_some_and(|s| !s.is_empty()) {
        commit.push_str("-dirty");
    }
    println!("cargo:rustc-env=DESMOS_COMMIT={commit}");
    println!("cargo:rerun-if-changed=../../.git/HEAD");
    println!("cargo:rerun-if-changed=../../.git/index");
}
