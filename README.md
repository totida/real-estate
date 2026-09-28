# 서면아이파크 25평 새 매물 알림

네이버 부동산에서 **서면아이파크 1단지·2단지 25평** 매물을 30분마다 조회하고,
새 매물이 올라오면 알림을 보냅니다. (GitHub Actions로 실행)

## 동작 방식
1. `config.json`의 `keyword`(서면아이파크)로 네이버 부동산 단지를 검색해, 이름에 키워드가 들어간 단지(1단지, 2단지)를 모두 감시합니다.
2. 매매·전세·월세 매물 목록을 가져와 `state/seen.json`에 저장된 기존 매물과 비교합니다.
3. 새 매물 중 **25평**(`config.json`의 `pyeong`)인 것만 알림을 보냅니다.
   - **GitHub 이슈** (기본값): 저장소에 `새매물` 라벨로 이슈가 만들어지고, GitHub 앱이나 이메일로 알림이 옵니다.
   - **텔레그램** (선택): 아래 시크릿을 설정하면 텔레그램으로도 보냅니다.
4. 첫 실행은 현재 매물을 기준으로 저장만 하고 알림은 보내지 않습니다.

## 설정
- **텔레그램 알림**: 저장소 Settings → Secrets and variables → Actions에서
  `TELEGRAM_BOT_TOKEN`(@BotFather에서 발급), `TELEGRAM_CHAT_ID`를 추가합니다.
- **단지 직접 지정**: 자동 검색이 안 되면 `config.json`에 단지번호를 적습니다.
  단지번호는 `new.land.naver.com/complexes/<단지번호>` URL에서 확인할 수 있습니다.
  ```json
  "complexes": {"12345": "서면아이파크1단지", "67890": "서면아이파크2단지"}
  ```
- **평형**: `"pyeong": [25]` 네이버 표기와 같이 공급면적 기준(㎡ ÷ 3.3058, 반올림)입니다.
  여러 평형은 `[25, 34]`, 전체는 `[]`로 설정합니다.
- **거래 유형**: `trade_types`: `A1` 매매, `B1` 전세, `B2` 월세
- **주기**: `.github/workflows/naver-alert.yml`의 `cron`

## 로컬 실행
```bash
python3 naver_alert.py          # 조회 (알림은 환경변수가 있을 때만 전송)
python3 -m unittest discover -s tests
```
