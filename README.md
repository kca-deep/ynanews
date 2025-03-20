# YNA 뉴스 수집 및 분석 시스템

연합뉴스(YNA) RSS 피드에서 최신 뉴스를 수집하고, 키워드 기반으로 필터링한 후 요약 및 중복 기사를 찾아 분석하는 자동화 시스템입니다.

## 주요 기능

- 연합뉴스 RSS 피드에서 최신 뉴스 수집
- 설정한 키워드 기반으로 관련 기사 필터링
- OpenAI GPT 모델을 활용한 기사 요약 생성
- 구글 뉴스 검색을 통한 유사/중복 기사 탐색
- 분석 결과를 엑셀 파일로 저장
- Gmail API를 통한 자동 이메일 발송
- OpenAI API 사용량 및 비용 추적 (토큰 단위 정확한 계산)

## 필요 라이브러리

```
requests>=2.25.1
feedparser>=6.0.8
python-dotenv>=0.15.0
tabulate>=0.8.9
openpyxl>=3.0.9
beautifulsoup4>=4.9.3
google-api-python-client>=2.70.0
google-auth>=2.16.0
google-auth-oauthlib>=1.0.0
google-auth-httplib2>=0.1.0
tqdm>=4.64.0
tiktoken>=0.5.1
```

## 시스템 요구사항

- Python 3.7 이상
- OpenAI API 키
- Gmail API OAuth2 인증 정보 (이메일 발송 기능 사용 시)

## 프로젝트 설정 방법

1. 저장소 클론
   ```bash
   git clone https://github.com/your-username/ynanews.git
   cd ynanews
   ```

2. 가상환경 생성 및 활성화
   ```bash
   python -m venv venv
   # Windows
   venv\Scripts\activate
   # macOS/Linux
   source venv/bin/activate
   ```

3. 필요 라이브러리 설치
   ```bash
   pip install -r requirements.txt
   ```

4. 환경 변수 설정
   - `.env` 파일을 생성하고 다음과 같이 설정합니다:
   ```
   # OpenAI API 키
   OPENAI_API_KEY=your_openai_api_key

   # 검색할 키워드 (쉼표로 구분)
   SEARCH_KEYWORDS=ICT,AI

   # 이메일 발송 설정
   SEND_EMAIL=true
   GMAIL_SENDER_EMAIL=your_email@gmail.com
   GMAIL_RECIPIENTS=recipient1@example.com, recipient2@example.com
   GMAIL_CREDENTIALS_FILE=token.json
   ```

5. Gmail API 설정 (이메일 발송 기능 사용 시)
   - [Google Cloud Console](https://console.cloud.google.com/)에서 프로젝트 생성
   - Gmail API 활성화
   - OAuth2 인증 정보 생성
   - 다운로드한 인증 정보를 `token.json` 파일로 저장

## 실행 방법

```bash
python main.py
```

## 실행 과정

1. **환경 변수 로드**
   - `.env` 파일에서 API 키, 검색 키워드 등 설정 로드

2. **뉴스 수집**
   - 연합뉴스 RSS 피드에서 최신 뉴스 기사 수집
   - 최근 2일 이내 기사만 필터링

3. **키워드 기반 필터링**
   - 제목과 본문에 설정한 키워드가 포함된 기사만 선별
   - 실제 기사 URL에 접속하여 전체 본문 수집

4. **기사 요약**
   - OpenAI의 GPT-4o-mini 모델을 활용하여 각 기사의 요약문 생성
   - 요약은 3문장 이내로 생성됨

5. **중복 기사 탐색**
   - 구글 뉴스 검색을 통해 유사한 기사 탐색
   - 기사 제목의 유사도가 70% 이상인 경우 중복으로 판단
   - 각 기사당 최대 3개의 중복 기사 정보 수집

6. **결과 저장**
   - 수집 및 분석된 데이터를 엑셀 파일로 저장
   - 파일명: `output_YYYYMMDD_HHMMSS.xlsx`

7. **이메일 발송**
   - 환경 변수 `SEND_EMAIL`이 `true`로 설정된 경우 이메일 발송
   - 지정된 수신자들에게 결과 엑셀 파일 첨부하여 전송

8. **OpenAI API 사용량 및 비용 추적**
   - 프로그램 실행 중 모든 API 호출의 토큰 수를 계산
   - 입력 토큰, 출력 토큰, 전체 토큰 수를 정확히 측정
   - GPT-4o-mini 모델 기준 비용 계산 (입력: $0.15/1M, 출력: $0.60/1M)
   - 설정된 환율(기본: 1$=1480원)로 원화 비용 환산
   - 모든 사용량 및 비용 정보는 로그 파일에 기록되며 메일 본문에도 포함

## 로깅

- 모든 작업 과정은 로그 파일에 기록됨
- 로그 파일명: `ynanews-YYYYMMDD_HHMMSS.log`
- 컬러 코딩된 로그 메시지를 통해 직관적인 상태 확인 가능
- OpenAI API 사용량 및 비용 정보가 로그에 상세히 기록됨

## 문제 해결

- **API 오류 발생 시**: OpenAI API 키가 유효한지 확인
- **이메일 발송 실패 시**: OAuth2 인증 정보가 유효한지 확인
- **중복 오류 처리**: 대부분의 에러에 대해 자동 재시도 로직 구현되어 있음

## 주의사항

- OpenAI API는 사용량에 따라 비용이 발생할 수 있습니다.
- Gmail API 사용 시 OAuth2 인증은 주기적으로 갱신이 필요할 수 있습니다.
- 크롤링 관련 법적 제한 사항을 확인하세요.

## 기여 방법

1. 이 저장소를 포크합니다.
2. 새로운 기능 브랜치를 생성합니다 (`git checkout -b feature/amazing-feature`).
3. 변경 사항을 커밋합니다 (`git commit -m 'Add some amazing feature'`).
4. 브랜치에 푸시합니다 (`git push origin feature/amazing-feature`).
5. Pull Request를 생성합니다. 