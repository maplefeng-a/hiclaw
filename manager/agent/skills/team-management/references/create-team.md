# Create Team

## Prerequisites

1. SOUL.md for the Team Leader must be generated from the builtin template (see below)

Team workers do NOT require a SOUL.md — the controller provides a default if none is specified.

## Leader SOUL.md Template

The Team Leader's SOUL.md must be generated from the builtin template. Replace the placeholders and write the result:

```bash
sed -e "s/\${TEAM_LEADER_NAME}/<LEADER_NAME>/g" \
    -e "s/\${TEAM_NAME}/<TEAM_NAME>/g" \
    -e "s/\${TEAM_WORKERS}/<worker1>, <worker2>, .../g" \
    /opt/hiclaw/agent/team-leader-agent/SOUL.md.tmpl \
    > /root/hiclaw-fs/agents/<LEADER_NAME>/SOUL.md
```

## Script Usage

```bash
bash /opt/hiclaw/agent/skills/team-management/scripts/create-team.sh \
  --name <TEAM_NAME> \
  --leader <LEADER_NAME> \
  --workers <w1>,<w2>,<w3> \
  [--leader-model <MODEL_ID>] \
  [--worker-models <m1>,<m2>,<m3>] \
  [--worker-skills <s1,s2>:<s3,s4>:...] \
  [--worker-mcp-servers <m1,m2>:<m3,m4>:...] \
  [--team-admin <HUMAN_NAME>] \
  [--team-admin-matrix-id <@user:domain>]
```

Notes:
- `--worker-skills` and `--worker-mcp-servers` use `:` to separate per-worker values (matching worker order)
- `--team-admin` is optional. If not specified, Global Admin is used as Team Admin
- Team Admin gets power level 100 in Team Room and Leader DM

## What the Script Does

1. Creates the Team Leader via `create-worker.sh --role team_leader --team <TEAM>`
2. Creates each team worker via `create-worker.sh --role worker --team <TEAM> --team-leader <LEADER>` with per-worker skills and mcpServers
3. Creates a Team Room (Leader + Team Admin + all workers) — no Global Admin unless they are the Team Admin
4. Creates a Leader DM room (Team Admin ↔ Leader)
5. Updates Leader's and Workers' `groupAllowFrom` to include Team Admin
6. Updates `teams-registry.json` with admin, leader_dm_room_id
7. Pushes team-leader-agent skills to Leader's MinIO workspace

## Room Topology Created

```
Leader Room:  Manager + Global Admin + Leader    (standard 3-party worker room)
Leader DM:    Team Admin ↔ Leader                (team management channel)
Team Room:    Leader + Team Admin + W1 + W2 + ... (no Global Admin unless they are Team Admin)
```

Note: Team Workers do NOT get individual rooms. All team communication happens in the Team Room.

## After Creation

1. Verify Leader is running: `hiclaw get workers <LEADER_NAME>`
2. Verify team info: `bash /opt/hiclaw/agent/skills/team-management/scripts/manage-teams-registry.sh --action get --team-name <TEAM_NAME>`
3. Send a greeting to the Team Leader in the Leader Room
4. The Team Leader will handle coordination with team workers from there
