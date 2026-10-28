use std::collections::BTreeSet;
use std::os::unix::fs::OpenOptionsExt;

use super::*;
use crate::test_support::{Rng, TempDir};

const JENKINS: &str = "https://jenkins.example/";
const TOKEN: &str = "s3cr3t-t0k3n";

fn credentials() -> Vec<(&'static str, &'static str)> {
    vec![("JENKINS_URL", JENKINS), ("JENKINS_USER", "agents"), ("JENKINS_TOKEN", TOKEN)]
}

/// A ci.env in a temporary directory holding text, with mode.
fn env_file(text: &str, mode: u32) -> (TempDir, String) {
    let dir = TempDir::new();
    let path = dir.join("ci.env");
    std::fs::write(&path, text).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
    (dir, path)
}

fn from(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
    let pairs: Vec<(String, String)> = pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
    move |name| pairs.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone())
}

fn map(pairs: &[(&str, &str)]) -> Map<String> {
    pairs.iter().map(|(k, v)| (*k, v.to_string())).collect()
}

#[test]
fn the_file_fills_what_the_environment_lacks() {
    let text: String = CI_ENV.iter().map(|k| format!("{k}=file-{k}\n")).collect();
    let (_dir, path) = env_file(&text, 0o600);
    let env = from(&[("JENKINS_USER", "from-env"), ("JENKINS_TOKEN", "")]);
    assert_eq!(
        ci_credentials(&env, &path).unwrap(),
        map(&[
            ("JENKINS_URL", "file-JENKINS_URL"),
            ("JENKINS_USER", "from-env"),
            ("JENKINS_TOKEN", "file-JENKINS_TOKEN")
        ])
    );
}

#[test]
fn the_file_is_not_read_when_the_environment_has_everything() {
    let (_dir, path) = env_file("garbage", 0o644);
    assert_eq!(ci_credentials(&from(&credentials()), &path).unwrap(), map(&credentials()));
}

#[test]
fn a_missing_file_leaves_the_environment() {
    let dir = TempDir::new();
    let env = from(&[("JENKINS_URL", JENKINS)]);
    assert_eq!(ci_credentials(&env, &dir.join("ci.env")).unwrap(), map(&[("JENKINS_URL", JENKINS)]));
}

#[test]
fn a_file_others_may_read_is_refused() {
    let (_dir, path) = env_file(&format!("JENKINS_TOKEN={TOKEN}\n"), 0o640);
    let e = ci_credentials(&from(&[]), &path).unwrap_err();
    assert!(e.0.ends_with(&format!("others may read it; run: chmod 600 {path}")), "{e}");
}

#[test]
fn shell_syntax() {
    let text = format!(
        "# Jenkins\n\nexport JENKINS_URL=https://jenkins.example/\n  JENKINS_USER='agents'  # the technical user\n\
         JENKINS_TOKEN=old\nJENKINS_TOKEN=\"{TOKEN}\"\n"
    );
    let (_dir, path) = env_file(&text, 0o600);
    assert_eq!(ci_credentials(&from(&[]), &path).unwrap(), map(&credentials()));
}

#[test]
fn a_malformed_line_is_named_without_its_content() {
    let (_dir, path) = env_file(&format!("JENKINS_URL={JENKINS}\n{TOKEN}\n"), 0o600);
    assert_eq!(ci_credentials(&from(&[]), &path).unwrap_err().0, format!("{path}:2: expected NAME=value"));
}

#[test]
fn a_file_that_is_not_utf8_is_named() {
    let dir = TempDir::new();
    let path = dir.join("ci.env");
    std::fs::write(&path, b"JENKINS_URL=\xff\n").unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    assert!(ci_credentials(&from(&[]), &path).unwrap_err().0.starts_with(&format!("could not read {path}: ")));
}

#[test]
fn path_follows_xdg_config_home() {
    let env = from(&[("XDG_CONFIG_HOME", "/xdg"), ("HOME", "/home/u")]);
    assert_eq!(ConfigDir::for_env(&env).ci_env_path(), "/xdg/ai-agents-orchestrator/ci.env");
    let env = from(&[("XDG_CONFIG_HOME", "relative"), ("HOME", "/home/u/")]);
    let config = ConfigDir::for_env(&env);
    assert_eq!(config.ci_env_path(), "/home/u/.config/ai-agents-orchestrator/ci.env");
    assert_eq!(config.prompts_dir(), "/home/u/.config/ai-agents-orchestrator/prompts");
    assert_eq!(config.workflows_dir(), "/home/u/.config/ai-agents-orchestrator/workflows");
    assert_eq!(ConfigDir::for_env(&from(&[])).path(), "~/.config/ai-agents-orchestrator");
    assert_eq!(ConfigDir::at("/").ci_env_path(), "/ci.env");
    assert!(ConfigDir::from_env().ci_env_path().ends_with("/ai-agents-orchestrator/ci.env"));
}

#[test]
fn child_processes_get_no_credentials_and_no_pathspec_switches() {
    let vars = credentials()
        .into_iter()
        .chain(GIT_PATHSPEC_ENV.iter().map(|k| (*k, "1")))
        .chain([("KEEP_ME", "1"), ("PATH", "/usr/bin")])
        .map(|(k, v)| (OsString::from(k), OsString::from(v)));
    let kept = without_withheld(vars);
    assert_eq!(kept, [("KEEP_ME".into(), "1".into()), ("PATH".into(), "/usr/bin".into())]);
    assert!(child_env().iter().all(|(k, _)| !CI_ENV.iter().any(|c| k == c)));
}

// -- What sh reads: tests/test_properties.py's TestEnvFile, with a seeded generator in place of Hypothesis --------

