# 부산 아파트 25~26평 새 매물 알림

네이버 부동산(fin.land.naver.com)에서 아래 단지들의 **25~26평** 매물을 하루 한 번 조회하고,
새 매물이 올라오면 알림을 보냅니다. 안드로이드 폰의 **Termux**에서 실행합니다.

- 서면아이파크 (1단지·2단지)
- 롯데캐슬 인피니엘 (문현)
- 대연양우내안애퍼스트
- 대연푸르지오클라센트
- 양정포레힐즈 스위첸
- 연산더샵

> GitHub Actions·클라우드 서버는 네이버가 해외/클라우드 IP 접속을 막아 쓸 수 없습니다.
> 또 Python·curl 로 매물 API 를 직접 부르면 429(요청 과다)로 막히므로,
> 폰에 설치한 **크로미움을 화면 없이 띄워** 그 안에서 조회합니다.

## 동작 방식
1. 크로미움을 띄워 fin.land 지도 페이지를 열고, 그 페이지 안에서 `config.json`의 `complexes`
   단지번호별 매매·전세·월세 매물 목록 API 를 호출합니다. (파이썬은 표준 라이브러리만 사용)
2. `state/seen.json`에 저장된 기존 매물과 비교합니다. 같은 집을 여러 중개사가 올린 매물은
   한 건으로 묶어서 대표 매물이 바뀌어도 새 매물로 보지 않습니다.
3. 새 매물 중 **25~26평**(`config.json`의 `pyeong`)인 것만 알림을 보냅니다.
   - **폰 알림**: `termux/run-alert.sh` 가 Termux 알림을 띄웁니다. (누르면 매물 페이지 열림)
   - **텔레그램** (선택): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 환경변수가 있으면 보냅니다.
   - **GitHub 이슈** (선택): `GITHUB_TOKEN`, `GITHUB_REPOSITORY` 환경변수가 있으면 만듭니다.
4. 단지를 처음 조회할 때는 현재 매물을 기준으로 저장만 하고 알림은 보내지 않습니다.
5. 네이버가 429 로 막으면 차단이 길어지지 않도록 나머지 단지는 건너뛰고 끝냅니다.

## Termux 설치
F-Droid 에서 **Termux**, **Termux:API**, **Termux:Boot** 를 설치하고
(Termux 와 Termux:API 는 배터리 "제한 없음", 알림 허용) Termux 에서:

```bash
pkg update -y && pkg upgrade -y
pkg install -y tur-repo x11-repo
pkg install -y python git termux-api cronie termux-services chromium xorg-xserver-xvfb
git clone https://github.com/totida/real-estate.git
cd ~/real-estate && python naver_alert.py      # 첫 실행: 단지별 "기준 저장" 이 나오면 성공
```

매일 저녁 8시 자동 실행 (Termux 를 한 번 껐다 켠 뒤):
```bash
sv-enable crond
(crontab -l 2>/dev/null | grep -v run-alert; echo "0 20 * * * $HOME/real-estate/termux/run-alert.sh") | crontab -
termux-wake-lock
```

## 설정
- **단지 추가/삭제**: `config.json`의 `complexes`를 수정합니다.
  단지번호는 `fin.land.naver.com/complexes/<단지번호>` URL에서 확인할 수 있습니다.
  ```json
  "complexes": {"119101": "서면아이파크1단지", "119102": "서면아이파크2단지"}
  ```
- **평형**: `"pyeong": [25, 26]` 네이버 표기와 같이 공급면적 기준(㎡ ÷ 3.3058, 반올림)입니다.
  여러 평형은 `[25, 34]`, 전체는 `[]`로 설정합니다.
- **거래 유형**: `trade_types`: `A1` 매매, `B1` 전세, `B2` 월세
- **크로미움 옵션** (환경변수):
  - `CHROMIUM` 실행 파일 경로 직접 지정
  - `CHROMIUM_XVFB=1` 화면 없는 모드 대신 가상 화면(Xvfb)에서 실행
    (화면 없는 모드가 실패하면 Xvfb 가 설치돼 있을 때 자동으로 전환됩니다)
  - `CHROMIUM_FLAGS` 크로미움에 넘길 추가 옵션

## 문제 해결
- **429 / TOO_MANY_REQUESTS**: 네이버가 일시 차단한 상태입니다. 몇 시간 쉬었다가 다시 실행하세요.
  짧은 시간에 여러 번 실행하면 차단이 길어집니다.
- **크로미움 실행 실패**: 오류에 나온 로그를 확인하고, `CHROMIUM_XVFB=1 python naver_alert.py` 로 시도해 보세요.
- 실행 기록: `tail -50 ~/alert.log`

## 테스트
```bash
python3 -m unittest discover -s tests   # 크로미움이 있으면 가짜 서버로 브라우저 조회까지 확인
```
