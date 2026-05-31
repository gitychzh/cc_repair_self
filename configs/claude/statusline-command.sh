#!/bin/bash
input=$(cat)
model=$(echo "$input" | jq -r '.model.display_name // .model.id // "unknown"')
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
total_input=$(echo "$input" | jq -r '.context_window.total_input_tokens // empty')
ctx_size=$(echo "$input" | jq -r '.context_window.context_window_size // empty')

if [ -n "$used_pct" ]; then
  echo "${model} | ${total_input}/${ctx_size} tokens (${used_pct}% used)"
else
  echo "${model}"
fi