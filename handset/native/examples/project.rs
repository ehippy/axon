//! Hardware-free projection of a server view, used by contract/integration checks.
use std::io::{self, Read};
fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    let value: serde_json::Value = serde_json::from_str(&input).unwrap();
    let rows: Vec<_> = value["sessions"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(axon_native::activity::activity_row)
        .collect();
    println!("{}", serde_json::to_string(&rows).unwrap());
}
