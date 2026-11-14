#!/usr/bin/env bash
# Regenerates every media file in fixtures/run/ with ffmpeg only. Idempotent: safe to re-run.
# Times and durations here MUST match the JSON fixtures (tests/test_contracts.py checks them).
#   frames/*.jpg  <- times parsed from candidates.json filenames (frames/<segment_id>_<t>s.jpg)
#   voice/*.mp3   <- assets.json voice durations (4 / 5 / 6 s)
#   shots/*.mp4   <- timeline.json shots
set -euo pipefail

cd "$(dirname "$0")/run"
command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }

FF=(ffmpeg -hide_banner -loglevel error -y)
# Strip encoder/version tags so re-runs on other machines produce fewer spurious git diffs.
BITEXACT=(-fflags +bitexact -flags:v +bitexact -flags:a +bitexact -map_metadata -1)
X264=(-c:v libx264 -preset medium -pix_fmt yuv420p)

# drawtext needs libfreetype + a font; fall back to plain color sources if either is missing.
FILTERS="$(ffmpeg -hide_banner -filters 2>/dev/null)"  # captured: grep -q on a pipe + pipefail = SIGPIPE
if grep -q ' drawtext ' <<<"$FILTERS" &&
   ffmpeg -hide_banner -loglevel error -f lavfi -i color=c=black:s=64x64:d=0.1 \
     -vf "drawtext=text=ok" -f null - 2>/dev/null; then
  HAS_DRAWTEXT=1
else
  HAS_DRAWTEXT=0
  echo "warning: drawtext unavailable, generating media without text" >&2
fi
text() {  # text <drawtext options> -> ",drawtext=..." or "" when unavailable
  if [[ $HAS_DRAWTEXT == 1 ]]; then printf ',drawtext=%s' "$1"; fi
}
HAS_ASS=0
grep -q ' ass ' <<<"$FILTERS" && HAS_ASS=1

rm -rf frames voice cards shots screenshots
mkdir -p frames voice cards/drafts shots screenshots

# --- recording.mp4: 30 s 1920x1080 30 fps "sloppy screen recording" ---------------
# A static desktop with the app window (testsrc2, 640x360) placed inside the s03 focus box
# [0.55,0.2,0.4,0.6]; full-frame testsrc2 at 1080p would be ~6 MB at crf 32.
"${FF[@]}" \
  -f lavfi -i "color=c=0x202428:s=1920x1080:r=30:d=30" \
  -f lavfi -i "testsrc2=s=640x360:r=30:d=30" \
  -f lavfi -i "sine=frequency=220:sample_rate=48000:duration=30" \
  -filter_complex "[0:v][1:v]overlay=x=1120:y=360$(text "text='PantryPal  t=%{pts\:hms}':fontsize=60:fontcolor=white:x=60:y=60")[v];[2:a]volume=0.2[a]" \
  -map "[v]" -map "[a]" "${X264[@]}" -crf 32 -c:a aac -b:a 48k -ac 1 -t 30 \
  "${BITEXACT[@]}" -movflags +faststart recording.mp4

# --- audio.wav: 16 kHz mono, extracted (ingest output) ----------------------------
"${FF[@]}" -i recording.mp4 -vn -ac 1 -ar 16000 -c:a pcm_s16le -bitexact -map_metadata -1 audio.wav

# --- screenshots/home.png: README screenshot (ingest output) ----------------------
"${FF[@]}" -f lavfi -i "color=c=0xF4A259:s=360x640:d=1$(text "text='PantryPal':fontsize=40:fontcolor=white:x=(w-tw)/2:y=80")" \
  -frames:v 1 screenshots/home.png

# --- frames/*.jpg: times taken from candidates.json -------------------------------
grep -o '"frames/[^"]*\.jpg"' candidates.json | tr -d '"' | while read -r f; do
  t="${f##*_}"; t="${t%s.jpg}"
  "${FF[@]}" -ss "$t" -i recording.mp4 -frames:v 1 -vf "scale=640:-2" -q:v 6 "$f" </dev/null
done

