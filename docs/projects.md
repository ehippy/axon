# Projects

The Projects view groups git repositories across hosts, agents, and worktrees.
Open a project to see its sessions (live first), host inventory, last activity,
and token growth over the selected period. A shared ID has one stable display
name even when hosts use different clone directory names. Labels outside git
appear as topics.
Old sessions retain their reported project names and are grouped by name until
their reporter sends a git identity.

Project settings require the same input token used for messaging. A rename only
changes the display. Hiding removes the project's sessions from the board while
keeping them available in Projects. Merging combines identities under the target
project, including host inventory and token totals. These overrides do not rewrite
events or the original session projection. A fork or a repository that gains a
remote can be merged with its earlier identity.

The API exposes `GET /projects?since=<unix-seconds>` and `GET /projects/<id>`.
The default token period is 24 hours; `since=0` includes retained history.
`POST /projects/<id>` accepts `name`, `hidden`, and/or `merge_into`, protected by
the input token. IDs are git hashes for new reports and `legacy:<name>` for old
reports; URL-encode them in paths. Session and event project filters accept the
canonical identity as well as legacy names. Project inventories never contain
checkout paths or remote URLs. Allowed start agents remain empty until the host
runner publishes an explicit policy (#19).
