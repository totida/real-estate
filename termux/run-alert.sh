#!/data/data/com.termux/files/usr/bin/bash
# Termux 에서 매물 조회를 실행한다. 새 매물이 있거나 조회에 실패하면 폰 알림을 띄운다.
# 기록: ~/alert.log
cd "$(dirname "$0")/.." || exit 1
git pull -q --ff-only >/dev/null 2>&1  # 코드 자동 업데이트 (실패해도 계속)

out=$(python naver_alert.py 2>&1)
code=$?
{ date; echo "$out"; echo; } >> ~/alert.log

title=$(echo "$out" | grep -m1 '^🏠')
if [ -n "$title" ]; then
  url=$(echo "$out" | grep -m1 -o 'https://fin.land.naver.com/articles/[0-9]*')
  termux-notification --id naver --priority high --title "$title" \
    --content "$(echo "$out" | grep -A1 '^- ' | grep -v '^--' | head -12)" \
    --action "termux-open-url $url"
elif [ "$code" -ne 0 ]; then
  termux-notification --id naver-error --title "매물 조회 실패" \
    --content "$(echo "$out" | grep -m1 -e '경고' -e '없습니다' | cut -c1-300)"
fi
