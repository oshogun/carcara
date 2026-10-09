## 18. record_run for carcara

This section is written to be relayed to the carcara session as it stands.

**Tool**: `record_run` on Urutau's MCP server (`/mcp`), for agent integration accounts. It records one
agent run on one GitHub issue and claims or releases the issue's card. It never reads or writes GitHub
and never changes the board's buckets or card positions.

**Call it**:

- once when the run starts, with `status: "running"`: this claims the card;
- every 10 minutes while running, with `status: "running"` (a heartbeat; the claim's lease is 30
  minutes and each running call renews it);
- whenever the status changes (pausing, resuming, ending), and with any new unverified items or
  probes;
- once at the end with a terminal status.

**Input** (JSON object; unknown keys are refused):

| Field | Required | Type and rule |
| --- | --- | --- |
| `repo` | yes | `owner/name`, any letter case; must be on the integration's repository list; `running` and the three waiting statuses also need an Urutau board for it |
| `issue` | yes | integer 1 to 2147483647; closed issues and issues not on the board are accepted |
| `runId` | yes | `^[A-Za-z0-9._-]{1,64}$`; unique across all repositories; reuse it for every call of the same run |
| `status` | yes | `running`, `awaiting_approval`, `needs_human`, `budget_exceeded`, `done`, `failed`, `rejected`, `plan_only` |
| `triageRange` | no | `S`, `M`, `L`, `S-M`, `M-L`, `S-L` (ASCII hyphen) |
| `uncertaintyKind` | no | `external`, `normative`, `untested`, `none` |
| `unverified` | no | up to 20 items `{ "id": "^[A-Za-z0-9._-]{1,32}$", "kind": "external" \| "normative" \| "untested", "text": "1-1000 characters; stored as plain text, cut to 280" }` |
| `mergeShas` | no | up to 20 lower-case hex SHAs, 40 or 64 characters |
| `files` | no | up to 200 repository-relative paths, each up to 256 characters, `^[A-Za-z0-9._@+-]+(/[A-Za-z0-9._@+-]+)*$`, no `.` or `..` segment |
| `areas` | no | up to 10 paths in the same format, each up to 64 characters |
| `observedBy` | no | `^[a-z][a-z0-9-]{0,31}/[A-Za-z0-9.+_-]{1,31}$`, for example `carcara/0.9.1` |
| `fixRounds` | no | integer 0 to 1000 |
| `filesOmitted` | no | integer 0 to 100000: how many paths were left out of `files` because they did not match its pattern; stored for reports, shown nowhere |
| `withdrawn` | no | up to 20 ids of this run's own `external` or `untested` items that no longer apply (reworded under a new id, or made untrue by the fix) |
| `costUsd` | no | number 0 to 1000000 |
| `findings` | no | 1-4000 characters of plain text, stored cut to 1000; for `plan_only`, the answer or a link to it (an `https://` URL alone is shown as a link) |
| `probes` | no | up to 20 `{ "item": "<id of an external or untested item of this run>", "note": "optional, 1-1000 characters, stored cut to 280" }`. A probe says the agent checked the item and it holds. |

**Merge rules** (the call is an upsert on `runId`, so a retry after a crash is safe):

- An omitted field keeps its stored value; a given field replaces it.
- `unverified` is merged by `id`: new ids are added, earlier items are kept even when left out, and an
  id may never change its `kind` or `text` (`item-changed`). At most 20 items per run in total.
- A probe closes its item: it means the agent checked the claim and it holds. There is no "refuted"
  probe: a claim found false is not closed; report it in `findings`, fix it, or end the run with
  `failed`, and the item stays open on the card. A probe on an item that is already closed is skipped
  (`probesSkipped`), so replaying a call does not fail. Only a person closes `normative` items, with Accept in Urutau.
- `withdrawn` takes back items: a withdrawn item stops counting as open and is shown in Urutau as
  "withdrawn by the agent", never as checked. Use it when an item no longer describes the work, not
  for a claim that was checked (that is a probe) or found false while it still applies (leave it open).
  A withdrawn or already closed item named again is skipped (`withdrawnSkipped`). Re-sending a
  withdrawn item in `unverified` with the same kind and text changes nothing; it stays withdrawn. To
  reword an item, withdraw the old id and add the new text under a new id (for example `U3`). The same
  id in `probes` and `withdrawn` of one call is refused (`probed-and-withdrawn`). Normative items cannot
  be withdrawn (`item-needs-a-person`).
