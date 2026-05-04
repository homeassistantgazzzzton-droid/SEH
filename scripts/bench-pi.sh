#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# bench-pi.sh — Mesure les ressources consommées par Simply Energy Home
# sur le Pi (avant/après optimisations sprint 13).
#
# Usage (sur le Pi cible) :
#   ./bench-pi.sh             # 5 minutes de mesure, sortie texte
#   ./bench-pi.sh 600         # 10 minutes
#   ./bench-pi.sh 300 json    # sortie JSON
#
# Mesure :
#   - CPU global et CPU process Docker SEH
#   - RAM (used %, RSS conteneur)
#   - Latence HTTP des endpoints /api/status, /api/perf
#   - Température CPU
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DURATION="${1:-300}"   # secondes
FORMAT="${2:-text}"    # text | json
INTERVAL=10            # samples toutes les 10s
CONTAINER="${SEH_CONTAINER:-seh-app}"

# Couleurs (text only)
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1;34m'; N='\033[0m'
[ "$FORMAT" = "json" ] && { G=''; Y=''; R=''; B=''; N=''; }

is_text() { [ "$FORMAT" = "text" ]; }

[ "$EUID" -ne 0 ] && [ ! -r /sys/class/thermal/thermal_zone0/temp ] && {
  echo "Note: lancer en sudo pour la température CPU" >&2
}

# ── Détection Pi ─────────────────────────────────────────────────────
MODEL="(unknown)"
if [ -f /sys/firmware/devicetree/base/model ]; then
  MODEL="$(tr -d '\0' < /sys/firmware/devicetree/base/model)"
fi
ARCH="$(uname -m)"

if is_text; then
  echo -e "${B}═══════════════════════════════════════════════════════════════${N}"
  echo -e "${B}  Simply Energy Home — Benchmark${N}"
  echo -e "${B}═══════════════════════════════════════════════════════════════${N}"
  echo "Modèle  : $MODEL"
  echo "Arch    : $ARCH"
  echo "Durée   : ${DURATION}s"
  echo "Sample  : toutes les ${INTERVAL}s"
  echo
fi

# ── Vérif présence du conteneur ─────────────────────────────────────
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo -e "${R}Conteneur '${CONTAINER}' non trouvé. Vérifier 'docker ps'.${N}" >&2
  exit 1
fi

# ── Helpers de lecture ───────────────────────────────────────────────
cpu_global() {
  awk '/^cpu / {idle=$5+$6; total=0; for(i=2;i<=8;i++) total+=$i; print 100-100*idle/total}' /proc/stat
}

mem_used_pct() {
  awk '/MemTotal:/ {t=$2} /MemAvailable:/ {a=$2} END {if(t>0) print 100-100*a/t}' /proc/meminfo
}

mem_available_mb() {
  awk '/MemAvailable:/ {print $2/1024}' /proc/meminfo
}

cpu_temp() {
  for p in /sys/class/thermal/thermal_zone0/temp; do
    if [ -r "$p" ]; then
      awk '{print $1/1000}' "$p"
      return
    fi
  done
  echo ""
}

container_stats() {
  # Format : CPU%,MEM_USAGE/LIMIT,MEM%
  docker stats --no-stream --format '{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}}' "$CONTAINER" 2>/dev/null || echo "0%,0MiB / 0MiB,0%"
}

http_latency_ms() {
  # Renvoie temps en ms ou -1 si erreur
  local url="$1"
  local t
  t=$(curl -o /dev/null -s -w '%{time_total}\n' --max-time 5 "$url" 2>/dev/null) || { echo "-1"; return; }
  awk -v t="$t" 'BEGIN {printf "%.0f\n", t*1000}'
}

# ── Init JSON ────────────────────────────────────────────────────────
JSON_FILE=""
if [ "$FORMAT" = "json" ]; then
  JSON_FILE="$(mktemp)"
  echo '{"model":"'"$MODEL"'","arch":"'"$ARCH"'","duration":'"$DURATION"',"samples":[' > "$JSON_FILE"
fi

# ── Header text ──────────────────────────────────────────────────────
if is_text; then
  printf "%-9s %-7s %-7s %-7s %-9s %-7s %-7s %-7s %-7s\n" \
    "Time" "CPU%" "MEM%" "MemMB" "Temp°C" "Cont%" "ContM" "/status" "/perf"
fi

# ── Boucle de samples ────────────────────────────────────────────────
START=$(date +%s)
END=$((START + DURATION))
FIRST=1

# Tableaux pour résumé
declare -a CPU_VALS MEM_VALS LAT_STATUS_VALS LAT_PERF_VALS

while [ "$(date +%s)" -lt "$END" ]; do
  T=$(date +%H:%M:%S)
  CPU=$(printf "%.1f" "$(cpu_global)")
  MEM=$(printf "%.1f" "$(mem_used_pct)")
  MEMMB=$(printf "%.0f" "$(mem_available_mb)")
  TEMP=$(cpu_temp)
  TEMP_PRINT="${TEMP:-—}"
  STATS="$(container_stats)"
  CONT_CPU=$(echo "$STATS" | cut -d',' -f1 | tr -d '%')
  CONT_MEM=$(echo "$STATS" | cut -d',' -f2 | awk '{print $1}')
  LAT_STATUS=$(http_latency_ms "http://localhost:8000/api/status")
  LAT_PERF=$(http_latency_ms "http://localhost:8000/api/perf")

  # Stocker pour résumé
  CPU_VALS+=("$CPU")
  MEM_VALS+=("$MEM")
  [ "$LAT_STATUS" -ge 0 ] 2>/dev/null && LAT_STATUS_VALS+=("$LAT_STATUS")
  [ "$LAT_PERF" -ge 0 ] 2>/dev/null && LAT_PERF_VALS+=("$LAT_PERF")

  if is_text; then
    printf "%-9s %-7s %-7s %-7s %-9s %-7s %-7s %-7s %-7s\n" \
      "$T" "$CPU" "$MEM" "$MEMMB" "$TEMP_PRINT" "$CONT_CPU" "$CONT_MEM" "${LAT_STATUS}ms" "${LAT_PERF}ms"
  else
    [ "$FIRST" = "0" ] && echo "," >> "$JSON_FILE"
    cat >> "$JSON_FILE" <<EOF
{"t":"$T","cpu_global":$CPU,"mem_pct":$MEM,"mem_avail_mb":$MEMMB,"temp_c":${TEMP:-null},"cont_cpu":$CONT_CPU,"cont_mem":"$CONT_MEM","lat_status_ms":$LAT_STATUS,"lat_perf_ms":$LAT_PERF}
EOF
    FIRST=0
  fi

  sleep "$INTERVAL"
done

# ── Résumé ──────────────────────────────────────────────────────────
avg() {
  if [ "$#" -eq 0 ]; then echo "0"; return; fi
  printf "%s\n" "$@" | awk '{s+=$1} END {if(NR>0) printf "%.1f", s/NR; else print "0"}'
}

p95() {
  if [ "$#" -eq 0 ]; then echo "0"; return; fi
  printf "%s\n" "$@" | sort -n | awk '{a[NR]=$1} END {idx=int(NR*0.95); if(idx<1)idx=1; print a[idx]}'
}

if is_text; then
  echo
  echo -e "${B}═══════════════════════════════════════════════════════════════${N}"
  echo -e "${B}  Résumé${N}"
  echo -e "${B}═══════════════════════════════════════════════════════════════${N}"
  echo "  CPU global moyen        : $(avg "${CPU_VALS[@]}")%"
  echo "  RAM utilisée moyenne    : $(avg "${MEM_VALS[@]}")%"
  echo "  Latence /api/status moy : $(avg "${LAT_STATUS_VALS[@]}") ms"
  echo "  Latence /api/status p95 : $(p95 "${LAT_STATUS_VALS[@]}") ms"
  echo "  Latence /api/perf moy   : $(avg "${LAT_PERF_VALS[@]}") ms"
  echo
  echo "Conseils Pi Zero 2W :"
  echo "  • CPU moyen idéal < 30% — au-dessus, le throttle s'active"
  echo "  • RAM idéal < 75% — au-dessus, swap probable"
  echo "  • /api/status idéal < 50 ms — au-dessus, polling à ralentir"
fi

if [ "$FORMAT" = "json" ]; then
  echo "],\"summary\":{\"cpu_avg\":$(avg "${CPU_VALS[@]}"),\"mem_avg\":$(avg "${MEM_VALS[@]}"),\"lat_status_avg\":$(avg "${LAT_STATUS_VALS[@]}"),\"lat_status_p95\":$(p95 "${LAT_STATUS_VALS[@]}"),\"lat_perf_avg\":$(avg "${LAT_PERF_VALS[@]}")}}" >> "$JSON_FILE"
  cat "$JSON_FILE"
  rm "$JSON_FILE"
fi
