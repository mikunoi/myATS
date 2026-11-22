use anyhow::{bail, ensure, Context, Result};
use fs2::FileExt;
use reqwest::blocking::Client;
use semver::Version;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;
use zip::ZipArchive;

const APP: &str = env!("DISTRIBUTION_APP");
const VERSION: &str = env!("CARGO_PKG_VERSION");
const MAGIC: &[u8] = b"MKN-APP-PACK-v1!";
const MAX_INSTALLER: u64 = 768 * 1024 * 1024;

fn launcher() -> &'static str {
    if cfg!(windows) { "launch.exe" } else { "launch" }
}

fn data_home() -> Result<PathBuf> {
    if cfg!(windows) {
        return std::env::var_os("LOCALAPPDATA").map(PathBuf::from).context("LOCALAPPDATA is missing");
    }
    if let Some(path) = std::env::var_os("XDG_DATA_HOME").filter(|p| Path::new(p).is_absolute()) {
        return Ok(path.into());
    }
    Ok(PathBuf::from(std::env::var_os("HOME").context("HOME is missing")?).join(".local/share"))
}

fn current(root: &Path) -> Result<Version> {
    Ok(Version::parse(fs::read_to_string(root.join("current"))?.trim())?)
}

fn python(release: &Path, gui: bool) -> PathBuf {
    release.join(if cfg!(windows) {
        if gui { "runtime/pythonw.exe" } else { "runtime/python.exe" }
    } else { "runtime/bin/python" })
}

fn python_command(release: &Path, gui: bool) -> Command {
    let mut command = Command::new(python(release, gui));
    if cfg!(windows) {
        let runtime = release.join("runtime");
        let mut paths = vec![runtime.clone(), runtime.join("Library/bin"), runtime.join("Scripts")];
        paths.extend(std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()));
        if let Ok(path) = std::env::join_paths(paths) {
            command.env("PATH", path);
        }
    }
    command
}

fn asset_name(version: &Version) -> String {
    let suffix = if cfg!(windows) { "windows-x64.exe" } else { "linux-x64.run" };
    format!("{APP}-Setup-{version}-{suffix}")
}

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    draft: bool,
    prerelease: bool,
    body: Option<String>,
    assets: Vec<Asset>,
}

#[derive(Deserialize)]
struct Asset {
    name: String,
    size: u64,
    digest: Option<String>,
    browser_download_url: String,
}

fn http_client() -> Result<Client> {
    Ok(Client::builder().user_agent(format!("{APP}/{VERSION}"))
        .connect_timeout(Duration::from_secs(10)).timeout(Duration::from_secs(240))
        .redirect(reqwest::redirect::Policy::custom(|r| {
            if r.previous().len() < 4 && r.url().scheme() == "https" &&
                matches!(r.url().host_str(), Some("github.com" | "api.github.com" | "release-assets.githubusercontent.com" | "objects.githubusercontent.com")) {
                r.follow()
            } else {
                r.error("Unexpected release redirect")
            }
        })).build()?)
}

fn latest(http: &Client, installed: &Version) -> Result<Option<(Version, String, Asset)>> {
    let repo = format!("mikunoi/{APP}");
    let response = http.get(format!("https://api.github.com/repos/{repo}/releases/latest")).send()?;
    if response.status() == reqwest::StatusCode::NOT_FOUND { return Ok(None); }
    let release: Release = response.error_for_status()?.json()?;
    let version = Version::parse(release.tag_name.strip_prefix('v').unwrap_or(""))?;
    if release.draft || release.prerelease || !version.pre.is_empty() || version <= *installed {
        return Ok(None);
    }
    let name = asset_name(&version);
    let url = format!("https://github.com/{repo}/releases/download/{}/{name}", release.tag_name);
    let Some(asset) = release.assets.into_iter().find(|a| a.name == name && a.browser_download_url == url)
    else { return Ok(None); };
    let digest = asset.digest.as_deref().and_then(|v| v.strip_prefix("sha256:")).unwrap_or("");
    ensure!(digest.len() == 64 && digest.bytes().all(|b| b.is_ascii_hexdigit()), "Missing or invalid SHA-256");
    ensure!((1..=MAX_INSTALLER).contains(&asset.size), "Unexpected installer size");
    Ok(Some((version, release.body.unwrap_or_default(), asset)))
}