# --- voice/*.mp3: 4 / 5 / 6 s sine "voice-over" at different pitches ---------------
for spec in "s01 330 4" "s02 440 5" "s03 550 6"; do
  read -r shot hz dur <<<"$spec"
  "${FF[@]}" -f lavfi -i "sine=frequency=$hz:sample_rate=22050:duration=$dur" \
    -af "volume=0.5" -ac 1 -c:a libmp3lame -b:a 32k -write_xing 1 "${BITEXACT[@]}" "voice/$shot.mp3"
done

# --- music.wav: 15 s quiet bed ----------------------------------------------------
"${FF[@]}" -f lavfi -i "sine=frequency=196:sample_rate=8000:duration=15" \
  -af "volume=0.05" -ac 1 -c:a pcm_s16le -bitexact -map_metadata -1 music.wav

# --- cards: 4 drafts + chosen (index 2 per assets.json) ---------------------------
colors=(0x2E4057 0x8C4A9E 0xF4A259 0x5B8E7D)
for i in 0 1 2 3; do
  "${FF[@]}" -f lavfi -i "color=c=${colors[$i]}:s=1280x720:d=1$(text "text='PantryPal':fontsize=110:fontcolor=white:x=(w-tw)/2:y=(h-th)/2-40")$(text "text='dinner from your fridge':fontsize=44:fontcolor=white:x=(w-tw)/2:y=(h/2)+70")" \
    -frames:v 1 "cards/drafts/s01_$i.png"
done
cp cards/drafts/s01_2.png cards/s01.png

# --- shots/*.mp4: one per timeline.json shot, 1280x720 30 fps, voice padded -------
VOUT=(-r 30 "${X264[@]}" -crf 34 -c:a aac -b:a 48k -ac 1 -ar 44100 "${BITEXACT[@]}")
# s01: card, 4.0 s
"${FF[@]}" -loop 1 -framerate 30 -t 4 -i cards/s01.png -i voice/s01.mp3 \
  -filter_complex "[0:v]scale=1280:720,format=yuv420p[v];[1:a]apad[a]" -map "[v]" -map "[a]" -t 4 \
  "${VOUT[@]}" shots/s01.mp4
# s02: recording 5.0-9.5 at 1x + 0.5 s freeze = 5.0 s
"${FF[@]}" -ss 5 -t 4.5 -i recording.mp4 -i voice/s02.mp3 \
  -filter_complex "[0:v]scale=1280:720,fps=30,tpad=stop_mode=clone:stop_duration=0.5[v];[1:a]apad[a]" \
  -map "[v]" -map "[a]" -t 5 "${VOUT[@]}" shots/s02.mp4
# s03: recording 12.0-18.0, zoom into box [0.55,0.2,0.4,0.6] eased over 0.8 s (24 frames) = 6.0 s
Z="1+0.6667*min(on/24\,1)"
"${FF[@]}" -ss 12 -t 6 -i recording.mp4 -i voice/s03.mp3 \
  -filter_complex "[0:v]fps=30,zoompan=z='$Z':x='max(0\,min(iw-iw/zoom\,iw*0.75-iw/zoom/2))':y='max(0\,min(ih-ih/zoom\,ih*0.5-ih/zoom/2))':d=1:s=1280x720:fps=30[v];[1:a]apad[a]" \
  -map "[v]" -map "[a]" -t 6 "${VOUT[@]}" shots/s03.mp4

# --- final.mp4: concat + ducked music at -18 dB + burned-in captions --------------
SUBS=""
[[ $HAS_ASS == 1 ]] && SUBS=",ass=captions.ass"
"${FF[@]}" -i shots/s01.mp4 -i shots/s02.mp4 -i shots/s03.mp4 -i music.wav \
  -filter_complex "[0:v][0:a][1:v][1:a][2:v][2:a]concat=n=3:v=1:a=1[cv][ca];[cv]null${SUBS}[v];\
[ca]asplit[voice][key];[3:a]aresample=44100,volume=-18dB[m];[m][key]sidechaincompress=threshold=0.02:ratio=6[duck];\
[voice][duck]amix=inputs=2:duration=first:normalize=0[a]" \
  -map "[v]" -map "[a]" -t 15 "${VOUT[@]}" -movflags +faststart final.mp4

du -ch recording.mp4 audio.wav music.wav final.mp4 frames voice cards shots screenshots | tail -1
