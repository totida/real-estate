> 작업 전에 비공개 저장소 totida/claude-env 를 받아 `CLAUDE.md`(탭·오라클·클라우드 공통 환경 안내)를 먼저 읽을 것.

# CLAUDE.md

## 목적
네이버 부동산에서 부산 7개 단지의 25~26평 **매매** 매물을 하루 한 번 조회해, 단지별 브리핑
메일과 폰 알림을 보내는 봇(`naver_alert.py`). 안드로이드 탭의 **Termux**에서 돈다.

## 현재 상태
- 탭 Termux cron 매일 10:00 → `termux/run-alert.sh` → `naver_alert.py` (실행마다 `git pull`)
- 10/4 낮~10/5 15:45 탭 cron 전체가 멈췄다가 다시 돎. 다른 실행 위치는 막혀서 탭 유지로 결정.
- 10/6 10:01 첫 조회 전부 `Failed to fetch` → 10분 뒤 자동 재조회 성공, 메일 정상 발송.

## 핵심 결정사항
- **한국 IP + 실제 브라우저**여야 조회된다. Termux 크로미움을 화면 없이 띄워
  fin.land 지도 페이지 안에서 fetch 한다. curl·Python 직접 호출은 429.
- 연결 단계에서 막히는 곳(15초 타임아웃, 브라우저로도 해결 안 됨): GitHub Actions,
  Claude 클라우드 세션, stock 저장소의 오라클 서버(일본 오사카, 10/5 확인).
  → **이 저장소를 여는 클라우드 세션에서는 네이버 부동산을 시험할 수 없다.** 로직은 오프라인 테스트로.
- 하루 한 번만 조회. 짧은 시간에 여러 번 부르면 IP 단위로 429 차단되고, 429를 받으면 즉시 중단한다.
- **GitHub 기본 브랜치가 아직 옛 버전**(`claude/naver-realestate-alert-mmxs08`)이다. 작업·clone은 `main` 기준.

## 작업 규칙
- 테스트: `python3 -m unittest discover -s tests`
- **테스트가 실제 메일을 보내면 안 된다.** 테스트 파일 맨 위의 막음(`LOCAL_PATH` 임시 경로,
  메일 환경변수 제거, `smtplib` 차단)을 풀지 말 것. 10/3에 실제로 시험 메일이 나갔다.
- 비밀값(Gmail 앱 비밀번호 등)은 탭의 `local.json`에만 둔다. 저장소·대화에 남기지 않는다.
- 탭 Claude 세션(proot)은 `~` 가 `/root` 라서 Termux 홈 파일이 안 보인다. 실행 기록은
  `/data/data/com.termux/files/home/alert.log`, push 는 `HOME=/data/data/com.termux/files/home` 를 붙여서
  (gh 로그인 정보가 Termux 홈에 있음). 파이썬도 Termux 것(`$PREFIX/bin` 을 PATH 앞에)으로.
- 메일·기록 변경 없이 네이버 조회만 점검: `python naver_alert.py --check-brokers 133976` (한 번만, 반복 금지)
- 작업이 마무리되거나 대화가 길어지면 project-wrapup 스킬로 정리할지 먼저 물어볼 것.

## 문서 (필요할 때만 읽을 것)
- `docs/작업기록.md` — 시행착오, 판단 규칙(가격 변동·같은 집·재등록), Termux 설정, 문제 해결
- `README.md` — 설치·설정 사용법

## TODO
- 다음 조회 실패 때 `state/browser-fail/` 최신 기록으로 `Failed to fetch` 원인 확인 (메모리 부족 추정)
- 10/4~10/5 탭 cron 정지 원인 미확인 (쇼핑도 같이 멈춤 → 탭 전체 문제라 별도 세션에서)
- GitHub 저장소 Settings → Default branch를 `main`으로 바꾸기
