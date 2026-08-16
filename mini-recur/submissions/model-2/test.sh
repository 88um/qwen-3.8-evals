#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

pass=0
fail=0

run_test() {
    local name="$1" input="$2" expected="$3"
    local actual
    actual=$(echo "$input" | ./recur)
    if python3 -c "
import json, sys
a = json.loads(sys.argv[1])
b = json.loads(sys.argv[2])
sys.exit(0 if a == b else 1)
" "$expected" "$actual"; then
        echo "PASS: $name"
        ((pass++)) || true
    else
        echo "FAIL: $name"
        echo "  Expected: $expected"
        echo "  Got:      $actual"
        ((fail++)) || true
    fi
}

# --- Provided examples ---

run_test "example-1: basic weekly" \
    '{"timezone":"America/New_York","window":{"start":"2026-06-01T00:00:00Z","end":"2026-06-08T00:00:00Z"},"rules":[{"id":"r1","days":["tue","sat"],"time":"08:30"}],"edits":[]}' \
    '{"occurrences":[{"rule_id":"r1","local_date":"2026-06-02","local_time":"08:30","instant":"2026-06-02T12:30:00Z","status":"scheduled"},{"rule_id":"r1","local_date":"2026-06-06","local_time":"08:30","instant":"2026-06-06T12:30:00Z","status":"scheduled"}]}'

run_test "example-2: DST spring-forward gap" \
    '{"timezone":"America/New_York","window":{"start":"2026-03-06T00:00:00Z","end":"2026-03-12T00:00:00Z"},"rules":[{"id":"r1","days":["sun","tue"],"time":"02:30"}],"edits":[]}' \
    '{"occurrences":[{"rule_id":"r1","local_date":"2026-03-08","local_time":"02:30","instant":null,"status":"skipped_dst"},{"rule_id":"r1","local_date":"2026-03-10","local_time":"02:30","instant":"2026-03-10T06:30:00Z","status":"scheduled"}]}'

# --- Edit semantics ---

run_test "set_time: edit steps over a date" \
    '{"timezone":"America/New_York","window":{"start":"2026-06-10T00:00:00Z","end":"2026-06-11T00:00:00Z"},"rules":[{"id":"r1","days":["wed"],"time":"15:00"}],"edits":[{"at":"2026-06-10T17:00:00Z","kind":"set_time","rule_id":"r1","time":"10:00"}]}' \
    '{"occurrences":[]}'

run_test "set_time: old time fires before edit" \
    '{"timezone":"America/New_York","window":{"start":"2026-06-10T00:00:00Z","end":"2026-06-11T00:00:00Z"},"rules":[{"id":"r1","days":["wed"],"time":"10:00"}],"edits":[{"at":"2026-06-10T18:00:00Z","kind":"set_time","rule_id":"r1","time":"15:00"}]}' \
    '{"occurrences":[{"rule_id":"r1","local_date":"2026-06-10","local_time":"10:00","instant":"2026-06-10T14:00:00Z","status":"scheduled"}]}'

run_test "set_time: new time fires after edit" \
    '{"timezone":"America/New_York","window":{"start":"2026-06-10T00:00:00Z","end":"2026-06-11T00:00:00Z"},"rules":[{"id":"r1","days":["wed"],"time":"10:00"}],"edits":[{"at":"2026-06-10T13:00:00Z","kind":"set_time","rule_id":"r1","time":"15:00"}]}' \
    '{"occurrences":[{"rule_id":"r1","local_date":"2026-06-10","local_time":"15:00","instant":"2026-06-10T19:00:00Z","status":"scheduled"}]}'

# --- Fall-back (ambiguous time) uses earlier offset ---

run_test "fall-back: ambiguous time uses earlier offset" \
    '{"timezone":"America/New_York","window":{"start":"2026-10-31T00:00:00Z","end":"2026-11-02T00:00:00Z"},"rules":[{"id":"r1","days":["sun"],"time":"01:30"}],"edits":[]}' \
    '{"occurrences":[{"rule_id":"r1","local_date":"2026-11-01","local_time":"01:30","instant":"2026-11-01T05:30:00Z","status":"scheduled"}]}'

# --- Timezone change ---

run_test "set_timezone: re-keys dates in new tz" \
    '{"timezone":"America/New_York","window":{"start":"2026-06-01T00:00:00Z","end":"2026-06-04T00:00:00Z"},"rules":[{"id":"r1","days":["mon","tue"],"time":"10:00"}],"edits":[{"at":"2026-06-02T00:00:00Z","kind":"set_timezone","timezone":"Asia/Tokyo"}]}' \
    '{"occurrences":[{"rule_id":"r1","local_date":"2026-06-01","local_time":"10:00","instant":"2026-06-01T14:00:00Z","status":"scheduled"},{"rule_id":"r1","local_date":"2026-06-02","local_time":"10:00","instant":"2026-06-02T01:00:00Z","status":"scheduled"}]}'

# --- Window boundary: start inclusive, end exclusive ---

run_test "window boundary: instant at start is included" \
    '{"timezone":"UTC","window":{"start":"2026-06-01T10:00:00Z","end":"2026-06-02T00:00:00Z"},"rules":[{"id":"r1","days":["mon"],"time":"10:00"}],"edits":[]}' \
    '{"occurrences":[{"rule_id":"r1","local_date":"2026-06-01","local_time":"10:00","instant":"2026-06-01T10:00:00Z","status":"scheduled"}]}'

run_test "window boundary: instant at end is excluded" \
    '{"timezone":"UTC","window":{"start":"2026-06-01T00:00:00Z","end":"2026-06-01T10:00:00Z"},"rules":[{"id":"r1","days":["mon"],"time":"10:00"}],"edits":[]}' \
    '{"occurrences":[]}'

# --- Multiple rules, sorted by instant then rule_id ---

run_test "multi-rule sort: by instant then rule_id" \
    '{"timezone":"UTC","window":{"start":"2026-06-01T00:00:00Z","end":"2026-06-02T00:00:00Z"},"rules":[{"id":"r2","days":["mon"],"time":"10:00"},{"id":"r1","days":["mon"],"time":"10:00"}],"edits":[]}' \
    '{"occurrences":[{"rule_id":"r1","local_date":"2026-06-01","local_time":"10:00","instant":"2026-06-01T10:00:00Z","status":"scheduled"},{"rule_id":"r2","local_date":"2026-06-01","local_time":"10:00","instant":"2026-06-01T10:00:00Z","status":"scheduled"}]}'

# --- Empty window ---

run_test "empty window: no occurrences" \
    '{"timezone":"UTC","window":{"start":"2026-06-01T00:00:00Z","end":"2026-06-01T00:00:00Z"},"rules":[{"id":"r1","days":["mon"],"time":"10:00"}],"edits":[]}' \
    '{"occurrences":[]}'

echo ""
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
