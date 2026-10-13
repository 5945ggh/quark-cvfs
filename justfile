# 列出可用命令
default:
    @just --list

alias snap := snapshot

# 生成带时间戳的仓库快照，供远端 agent 阅读
snapshot:
    #!/usr/bin/env sh
    set -eu
    snapshot_name="$(basename "$PWD")_仓库快照_$(date +%Y%m%d%H%M%S).xml"
    repomix --style xml --token-count-tree --output "$snapshot_name"
    printf '\n快照已生成：%s/%s\n' "$PWD" "$snapshot_name"
