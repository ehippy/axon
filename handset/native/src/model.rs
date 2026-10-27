//! Apply wire data to display state without network or hardware access.
use crate::{activity::activity_row, hud::AgentInfo, state::AppState};
use serde_json::Value;

pub fn apply_view(state: &mut AppState, value: &Value) -> Result<(), &'static str> {
    if value["version"].as_u64() != Some(1) {
        return Err("Unsupported view version");
    }
    let sessions = value["sessions"].as_array().ok_or("Missing sessions")?;
    state.agents.retain(|key, _| !key.starts_with('@'));
    state.activity_deadlines.clear();
    for session in sessions {
        if let Some(row) = activity_row(session) {
            if matches!(
                session["state"].as_str(),
                Some("working" | "waiting" | "blocked")
            ) {
                let expires = row.updated_at + session["lease_seconds"].as_f64().unwrap_or(900.0);
                state.activity_deadlines.insert(row.key.clone(), expires);
            }
            state.agents.insert(
                row.key,
                AgentInfo {
                    state: row.state,
                    task: row.task,
                    ts: row.updated_at,
                },
            );
        }
    }
    Ok(())
}

pub fn apply_integration(
    state: &mut AppState,
    source: &str,
    value: &Value,
) -> Result<(), &'static str> {
    let status = value["state"].as_str().ok_or("Missing integration state")?;
    if !matches!(status, "idle" | "running" | "error") {
        return Err("Invalid integration state");
    }
    let ts = value["ts"]
        .as_f64()
        .filter(|v| v.is_finite() && *v > 0.0)
        .ok_or("Missing sender timestamp")?;
    state.agents.insert(
        format!("integration:{source}"),
        AgentInfo {
            state: status.into(),
            task: value["task"].as_str().unwrap_or("").into(),
            ts,
        },
    );
    Ok(())
}
