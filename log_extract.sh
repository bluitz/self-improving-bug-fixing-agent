#!/usr/bin/env bash
set -euo pipefail

: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}"
CLAUDE_MODEL="${CLAUDE_MODEL:-claude-3-5-sonnet-latest}"

LOG_FILE="${1:?usage: log_extract.sh /path/to/app.log}"
QUERY="${2:-"Create an incident timeline, cluster errors, extract KPIs."}"

OUT_DIR="analyses/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR/chunks" "$OUT_DIR/llm" "$OUT_DIR/tmp"

# Detect tools and define fallbacks
if command -v rg >/dev/null 2>&1; then
  RG_CMD=(rg -n)
else
  echo "⚠️  ripgrep (rg) not found; using grep fallback (slower)." >&2
  RG_CMD=(grep -nE)
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "❌ jq is required. Please install jq (e.g., brew install jq)" >&2
  exit 1
fi

# Provide a minimal ask_claude() if none is defined by the caller's shell
if ! type ask_claude >/dev/null 2>&1; then
  ask_claude() {
    local prompt="$1"
    local model="${CLAUDE_MODEL:-claude-3-5-sonnet-latest}"
    curl -sS https://api.anthropic.com/v1/messages \
      -H "x-api-key: ${ANTHROPIC_API_KEY}" \
      -H "anthropic-version: 2023-06-01" \
      -H "content-type: application/json" \
      -d "$(jq -Rn --arg p "$prompt" --arg m "$model" '{model: $m, max_tokens: 4096, temperature: 0.2, messages: [{role:"user", content: $p}] }')" \
      | jq -r '.content[0].text // empty'
  }
fi

############################################
# 0) Prefilter: prioritize Netflix RefApp signal
############################################
# Collect errors/warnings and Netflix-specific domains, plus a sample of debug/info
"${RG_CMD[@]}" '(ERROR|WARN|FATAL|Exception|Traceback|panic)' "$LOG_FILE" > "$OUT_DIR/tmp/signal.err" || true
"${RG_CMD[@]}" '(UI_VIDEO|VIDEO::|Video::|Video#setTargetState|PLAYBACK_|Player|manifest|license|DRM|Widevine|bitrate|stall|buffer|bookmark)' "$LOG_FILE" > "$OUT_DIR/tmp/signal.playback" || true
"${RG_CMD[@]}" '(FocusPath|focusPath|focus|Route|route|Navigation|Nav|screen|page)' "$LOG_FILE" > "$OUT_DIR/tmp/signal.nav" || true
"${RG_CMD[@]}" '(Key|KEY_|keydown|keyup|ENTER|SELECT|BACK|HOME)' "$LOG_FILE" > "$OUT_DIR/tmp/signal.keys" || true
"${RG_CMD[@]}" '(Falcor|NodeQuark|GraphQL|HTTP|status=|status_code|response|request)' "$LOG_FILE" > "$OUT_DIR/tmp/signal.data" || true

# Pull up to 1000 random debug/info lines to give normal context
"${RG_CMD[@]}" 'INFO|debug|DEBUG' "$LOG_FILE" | awk 'BEGIN{srand()} {if (rand()<1000/NR) print $0}' > "$OUT_DIR/tmp/info.sample" || : > "$OUT_DIR/tmp/info.sample"

cat \
  "$OUT_DIR/tmp/signal.err" \
  "$OUT_DIR/tmp/signal.playback" \
  "$OUT_DIR/tmp/signal.nav" \
  "$OUT_DIR/tmp/signal.keys" \
  "$OUT_DIR/tmp/signal.data" \
  "$OUT_DIR/tmp/info.sample" \
  | sort -n -t: -k1,1 -k2,2 > "$OUT_DIR/prefilter.log" || cp "$LOG_FILE" "$OUT_DIR/prefilter.log"

############################################
# 1) Chunk with overlap to preserve causality
############################################
# Split by ~250KB with 5KB overlap to catch cross-chunk stack traces
split -b 250k -d "$OUT_DIR/prefilter.log" "$OUT_DIR/chunks/chunk-"
# add overlap from end of previous chunk to the next
prev=""
for f in "$OUT_DIR"/chunks/chunk-*; do
  if [[ -n "${prev:-}" ]]; then
    tail -c 5k "$prev" > "$OUT_DIR/tmp/ovlp"
    cat "$OUT_DIR/tmp/ovlp" "$f" > "$f.merged" && mv "$f.merged" "$f"
  fi
  prev="$f"
done

