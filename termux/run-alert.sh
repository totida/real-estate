#!/data/data/com.termux/files/usr/bin/bash
# Termux 에서 매물 조회를 실행한다. 메일 브리핑이 설정돼 있으면 메일로 받고,
# 아니면(또는 메일 전송 실패 시) 새 매물·사라진 매물을 폰 알림으로 띄운다. 조회 실패도 알린다.
# 기록: ~/alert.log, 사라진 매물 목록: python naver_alert.py --history
# cron 에서는 Termux 프로그램 경로가 빠질 수 있어 직접 넣는다
export PATH="/data/data/com.termux/files/usr/bin:$PATH"
cd "$(dirname "$0")/.." || exit 1
git pull -q --ff-only >/dev/null 2>&1  # 코드 자동 업데이트 (실패해도 계속)

out=$(python naver_alert.py 2>&1)
code=$?
{ date; echo "$out"; echo; } >> ~/alert.log

# 🏠 새 매물 / 📉 사라진 매물 부분만 잘라낸다
section() { echo "$out" | awk -v m="$1" 'index($0, m) == 1 {f = 1; next} /^(🏠|📉|알림 전송)/ {f = 0} f'; }

# 메일을 보냈거나 브리핑 시간이 아니라 생략했으면 폰 알림 대신 메일로 받는다
mailed=$(echo "$out" | grep -m1 -e '^메일 전송 완료' -e '^메일 전송 생략')
if [ -z "$mailed" ] && echo "$out" | grep -q '^메일 전송 실패'; then
  termux-notification --id naver-mail --title "브리핑 메일 전송 실패" \
    --content "$(echo "$out" | grep -m1 '^메일 전송 실패' | cut -c1-300)"
fi

new_title=$([ -z "$mailed" ] && echo "$out" | grep -m1 '^🏠')
if [ -n "$new_title" ]; then
  url=$(echo "$out" | grep -m1 -o 'https://fin.land.naver.com/articles/[0-9]*')
  termux-notification --id naver --priority high --title "$new_title" \
    --content "$(section '🏠' | grep -A1 '^- ' | grep -v '^--' | head -12)" \
    --action "termux-open-url $url"
fi

gone_title=$([ -z "$mailed" ] && echo "$out" | grep -m1 '^📉')
if [ -n "$gone_title" ]; then
  termux-notification --id naver-gone --title "$gone_title" \
    --content "$(section '📉' | grep '^- ' | head -8)"
fi

if [ -z "$new_title$gone_title" ] && [ "$code" -ne 0 ]; then
  termux-notification --id naver-error --title "매물 조회 실패" \
    --content "$(echo "$out" | grep -m1 -e '경고' -e '없습니다' | cut -c1-300)"
fi