fn update(root: &Path, expected: &str) -> Result<()> {
    let http = http_client()?;
    let (version, _, asset) = latest(&http, &current(root)?)?.context("Update is no longer available")?;
    ensure!(version.to_string() == expected, "Release changed. Check again.");
    let scratch = tempfile::tempdir_in(root)?;
    let path = scratch.path().join(&asset.name);
    let mut output = File::create(&path)?;
    let mut response = http.get(asset.browser_download_url).send()?.error_for_status()?.take(asset.size + 1);
    let mut hash = Sha256::new();
    let mut total = 0;
    let mut buffer = [0; 64 * 1024];
    loop {
        let n = response.read(&mut buffer)?;
        if n == 0 { break; }
        output.write_all(&buffer[..n])?;
        hash.update(&buffer[..n]);
        total += n as u64;
    }
    drop(output);
    ensure!(total == asset.size && asset.digest.as_deref().unwrap().eq_ignore_ascii_case(&format!("sha256:{:x}", hash.finalize())), "Installer integrity check failed");
    executable(&path)?;
    ensure!(Command::new(&path).arg("--install-update").status()?.success(), "Installer did not complete");
    Ok(())
}

fn executable(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    #[cfg(windows)]
    let _ = path;
    Ok(())
}

fn payload(file: &mut File) -> Result<Option<(u64, u64)>> {
    let size = file.metadata()?.len();
    let footer = (8 + MAGIC.len()) as u64;
    if size < footer { return Ok(None); }
    file.seek(SeekFrom::End(-(footer as i64)))?;
    let mut length = [0; 8];
    let mut marker = vec![0; MAGIC.len()];
    file.read_exact(&mut length)?;
    file.read_exact(&mut marker)?;
    if marker != MAGIC { return Ok(None); }
    let length = u64::from_le_bytes(length);
    ensure!(length > 0 && length < size - footer, "Invalid payload length");
    Ok(Some((size - footer - length, length)))
}

fn extract(source: &mut File, offset: u64, length: u64, destination: &Path) -> Result<()> {
    source.seek(SeekFrom::Start(offset))?;
    let mut buffer = tempfile::tempfile_in(destination)?;
    ensure!(io::copy(&mut source.take(length), &mut buffer)? == length, "Truncated payload");
    let mut archive = ZipArchive::new(buffer)?;
    let mut total = 0u64;
    for i in 0..archive.len() {
        let mut member = archive.by_index(i)?;
        let path = member.enclosed_name().context("Invalid payload path")?;
        ensure!(!member.is_symlink(), "Payload symlinks are unsupported");
        total = total.checked_add(member.size()).context("Payload too large")?;
        ensure!(total <= 2 * 1024 * 1024 * 1024, "Payload too large");
        let path = destination.join(path);
        if member.is_dir() {
            fs::create_dir_all(path)?;
        } else {
            fs::create_dir_all(path.parent().context("Missing directory")?)?;
            io::copy(&mut member, &mut File::options().write(true).create_new(true).open(&path)?)?;
            #[cfg(unix)]
            if member.unix_mode().unwrap_or(0) & 0o111 != 0 {
                executable(&path)?;
            }
        }
    }
    Ok(())
}

fn setup(exe: &Path, root: &Path) -> Result<()> {
    fs::create_dir_all(root)?;
    let lock = OpenOptions::new().write(true).create(true).truncate(false).open(root.join("install.lock"))?;
    lock.try_lock_exclusive().context("Another installation is running")?;
    let version = Version::parse(VERSION)?;
    if current(root).is_ok_and(|old| old >= version) { return Ok(()); }
    let mut source = File::open(exe)?;
    let (offset, length) = payload(&mut source)?.context("Installer payload is missing")?;
    let versions = root.join("versions");
    fs::create_dir_all(&versions)?;
    let dest = versions.join(VERSION);
    if !dest.join(".ready").is_file() {
        if dest.exists() { fs::remove_dir_all(&dest)?; }
        fs::create_dir(&dest)?;
        extract(&mut source, offset, length, &dest)?;
        let unpack = if cfg!(windows) { "runtime/Scripts/conda-unpack-script.py" } else { "runtime/bin/conda-unpack" };
        ensure!(python_command(&dest, false).arg(dest.join(unpack)).status()?.success(), "Bundled runtime configuration failed");
        fs::write(dest.join(".ready"), VERSION)?;
    }
    if APP == "myATS" {
        let workspace = root.join("workspace");
        fs::create_dir_all(workspace.join("config"))?;
        if !workspace.join("config/default.json").exists() {
            fs::copy(dest.join("Kiwoom5814/ats/config/default.json"), workspace.join("config/default.json"))?;
        }
        if !workspace.join(".env").exists() {
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            options.open(workspace.join(".env"))?;
        }
    }
    let installed = root.join(launcher());
    if !installed.is_file() {
        source.seek(SeekFrom::Start(0))?;
        let mut stable = tempfile::NamedTempFile::new_in(root)?;
        ensure!(io::copy(&mut source.take(offset), stable.as_file_mut())? == offset, "Truncated launcher");
        executable(stable.path())?;
        stable.persist(&installed)?;
    }
    #[cfg(unix)]
    {
        let fonts = data_home()?.join("fonts/steamdeck-5814");
        if dest.join("fonts").is_dir() {
            fs::create_dir_all(&fonts)?;
            for item in fs::read_dir(dest.join("fonts"))? {
                let item = item?;
                if !fonts.join(item.file_name()).is_file() {
                    fs::copy(item.path(), fonts.join(item.file_name()))?;
                }
            }
            let _ = Command::new("fc-cache").arg("-f").arg(fonts).status();
        }
    }
    shortcut(&installed)?;
    let mut pointer = tempfile::NamedTempFile::new_in(root)?;
    writeln!(pointer, "{VERSION}")?;
    pointer.as_file().sync_all()?;
    pointer.persist(root.join("current"))?;
    Ok(())
}