############################################
# 2) Ask Claude for per-chunk structured extraction (Netflix RefApp tuned)
############################################
extract_schema='{
  "session": {
    "start_ts": "string?",
    "end_ts": "string?",
    "duration_s": 0,
    "start_screen": "string?",
    "end_screen": "string?",
    "user_actions": ["string"],
    "selected_video_id": "number|string|null"
  },
  "events": [
    {
      "ts": "RFC3339 or HH:MM:SS.mmm",
      "level": "DEBUG|INFO|WARN|ERROR|FATAL",
      "category": "navigation|input|playback|data|ui|system|error",
      "component": "string?",
      "action": "key_press|route_change|focus_change|play_request|play_start|stall|bitrate_change|license_request|manifest_request|play_end|bookmark_update|request|response|error|warning",
      "summary": "<=140 chars",
      "details": {
        "focus_path": "string?",
        "route": "string?",
        "key": "string?",
        "video_id": "number|string|null",
        "track_id": "number|string|null",
        "row": "number|null",
        "request_id": "string?",
        "status": "number|null",
        "latency_ms": "number|null"
      }
    }
  ],
  "error_clusters": [
    {
      "fingerprint": "stable hash of root cause",
      "severity": "WARN|ERROR|FATAL",
      "representative": "one-line summary",
      "count": 0,
      "stack_excerpt": "<=20 lines",
      "first_ts": "string?",
      "last_ts": "string?",
      "impact": "<=120 chars"
    }
  ],
  "kpis": {
    "keypress_total": 0,
    "navigations_total": 0,
    "play_attempts": 0,
    "play_successes": 0,
    "stall_events": 0,
    "stall_time_total_ms": 0,
    "falcor_requests": 0,
    "http_errors": 0,
    "latency_ms": {"p50": 0, "p95": 0, "p99": 0}
  }
}'

i=0
for f in "$OUT_DIR"/chunks/chunk-*; do
  i=$((i+1))
  prompt=$(cat <<EOF
You are extracting Netflix RefApp user-behavior telemetry from logs.
Rules:
- Output ONLY JSON matching this schema (fields may be omitted if unknown):
$extract_schema
- Interpret Netflix-specific tags, e.g., UI_VIDEO, PLAYBACK_*, FocusPath, route/navigation, keypresses (ENTER/SELECT/BACK/HOME), Falcor/NodeQuark requests.
- Map timestamps to RFC3339 if possible; otherwise keep HH:MM:SS.mmm.
- Derive events like: key_press, route_change, focus_change, play_request/start/end, stall, bitrate_change, license_request, manifest_request, bookmark_update.
- Populate video_id, track_id, row, request_id when visible in lines.
- Cluster identical root-cause errors by message + top frames.
- For KPIs, infer from common patterns (status=500, lat=123ms, stall/buffer, PLAYBACK_*, bookmark updates).
Extract from this chunk:

$(cat "$f")
EOF
)
  # call Claude (assumes ask_claude defined elsewhere)
  resp_json="$(ask_claude "$prompt")" || resp_json='{}'
  # Validate JSON structure gently (existence checks)
  echo "$resp_json" | jq '.' > "$OUT_DIR/llm/chunk-$i.json" || echo '{}' > "$OUT_DIR/llm/chunk-$i.json"
done

