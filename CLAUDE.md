# CLAUDE.md

## 목적
네이버 부동산에서 부산 7개 단지의 25~26평 **매매** 매물을 하루 한 번 조회해, 단지별 브리핑
메일과 폰 알림을 보내는 봇(`naver_alert.py`). 안드로이드 탭의 **Termux**에서 돈다.

## 현재 상태
- 탭 Termux cron 매일 10:00 → `termux/run-alert.sh` → `naver_alert.py` (실행마다 `git pull`)
- 2026-10-05: 탭이 원격지에서 꺼져 멈춘 상태. 다른 실행 위치를 찾았지만 막혀서 탭 유지로 결정.

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
- 작업이 마무리되거나 대화가 길어지면 project-wrapup 스킬로 정리할지 먼저 물어볼 것.

## 문서 (필요할 때만 읽을 것)
- `docs/작업기록.md` — 시행착오, 판단 규칙(가격 변동·같은 집·재등록), Termux 설정, 문제 해결
- `README.md` — 설치·설정 사용법

## TODO
- 탭 다시 켜기 → `tail ~/alert.log`로 10시 실행 확인 (Termux:Boot가 crond·wake-lock을 다시 켜는지)
- GitHub 저장소 Settings → Default branch를 `main`으로 바꾸기