fn shortcut(installed: &Path) -> Result<()> {
    #[cfg(windows)]
    {
        let status = Command::new("powershell.exe").args(["-NoProfile", "-NonInteractive", "-Command",
            "$w=New-Object -ComObject WScript.Shell; $p=Join-Path ([Environment]::GetFolderPath('Programs')) ($env:APP_SHORTCUT_NAME + '.lnk'); $s=$w.CreateShortcut($p); $s.TargetPath=$env:APP_SHORTCUT_TARGET; $s.Save()"])
            .env("APP_SHORTCUT_NAME", APP).env("APP_SHORTCUT_TARGET", installed).status()?;
        ensure!(status.success(), "Start Menu shortcut failed");
    }
    #[cfg(unix)]
    {
        let applications = data_home()?.join("applications");
        fs::create_dir_all(&applications)?;
        let installed = installed.to_str().context("Installation path is not UTF-8")?;
        ensure!(!installed.contains(['\n', '\r', '\\', '"', '$', '`']), "Unsupported installation path");
        fs::write(applications.join(format!("{APP}.desktop")),
            format!("[Desktop Entry]\nType=Application\nName={APP}\nExec=\"{}\"\nTerminal=false\nCategories=Office;Finance;\n", installed.replace('%', "%%")))?;
    }
    Ok(())
}

fn launch(root: &Path, extra: impl Iterator<Item = std::ffi::OsString>) -> Result<()> {
    let release = root.join("versions").join(current(root)?.to_string());
    let mut app = python_command(&release, true);
    app.current_dir(&release).env("DISTRIBUTION_LAUNCHER", root.join(launcher()))
        .env("PYTHONUTF8", "1").env_remove("PYTHONHOME").env_remove("PYTHONPATH");
    if APP == "myATS" {
        let source = release.join("Kiwoom5814");
        app.arg(source.join("app/launcher.py")).env("MYATS_WORKSPACE", root.join("workspace"))
            .env("PYTHONPATH", source.join("ats")).env("MYATS_RUNTIME_PYTHON", python(&release, false));
    } else {
        app.arg(release.join("ui/main.py"));
    }
    ensure!(app.args(extra).status()?.success(), "Application closed with an error");
    Ok(())
}

fn run() -> Result<()> {
    let root = data_home()?.join(APP);
    let exe = std::env::current_exe()?;
    let mut args = std::env::args_os().skip(1);
    match args.next().as_deref().and_then(|arg| arg.to_str()) {
        Some("--check-update") => {
            let update = latest(&http_client()?, &current(&root)?)?.map(|(version, notes, _)|
                serde_json::json!({"version": version.to_string(), "notes": notes}));
            println!("{}", serde_json::to_string(&update)?);
        }
        Some("--apply-update") => update(&root, &args.next().context("Version required")?.to_string_lossy())?,
        Some("--install-update") => setup(&exe, &root)?,
        Some("--version") => println!("{VERSION}"),
        Some("--launch") => launch(&root, args)?,
        None => {
            if payload(&mut File::open(&exe)?)?.is_some() { setup(&exe, &root)?; }
            launch(&root, args)?;
        }
        _ => bail!("Unknown option"),
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{APP}: {error:#}");
        std::process::exit(1);
    }
}
