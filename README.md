# google-play-mcp

A minimal [MCP](https://modelcontextprotocol.io/) server (stdio transport) that wraps the
Google Play **Android Publisher API v3** for subscription catalog inspection, track
inspection, regional base-plan price updates, and store listing edits (text and feature
graphic per language).

- Transport: stdio, newline-delimited JSON-RPC 2.0. No network port is opened.
- Auth: a Google Cloud service account key file, read only inside this process.
  The key is never written to stdout, logs, request bodies, or error messages.
- Read tools: `play_list_subscriptions`, `play_get_subscription`, `play_get_track`,
  `play_get_listings`, `play_validate_edit`.
- Write tools: `play_patch_base_plan_price` (prices), and the store listing edit flow
  `play_create_edit` → `play_update_listings` / `play_upload_feature_graphic` →
  `play_commit_edit` (or `play_delete_edit`).
- `play_get_track` and `play_get_listings` (without `editId`) create a temporary app edit,
  read, and delete the edit. **Only `play_commit_edit` commits an edit**, and only when
  called with `confirm: true`. Committing sends the changes for review.

The protocol framing is implemented by hand (no MCP SDK dependency) to keep the server
small and auditable. Dependencies are `google-auth` and `requests` only.

## Setup

### 1. Create a service account for the Play Developer API

1. In the Google Cloud console, create (or reuse) a project and enable the
   **Google Play Android Developer API**.
2. Create a service account (IAM & Admin → Service Accounts) and download a JSON key.
3. In the **Play Console** → *Users and permissions*, invite the service account's email
   address and grant it access to the app(s) you want to manage. For the tools in this
   repository you need at least:
   - *View app information and download bulk reports* (read tools),
   - *Manage orders and subscriptions* / *Manage products* or equivalent permission that
     allows editing subscriptions (for `play_patch_base_plan_price`),
   - *Edit and delete draft apps* / release-view permission for `play_get_track`
     (it creates and deletes an uncommitted edit),
   - *Manage store presence* for the listing tools (`play_update_listings`,
     `play_upload_feature_graphic`, `play_commit_edit`).
4. Store the key file outside this repository, with restrictive permissions
   (`chmod 600`). The `.gitignore` excludes `*.json` as a safety net, but do not rely on it.

The OAuth scope used is `https://www.googleapis.com/auth/androidpublisher`.

### 2. Install

```sh
git clone https://github.com/re-x-inc/google-play-mcp.git
cd google-play-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Python 3.10+ is required.

### 3. Pass the key file path

The server reads the key **path** from the environment variable
`GOOGLE_PLAY_SA_KEY_FILE`. The path is the only thing passed around; the key contents are
read by the server process itself.

```sh
export GOOGLE_PLAY_SA_KEY_FILE=/path/to/service-account.json
```

Never pass the key contents via arguments, environment variables, or MCP requests.

### 4. Run

```sh
GOOGLE_PLAY_SA_KEY_FILE=/path/to/service-account.json .venv/bin/python server.py
```

The server speaks MCP on stdin/stdout. Use absolute paths when launching it from a
non-interactive shell (for example `ssh host /abs/path/.venv/bin/python /abs/path/server.py`),
because no login profile is loaded there.

Helper modes for manual checks:

```sh
.venv/bin/python server.py --list-tools                       # print the tool list as JSON
.venv/bin/python server.py --call play_get_subscription \
  '{"packageName":"com.example.app","productId":"premium_yearly"}'   # call one tool, print JSON
python examples/stdio_client.py com.example.app [productId]   # end-to-end stdio smoke test
```

Example MCP client configuration (Claude Desktop / Claude Code style):

```json
{
  "mcpServers": {
    "google-play": {
      "command": "/abs/path/google-play-mcp/.venv/bin/python",
      "args": ["/abs/path/google-play-mcp/server.py"],
      "env": { "GOOGLE_PLAY_SA_KEY_FILE": "/path/to/service-account.json" }
    }
  }
}
```

## Tools

All tools return a JSON object both as `structuredContent` and as a text block. Errors
from the Google API are returned with `isError: true` and the API's error body
(`httpStatus`, `method`, `url`, `error`). Validation errors are returned as
`{"error": "invalid_request", "message": ...}`.

### `play_list_subscriptions` (read)

`monetization.subscriptions.list`.

| Input | Type | Required | Notes |
|---|---|---|---|
| `packageName` | string | yes | e.g. `com.example.app` |
| `pageSize` | integer 1..1000 | no | API default 50 |
| `pageToken` | string | no | from a previous `nextPageToken` |
| `raw` | boolean | no | return raw `Subscription` resources instead of summaries |

Output: `{ packageName, count, subscriptions: [Summary | Subscription], nextPageToken? }`.

A `Summary` is
`{ packageName, productId, listings: { <lang>: {title, description} }, basePlans: [BasePlanSummary] }`
and a `BasePlanSummary` is
`{ basePlanId, state, type: {kind, ...}, offerTags, regionalConfigs: [{regionCode, newSubscriberAvailability, price, priceText}], otherRegionsConfig }`.
`state` is `DRAFT` | `ACTIVE` | `INACTIVE`; `price` is a `google.type.Money`
(`{currencyCode, units, nanos?}`).

### `play_get_subscription` (read)

`monetization.subscriptions.get`.

| Input | Type | Required |
|---|---|---|
| `packageName` | string | yes |
| `productId` | string | yes |

Output: `{ summary: Summary, raw: Subscription }` (raw is the untouched API resource).

### `play_get_track` (read)

`edits.insert` → `edits.tracks.get` → `edits.delete`. The edit is never committed.

| Input | Type | Required | Notes |
|---|---|---|---|
| `packageName` | string | yes | |
| `track` | string | no | `production` (default), `beta`, `alpha`, `internal`, `wear:production`, ... |

Output:
`{ packageName, track, releases: [{name, status, versionCodes, userFraction, inAppUpdatePriority, countryTargeting, releaseNotes}], edit: {id, committed: false, deleted} }`.
`status` is `draft` | `inProgress` | `halted` | `completed`. `releases` is empty when the
track has no active release.

### `play_patch_base_plan_price` (write)

`monetization.subscriptions.patch` with `updateMask=basePlans`. The server fetches the
subscription, replaces the price of one existing regional config, and sends the full
`basePlans` array back (field masks cannot address a single list element).

| Input | Type | Required | Notes |
|---|---|---|---|
| `packageName` | string | yes | |
| `productId` | string | yes | |
| `basePlanId` | string | yes | |
| `regionCode` | string | yes | must already exist in the base plan's `regionalConfigs` |
| `price` | object | yes | `{ currencyCode, units, nanos? }`; currency must match the region's current currency |
| `regionsVersion` | string | no | `regionsVersion.version` query parameter, default `2022/02` |
| `latencyTolerance` | enum | no | `PRODUCT_UPDATE_LATENCY_TOLERANCE_LATENCY_SENSITIVE` (API default) or `..._TOLERANT` |
| `allowActive` | boolean | no | default `false`: writes to an `ACTIVE` base plan are refused unless `true` |

Output:
`{ packageName, productId, basePlanId, regionCode, basePlanState, before: {price, priceText}, requested: {price, priceText}, after: {price, priceText}, updateMask, regionsVersion }`.

Safety checks performed before any write: base plan must exist; the region must already be
configured; the currency must match; `ACTIVE` base plans are refused unless `allowActive`
is set. Adding regions, changing availability, or changing base plan state is out of scope.

### Store listing tools

Store listing changes go through an *app edit*: create one edit, stage every change in it,
check it, then commit once. Language codes are the Play Console codes (`ja-JP`, `en-US`,
`es-ES`, `es-419`, `pt-BR`, `de-DE`, `fr-FR`, `ko-KR`, `zh-CN`, `zh-TW`, `hi-IN`, `id`, ...).
Bare codes such as `es` or `de` are rejected by the API with
`400 The requested language is not currently supported`.

#### `play_get_listings` (read)

`edits.details.get`, `edits.listings.list`, `edits.images.list(featureGraphic)`.

| Input | Type | Required | Notes |
|---|---|---|---|
| `packageName` | string | yes | |
| `languages` | string[] | no | default: all languages |
| `editId` | string | no | read inside an existing edit (shows staged, uncommitted changes); the edit is kept. Without it a temporary edit is created and deleted. |
| `includeFeatureGraphic` | boolean | no | default `true` |

Output:
`{ packageName, defaultLanguage, availableLanguages, listings: [{language, title, shortDescription, fullDescription, video, featureGraphic: [{id, sha1, sha256, url}]}], missingLanguages, edit: {id, temporary, committed: false, deleted} }`.

#### `play_create_edit` (write, preparation)

`edits.insert`. Input `{ packageName }`. Output `{ packageName, editId, expiryTimeSeconds, committed: false }`.

#### `play_update_listings` (write, inside an edit)

`edits.listings.update` (all three fields given: creates the language if it does not exist)
or `edits.listings.patch` (some fields given: existing languages only).

| Input | Type | Required | Notes |
|---|---|---|---|
| `packageName` | string | yes | |
| `editId` | string | yes | |
| `listings` | array | yes | `[{ language, title?, shortDescription?, fullDescription? }]` |

All items are validated before anything is written: limits are title 30, short description
80, full description 4000 characters; languages must not repeat.
Output: `{ packageName, editId, committed: false, results: [{language, operation: create|update|patch, changedFields, after}] }`.

#### `play_upload_feature_graphic` (write, inside an edit)

`edits.images.deleteall(featureGraphic)` → `edits.images.upload(featureGraphic)`.

| Input | Type | Required | Notes |
|---|---|---|---|
| `packageName` | string | yes | |
| `editId` | string | yes | |
| `language` | string | yes | |
| `filePath` | string | yes | absolute path on the machine running the server; `.png` / `.jpg` / `.jpeg`, ≤ 15 MB (Play requires 1024×500) |

The returned `sha256` is checked against the local file.
Output: `{ packageName, editId, language, imageType, file: {path, bytes, sha256}, before: [Image], uploaded: Image, committed: false }`.

#### `play_validate_edit` (read)

`edits.validate`. Input `{ packageName, editId }`. Output `{ packageName, editId, valid: true, edit, committed: false }`;
a failed validation comes back as a Google API error.

#### `play_commit_edit` (write, publishes)

`edits.commit`. Committing sends the changes for review and cannot be undone.

| Input | Type | Required | Notes |
|---|---|---|---|
| `packageName` | string | yes | |
| `editId` | string | yes | |
| `confirm` | boolean | yes | must be `true`, otherwise the call is refused |
| `changesNotSentForReview` | boolean | no | default `false`; `true` commits without sending for review (send it from the Play Console afterwards) |

Output: `{ packageName, editId, committed: true, changesNotSentForReview, edit }`.

#### `play_delete_edit` (write, discards)

`edits.delete`. Input `{ packageName, editId }`. Output `{ packageName, editId, deleted: true, committed: false }`.

## Security notes

- Do not commit key files. `.gitignore` excludes `*.json`, `*.pem`, `*.p12`, `.env*`.
- Run the server on the machine that holds the key, and connect clients over stdio
  (for example through SSH) so that the key never leaves that machine.
- Consider restricting the set of callable tools on the client side (an allow-list) when
  exposing this server to automated agents.

## License

MIT — see [LICENSE](LICENSE).