- Probes and withdrawals are accepted only while the run is open, including on the call that ends it.

**Status and claim lifecycle**:

| Status sent | Claim |
| --- | --- |
| `running` | takes the claim if the issue is free (or its previous claim's lease has expired), renews it if this run holds it; lease = now + 30 min |
| `awaiting_approval`, `needs_human`, `budget_exceeded` | takes or keeps the claim with no lease: it never expires; only a person can release it from the card |
| `done`, `failed`, `rejected`, `plan_only` | ends the run and deletes this run's claim; never refused because another run holds the claim |

- A run that crashed stops renewing; 30 minutes after its last running call, another run can claim the
  issue.
- Resuming after a person released the claim: send `running` with the same `runId`. If the issue is
  free, the claim comes back to this run. If another run claimed it, the answer is
  `claimed-by-other-run`; stop work on the issue rather than continue unclaimed. The run's history
  stays either way.
- A run that has ended never changes; any later call with its `runId` gets `run-finished`.

**Output** (`structuredContent`, and the same JSON as text):

```json
{
  "repo": "acme/widgets", "issue": 7, "runId": "c-20261008-0001", "status": "running",
  "created": true, "statusChanged": true,
  "claim": { "held": true, "leaseUntil": "2026-10-08T12:30:00.000Z" },
  "unverifiedOpen": { "external": 1, "normative": 1, "untested": 0 },
  "probesApplied": 0, "probesSkipped": 0, "withdrawnApplied": 0, "withdrawnSkipped": 0,
  "notified": true
}
```

`notified` says whether open Urutau boards were told; a heartbeat that changes nothing visible is not
announced.

**Errors**. Urutau's own errors are a result with `isError: true` whose text is
`{"error": "<code>", "message": "<fixed sentence>", ...}`; nothing was recorded:

| Code | Meaning | What to do |
| --- | --- | --- |
| `claimed-by-other-run` | another run holds the claim | stop work on this issue |
| `run-finished` | the run already ended; `runStatus` holds the status it ended with | if `runStatus` is the status you were sending, your earlier call landed: treat it as done; otherwise start a new run with a new `runId` |
| `run-id-taken` | the `runId` belongs to another issue or another integration | use a new `runId` |
| `item-changed` | an item id was reused with a different kind or text | give the changed item a new id |
| `too-many-items` | more than 20 items in the run | send fewer |
| `duplicate-item` | the same id twice in `unverified`, `probes` or `withdrawn` | send each once |
| `probed-and-withdrawn` | the same id in both `probes` and `withdrawn` | send it in one of them |
| `unknown-item` | a probe or withdrawal names an id that is not an item of this run | send the item first, or fix the id |
| `item-needs-a-person` | a probe or withdrawal names a normative item | leave it for a person |
| `invalid-text` | a text, note or findings is empty after cleaning | send real text |
| `repo-not-allowed` | the repository is not on the integration's list | ask the admin |
| `no-board` | a claiming status was sent for a repository nobody has opened a board for in Urutau | ask a person to open the board, then retry |
| `rate-limited` | over 120 calls a minute; `retryAfterSeconds` given | wait and retry the same call |
| `call-stopped` | the token was revoked, the integration removed, or the connection closed | stop |
| `server-error` | an unexpected failure | retry the same call; it is an upsert |

A call that breaks the input schema (a pattern, a length, an unknown key such as `kind` on a probe) is
refused by the MCP library before Urutau sees it: `isError: true` with the library's text, not the
JSON above. Nothing was recorded.

**What Urutau shows**: the card shows the claim's status and age, the run's open unverified items as
"⚑ N unverified (kinds)", "Question answered" for `plan_only`, and "triage: M–L" only when a person's
estimate falls outside the triage range. `get_board` cards carry `estimate`, `lastRun` and `claim`;
the top level carries `humanWaitLimit` (hours) and `waitingOnHuman` (each waiting card with its
`since` and `overLimit`). A card that has waited on a person longer than `humanWaitLimit` hours,
measured from the moment the run entered its waiting status, is shown red; sending the same waiting
status again does not restart that time, a different waiting status does. Costs, fix rounds, files, areas and merge
SHAs are stored for reports and shown nowhere on the board.
