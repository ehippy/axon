//! Hardware-independent mapping of journal sessions onto the small Axon display.
#[derive(serde::Serialize)]
pub struct ActivityRow {
    pub key: String,
    pub source: String,
    pub state: String,
    pub task: String,
    pub updated_at: f64,
}

// Use the sender's timestamp: retained replay is never made to look like new work.
pub fn activity_row(v: &serde_json::Value) -> Option<ActivityRow> {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()?
        .as_secs_f64();
    let source = v["source"].as_str()?;
    let session = v["session"].as_str()?;
    let reported = v["effective_state"].as_str()?;
    let expires = v["updated_at"].as_f64()? + v["lease_seconds"].as_f64().unwrap_or(900.0);
    let status = if now > expires && matches!(reported, "working" | "waiting" | "blocked") {
        "lost_contact"
    } else {
        reported
    };
    let state = match status {
        "working" => "running",
        "blocked" => "error",
        _ => "idle",
    };
    let project = v["project"].as_str().unwrap_or("");
    let goal = v["goal"].as_str().filter(|s| !s.is_empty());
    let step = v["step"].as_str().filter(|s| !s.is_empty());
    let detail = if status == "finished" {
        v["outcome"].as_str()
    } else if status == "blocked" {
        v["blocker"].as_str().or(step).or(goal)
    } else {
        goal.or(step).or_else(|| v["activity"].as_str())
    }
    .unwrap_or(status);
    let activity = v["step"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or_else(|| v["activity"].as_str())
        .unwrap_or("");
    let task = format!("{project}: {detail}\n{status}: {activity}");
    // Keep status visible even when the goal occupies the entire task line.
    let label = format!("@{source}/{}", session);
    Some(ActivityRow {
        key: label,
        source: source.into(),
        state: state.into(),
        task,
        updated_at: v["updated_at"].as_f64()?,
    })
}

#[cfg(test)]
mod activity_tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn session_identity_and_stale_state_are_preserved() {
        let v = json!({"source":"codex-host", "session":"abc123", "project":"glass",
            "goal":"Fix agenda", "effective_state":"lost_contact", "updated_at":100.0});
        let row = activity_row(&v).unwrap();
        assert_eq!(row.key, "@codex-host/abc123");
        assert_eq!(row.source, "codex-host");
        assert_eq!(row.state, "idle");
        assert_eq!(row.updated_at, 100.0);
        assert!(row.task.contains("lost_contact"));
        assert!(row.task.contains("Fix agenda"));
    }
    #[test]
    fn completion_shows_outcome() {
        let v = json!({"source":"claude-host", "session":"abc", "effective_state":"finished",
            "goal":"Old goal", "outcome":"Tests passed", "updated_at":100.0});
        assert!(activity_row(&v).unwrap().task.contains("Tests passed"));
        assert!(activity_row(&json!({})).is_none());
    }
}

/// Recover publisher identity from a rich session key for existing agent colors.
pub fn identity_source(key: &str) -> &str {
    let key = key.strip_prefix("integration:").unwrap_or(key);
    key.strip_prefix('@')
        .and_then(|s| s.split_once('/').map(|(source, _)| source))
        .unwrap_or(key)
}