############################################
# 3) Merge per-chunk JSON deterministically
############################################
merged_events=$(jq -s '[.[].events] | flatten | map(select(.ts != null))' "$OUT_DIR"/llm/chunk-*.json 2>/dev/null || echo '[]')
merged_clusters=$(jq -s '[.[].error_clusters] | flatten' "$OUT_DIR"/llm/chunk-*.json 2>/dev/null || echo '[]')
merged_kpis=$(jq -s '
  reduce .[]?.kpis as $k (
    {"requests_total":0,"errors_total":0,"latency_ms":{"p50":null,"p95":null,"p99":null}};
    .requests_total += ($k.requests_total // 0)
    | .errors_total += ($k.errors_total // 0)
    | .latency_ms.p50 = ([$k.latency_ms.p50, .latency_ms.p50] | map(select(.) ) | add/length)
    | .latency_ms.p95 = ([$k.latency_ms.p95, .latency_ms.p95] | map(select(.) ) | add/length)
    | .latency_ms.p99 = ([$k.latency_ms.p99, .latency_ms.p99] | map(select(.) ) | add/length)
  )' "$OUT_DIR"/llm/chunk-*.json 2>/dev/null || echo '{}')

# Re-cluster globally by fingerprint and compute counts & windows
global_clusters=$(jq -n --argjson arr "$merged_clusters" '
  ($arr // []) 
  | group_by(.fingerprint) 
  | map({
      fingerprint: .[0].fingerprint,
      representative: (.[0].representative // "Unknown"),
      count: (map(.count // 1) | add),
      first_ts: (map(.first_ts) | sort | .[0]),
      last_ts:  (map(.last_ts)  | sort | .[-1]),
      stack_excerpt: (.[0].stack_excerpt // null)
    })
')

# Sort events by timestamp (string sort works for RFC3339)
sorted_events=$(jq -n --argjson ev "$merged_events" '($ev // []) | sort_by(.ts)')

# Persist merged JSON
jq -n --argjson events "$sorted_events" \
      --argjson clusters "$global_clusters" \
      --argjson kpis "$merged_kpis" \
      '{events:$events,error_clusters:$clusters,kpis:$kpis}' \
      > "$OUT_DIR/summary.json"

############################################
# 4) Produce a human-readable Markdown from JSON (Netflix RefApp tuned)
############################################
md_prompt=$(cat <<'EOF'
You are given structured Netflix RefApp session data as JSON with {events, error_clusters, kpis}.
Write a concise Markdown report with:
- Executive summary (3–5 bullets) capturing user intent and session outcome
- KPI snapshot: keypresses, navigations, play attempts/successes, stalls (count/time), Falcor requests, HTTP errors, latency p50/p95/p99
- Playback details per video (video_id, track_id, row if present) with play start/end, stalling, bitrate changes
- Top error clusters (fingerprint, severity, count, 1–2 line explanation and impact)
- User journey timeline (20 most important events with ts, category, action, brief summary)
Keep under 400 lines. Avoid speculation. If data is missing, state it.
EOF
)

report_md=$(ask_claude "$md_prompt

JSON:
$(cat "$OUT_DIR/summary.json")
")
printf '%s\n' "$report_md" > "$OUT_DIR/report.md"

############################################
# 5) Judge against rubric and iterate for higher score
############################################
RUBRIC_FILE="${RUBRIC_FILE:-refapp_rubric.md}"
if [[ ! -f "$RUBRIC_FILE" ]]; then
  cat > "$RUBRIC_FILE" <<'RUBRIC'
Netflix RefApp Session Report Rubric (0–5 per criterion; 5 = excellent)

1. User Behavior Narrative (weight 2): Captures user intent, key actions (keypresses, navigations), and outcome coherently.
2. Playback Analysis (weight 2): Correctly identifies play attempts, starts/ends, stalls, bitrate changes, bookmark updates.
3. Data/Network Insight (weight 1): Summarizes Falcor/HTTP requests, statuses, and notable latencies.
4. Error/Warning Clustering (weight 1.5): Deduplicates root causes; conveys severity and likely impact.
5. KPI Accuracy (weight 2): Keypress, navigation, play, stall, error counts and latency percentiles are plausible and consistent with events.
6. Timeline Quality (weight 1): Selects the 20 most salient events with clear summaries and timestamps.
7. Clarity & Brevity (weight 0.5): Clear structure; minimal speculation; <= 400 lines.

Output score JSON shape:
{
  "scores": {"user_behavior": 0-5, "playback": 0-5, "data_network": 0-5, "errors": 0-5, "kpis": 0-5, "timeline": 0-5, "clarity": 0-5},
  "overall": 0-5,
  "justification": "<=200 words",
  "suggestions": ["concrete improvement"...]
}
RUBRIC
fi

judge_prompt=$(cat <<EOF
You are grading a Netflix RefApp session report against a rubric.
Rubric:
$(cat "$RUBRIC_FILE")

Report to grade (Markdown):
$(cat "$OUT_DIR/report.md")

Grade strictly. Output ONLY JSON with fields: scores, overall, justification, suggestions.
EOF
)

judge_json="$(ask_claude "$judge_prompt" || echo '{}')"
echo "$judge_json" | jq '.' > "$OUT_DIR/judge.v1.json" || echo '{}' > "$OUT_DIR/judge.v1.json"

best_overall=$(jq -r '.overall // 0' "$OUT_DIR/judge.v1.json" 2>/dev/null || echo 0)
best_report="$OUT_DIR/report.md"

# If score < 4.5, request a revised report to maximize the score
if awk "BEGIN {exit !($best_overall < 4.5)}"; then
  improve_prompt=$(cat <<EOF
You are to revise the report to maximize the rubric score. Use the same JSON data below.
Rubric:
$(cat "$RUBRIC_FILE")

Original report:
$(cat "$OUT_DIR/report.md")

Structured JSON data:
$(cat "$OUT_DIR/summary.json")

Task: Return ONLY the improved Markdown report (no commentary).
EOF
  )
  improved_report="$(ask_claude "$improve_prompt" || true)"
  if [[ -n "$improved_report" ]]; then
    printf '%s\n' "$improved_report" > "$OUT_DIR/report.v2.md"
    # Re-grade
    judge2_prompt=$(cat <<EOF
Rubric:
$(cat "$RUBRIC_FILE")

Report to grade (Markdown):
$(cat "$OUT_DIR/report.v2.md")

Grade strictly. Output ONLY JSON with fields: scores, overall, justification, suggestions.
EOF
    )
    judge2_json="$(ask_claude "$judge2_prompt" || echo '{}')"
    echo "$judge2_json" | jq '.' > "$OUT_DIR/judge.v2.json" || echo '{}' > "$OUT_DIR/judge.v2.json"
    overall2=$(jq -r '.overall // 0' "$OUT_DIR/judge.v2.json" 2>/dev/null || echo 0)
    if awk "BEGIN {exit !($overall2 > $best_overall)}"; then
      best_overall="$overall2"
      best_report="$OUT_DIR/report.v2.md"
    fi
  fi
fi

echo "✅ Done."
echo "JSON:      $OUT_DIR/summary.json"
echo "Report:    $best_report"
echo "Rubric:    ${RUBRIC_FILE}"
echo "Judgment:  $OUT_DIR/judge.v1.json"
if [[ -f "$OUT_DIR/judge.v2.json" ]]; then echo "Judgment2: $OUT_DIR/judge.v2.json"; fi
