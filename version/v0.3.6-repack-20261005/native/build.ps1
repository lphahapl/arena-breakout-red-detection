$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    cargo build --release --locked --manifest-path native/Cargo.toml
    if ($LASTEXITCODE -ne 0) { throw 'Rust build failed' }
} finally {
    Pop-Location
}