/// Characters an unquoted value may not hold.
const NOT_UNQUOTED: &str = " \t'\"\\$`;&|<>()~{";

fn env_name(rng: &mut Rng) -> String {
    const TAIL: &[u8] = b"ABCxyz019_";
    let prefix = if rng.below(4) == 0 { "export" } else { "" };
    let tail: String = (0..rng.below(9)).map(|_| char::from(*rng.pick(TAIL))).collect();
    format!("{prefix}V{tail}")
}

fn env_value(rng: &mut Rng) -> String {
    match rng.below(3) {
        0 => rng.text(12, &format!("{NOT_UNQUOTED}\n\r")),
        1 => format!("'{}'", rng.text(12, "'\n\r")),
        _ => format!("\"{}\"", rng.text(12, "\"\\$`\n\r")),
    }
}

fn padding(rng: &mut Rng) -> String {
    (0..rng.below(3)).map(|_| *rng.pick(&[' ', '\t'])).collect()
}

/// An env file of blank lines, comments and assignments, and the names it assigns.
fn generated_env_file(rng: &mut Rng) -> (String, BTreeSet<String>) {
    let (mut lines, mut names) = (Vec::new(), BTreeSet::new());
    for _ in 0..rng.below(9) {
        match rng.below(3) {
            0 => lines.push(padding(rng)),
            1 => lines.push(format!("{}#{}", padding(rng), rng.text(10, "\n"))),
            _ => {
                let name = env_name(rng);
                names.insert(name.clone());
                let export = *rng.pick(&["", "export ", "export\t"]);
                let after = if rng.below(2) == 0 {
                    padding(rng)
                } else {
                    format!(" {}#{}", padding(rng), rng.text(8, "\n\r"))
                };
                lines.push(format!("{}{export}{name}={}{after}", padding(rng), env_value(rng)));
            }
        }
    }
    (lines.join("\n") + "\n", names)
}

/// names as sh has them after sourcing the file at path with `set -a`.
fn sh_exports(path: &str, names: &BTreeSet<String>) -> Map<String> {
    let out = std::process::Command::new("sh")
        .args(["-c", "set -a; . \"$1\"; exec env -0", "sh", path])
        .output()
        .expect("sh runs");
    assert!(out.status.success(), "sh could not source {path}: {}", String::from_utf8_lossy(&out.stderr));
    let text = String::from_utf8(out.stdout).expect("sh exports UTF-8");
    let exported: Map<String> =
        text.split('\0').filter(|e| !e.is_empty()).filter_map(|e| e.split_once('=')).map(|(k, v)| (k, v.to_string())).collect();
    names.iter().map(|n| (n.as_str(), exported.get(n).cloned().expect("sh exports each name"))).collect()
}

fn write_private(dir: &TempDir, text: &str) -> String {
    let path = dir.join("ci.env");
    let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&path).unwrap();
    std::io::Write::write_all(&mut f, text.as_bytes()).unwrap();
    path
}

#[test]
fn reads_what_sh_exports() {
    let examples = [
        ("# note\u{85}V=evil\n".to_string(), BTreeSet::new()),
        ("V=x # c\n".to_string(), BTreeSet::from(["V".to_string()])),
        ("V='a\x0cb'\n".to_string(), BTreeSet::from(["V".to_string()])),
        ("V='a\u{2028}b'\n".to_string(), BTreeSet::from(["V".to_string()])),
    ];
    let mut rng = Rng::new(1);
    let generated = (0..100).map(|_| generated_env_file(&mut rng));
    for (text, names) in examples.into_iter().chain(generated) {
        let dir = TempDir::new();
        let path = write_private(&dir, &text);
        assert_eq!(ci_credentials(&from(&[]), &path).unwrap(), sh_exports(&path, &names), "{text:?}");
    }
}

/// An assignment parse_env_file refuses because sh reads it otherwise, or not at all, in some of its forms.
fn misread_line(rng: &mut Rng) -> String {
    let (name, value) = (env_name(rng), rng.text(8, &format!("{NOT_UNQUOTED}\n\r")));
    match rng.below(6) {
        0 => format!("{name}={value}{}x", rng.pick(&NOT_UNQUOTED.chars().collect::<Vec<_>>())),
        1 => format!("{name}=\"{value}{}x\"", rng.pick(&['\\', '$', '`'])),
        2 => rng.pick(&[format!("{name}={value}\0x"), format!("{name}='{value}\0x'"), format!("{name}=\"{value}\0x\"")]).clone(),
        3 => format!("{name}='{}'x{value}", rng.text(8, "'\n\r")),
        4 => format!("{name} ={value}"),
        _ => format!("{name}= x{value}"),
    }
}

#[test]
fn refuses_what_sh_reads_otherwise() {
    let examples = ["V=x y", "V='a'b", "V=a:~", "V=\"a$x\"", "V=a\0b", "export V={a,b}"].map(String::from);
    let mut rng = Rng::new(2);
    for line in examples.into_iter().chain((0..200).map(|_| misread_line(&mut rng))) {
        let e = parse_env_file(&format!("{line}\n"), "ci.env").unwrap_err();
        assert_eq!(e.0, "ci.env:1: expected NAME=value", "{line:?}");
    }
}

#[test]
fn refuses_a_carriage_return() {
    let mut rng = Rng::new(3);
    for _ in 0..200 {
        let line_end = rng.pick(&["\r", "\r\n"]);
        let text = format!("{}{}={}{line_end}", padding(&mut rng), env_name(&mut rng), env_value(&mut rng));
        let e = parse_env_file(&text, "ci.env").unwrap_err();
        assert!(e.0.starts_with("ci.env:1: a carriage return"), "{text:?}: {e}");
    }
}
